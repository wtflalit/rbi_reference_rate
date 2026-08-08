"""RBI reference-rate archive — fallback when FBIL is unreachable.

RBI re-publishes FBIL's numbers, so the values are identical; only the
delivery mechanism differs. Two entry points are tried:

  1. the modern portal page (website.rbi.org.in), plain HTML; and
  2. the legacy ASP.NET archive (rbi.org.in/Scripts/ReferenceRateArchive.aspx),
     which needs a __VIEWSTATE postback carrying a from/to date range.

The legacy archive is the one your brief warned about. It is handled, but it
is genuinely brittle: RBI rotates control IDs and occasionally geo-throttles
non-Indian IPs, which GitHub's US-hosted runners will hit. Treat a persistent
RBI failure as expected rather than alarming — that is exactly why there is a
third source in the chain.
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

RBI_MODERN_URLS = (
    "https://website.rbi.org.in/web/rbi/-/reference-rate-archive",
    "https://website.rbi.org.in/web/rbi",
)
RBI_LEGACY_ARCHIVE = "https://www.rbi.org.in/Scripts/ReferenceRateArchive.aspx"

# Legacy WebForms control names. Verify with DevTools if this path breaks.
LEGACY_FORM_FIELDS = {
    "from": "hdnFrmDate",
    "to": "hdnToDate",
    "submit": "btnFetch",
}


class RBISource(RateSource):
    name = "rbi"
    description = "Reserve Bank of India archive (re-publishes FBIL rates)"

    def fetch(self, target_date: dt.date) -> list[Rate]:
        errors: list[str] = []

        for url in RBI_MODERN_URLS:
            try:
                html = self.get(url).text
            except SourceUnavailable as exc:
                errors.append(str(exc))
                continue
            rates = self._rates_from_page(html, target_date, url)
            if rates:
                return rates

        try:
            rates = self._fetch_legacy_archive(target_date)
        except SourceUnavailable as exc:
            errors.append(str(exc))
        else:
            if rates:
                return rates

        if errors:
            raise SourceUnavailable("RBI unreachable: " + "; ".join(errors))
        raise NoDataForDate(f"RBI archive has no reference rate for {target_date}")

    # -- internals ---------------------------------------------------------

    def _rates_from_page(
        self, html: str, target_date: dt.date, url: str
    ) -> list[Rate]:
        values = extract_rates_from_html(html)
        if not values:
            return []

        page_date = extract_rate_date(html)
        if page_date != target_date:
            log.info("%s shows %s, not %s", url, page_date, target_date)
            return []

        log.info("RBI: parsed %d rates for %s from %s", len(values), page_date, url)
        return [
            make_rate(code, value, page_date, self.name)
            for code, value in values.items()
        ]

    def _fetch_legacy_archive(self, target_date: dt.date) -> list[Rate]:
        """POST a single-day date range at the old ASP.NET archive page."""
        landing = self.get(RBI_LEGACY_ARCHIVE).text
        state = extract_aspnet_state(landing)
        if "__VIEWSTATE" not in state:
            log.debug("Legacy RBI archive did not return a WebForms page")
            return []

        stamp = target_date.strftime("%d/%m/%Y")
        payload = dict(state)
        payload[LEGACY_FORM_FIELDS["from"]] = stamp
        payload[LEGACY_FORM_FIELDS["to"]] = stamp
        payload[LEGACY_FORM_FIELDS["submit"]] = "Go"

        response = self.post(
            RBI_LEGACY_ARCHIVE,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": RBI_LEGACY_ARCHIVE,
                "Origin": "https://www.rbi.org.in",
            },
        )
        return self._rates_from_page(
            response.text, target_date, f"{RBI_LEGACY_ARCHIVE} (postback)"
        )
