"""FBIL — the authoritative publisher of INR reference rates since July 2018.

Background
----------
Until 2018 the RBI computed and published the USD/INR reference rate itself.
Since 10 July 2018 that job belongs to Financial Benchmarks India Pvt Ltd
(FBIL), which publishes USD/INR, EUR/INR, GBP/INR and JPY/INR at roughly
13:30 IST on every Mumbai business day. RBI now merely re-publishes FBIL's
numbers, which is why FBIL is the primary source here and RBI the fallback.

Implementation note
-------------------
FBIL exposes no documented public API. The reference-rate page is an ASP.NET
WebForms view, so this scraper:

  1. GETs the page and parses whatever table is on it (today's rate);
  2. if a *past* date is requested, replays the page's own postback with the
     hidden __VIEWSTATE fields to drive the archive date picker.

The postback field names in ARCHIVE_FORM_FIELDS below are the ones the site
has historically used, but WebForms control IDs change whenever the page is
edited. If step 2 stops working, open the page in a browser, submit the date
filter with DevTools' Network tab recording, copy the form-data key names from
the request, and update that dict — nothing else needs to change.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..models import NoDataForDate, Rate, SourceUnavailable, make_rate
from .base import RateSource
from .html_parsing import (
    extract_aspnet_state,
    extract_rate_date,
    extract_rates_from_html,
)

log = logging.getLogger(__name__)

FBIL_BASE = "https://www.fbil.org.in"

# Landing pages tried in order; the first that yields rates wins.
FBIL_URLS = (
    f"{FBIL_BASE}/modules/SecuritiesApproval/SecuritiesApproval.aspx"
    "?mq=o&op=referencerate",
    f"{FBIL_BASE}/modules/ReferenceRate/ReferenceRate.aspx",
    f"{FBIL_BASE}/",
)

# WebForms control names for the archive date filter. See module docstring.
ARCHIVE_FORM_FIELDS = {
    "date": "ctl00$ContentPlaceHolder1$txtFromDate",
    "submit": "ctl00$ContentPlaceHolder1$btnSubmit",
}


class FBILSource(RateSource):
    name = "fbil"
    description = "Financial Benchmarks India Pvt Ltd — official INR reference rates"

    def fetch(self, target_date: dt.date) -> list[Rate]:
        last_error: Exception | None = None

        for url in FBIL_URLS:
            try:
                html = self.get(url).text
            except SourceUnavailable as exc:
                last_error = exc
                continue

            if not html or len(html) < 200:
                log.debug("%s returned an empty body", url)
                continue

            rates = self._rates_from_page(html, target_date, url)
            if rates:
                return rates

            # Landing page shows a different day — try the archive postback.
            try:
                archived = self._fetch_archive(url, html, target_date)
            except SourceUnavailable as exc:
                last_error = exc
                continue
            if archived:
                return archived

        if last_error:
            raise SourceUnavailable(f"FBIL unreachable: {last_error}")
        raise NoDataForDate(f"FBIL published no reference rate for {target_date}")

    # -- internals ---------------------------------------------------------

    def _rates_from_page(
        self, html: str, target_date: dt.date, url: str
    ) -> list[Rate]:
        """Turn one page of HTML into Rates, but only if the date matches."""
        values = extract_rates_from_html(html)
        if not values:
            return []

        page_date = extract_rate_date(html)
        if page_date is None:
            log.warning(
                "%s: found rates %s but no readable date; refusing to guess "
                "that they belong to %s",
                url,
                values,
                target_date,
            )
            return []

        if page_date != target_date:
            log.info(
                "%s currently shows %s, not the requested %s",
                url,
                page_date,
                target_date,
            )
            return []

        log.info("FBIL: parsed %d rates for %s from %s", len(values), page_date, url)
        return [
            make_rate(code, value, page_date, self.name)
            for code, value in values.items()
        ]

    def _fetch_archive(
        self, url: str, landing_html: str, target_date: dt.date
    ) -> list[Rate]:
        """Replay the page's date-filter postback for a historical date."""
        state = extract_aspnet_state(landing_html)
        if "__VIEWSTATE" not in state:
            log.debug("%s is not a WebForms page; no archive postback possible", url)
            return []

        payload = dict(state)
        payload[ARCHIVE_FORM_FIELDS["date"]] = target_date.strftime("%d/%m/%Y")
        payload[ARCHIVE_FORM_FIELDS["submit"]] = "Submit"

        response = self.post(
            url,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": url,
                "Origin": FBIL_BASE,
            },
        )
        return self._rates_from_page(response.text, target_date, f"{url} (archive)")
