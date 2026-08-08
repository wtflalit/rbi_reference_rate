"""Frankfurter — a JSON mirror, used as the last resort.

Why this exists
---------------
Both FBIL and RBI are HTML-scraped ASP.NET sites with no contract to keep
their markup stable, and RBI in particular can throttle the US-hosted IPs
GitHub Actions runs on. Frankfurter (https://frankfurter.dev) is a free,
key-less JSON API that publishes an FBIL-sourced INR series from 2018 onward,
which makes it a cheap third leg for the fallback chain.

Read this before trusting the output
------------------------------------
Frankfurter can serve INR from more than one upstream. If the FBIL-specific
endpoint answers, the numbers are the official fix and are tagged
`frankfurter-fbil`. If only the default endpoint answers, the numbers come
from the ECB's daily fixing instead — close to the FBIL rate but NOT the same
number, and not the official Indian benchmark. Those rows are tagged
`frankfurter-ecb` so the provenance is visible in the workbook, and
`--require-official` on the CLI refuses them outright. Use that flag if the
output feeds anything where "close enough" is not acceptable.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..models import CURRENCY_UNITS, NoDataForDate, Rate, SourceUnavailable, make_rate
from .base import RateSource

log = logging.getLogger(__name__)

# Tried in order; {date} is substituted. The first two target FBIL data, the
# last is the generic (ECB-backed) endpoint.
ENDPOINT_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("frankfurter-fbil", "https://api.frankfurter.dev/fbil/{date}"),
    ("frankfurter-fbil", "https://api.frankfurter.dev/v1/{date}?provider=fbil"),
    ("frankfurter-ecb", "https://api.frankfurter.dev/v1/{date}"),
)

BASES = ("USD", "EUR", "GBP", "JPY")


class FrankfurterSource(RateSource):
    name = "frankfurter"
    description = "Frankfurter JSON API (FBIL series, ECB fallback)"

    #: set to False by the orchestrator when --require-official is passed
    allow_indicative: bool = True

    def fetch(self, target_date: dt.date) -> list[Rate]:
        errors: list[str] = []

        for tag, template in ENDPOINT_TEMPLATES:
            if tag == "frankfurter-ecb" and not self.allow_indicative:
                log.info("Skipping ECB endpoint: --require-official is set")
                continue
            try:
                rates = self._fetch_all_bases(template, tag, target_date)
            except SourceUnavailable as exc:
                errors.append(str(exc))
                continue
            if rates:
                if tag == "frankfurter-ecb":
                    log.warning(
                        "Falling back to ECB fixing for %s — these are INDICATIVE "
                        "rates, not the official FBIL benchmark",
                        target_date,
                    )
                return rates

        if errors:
            raise SourceUnavailable("Frankfurter unreachable: " + "; ".join(errors))
        raise NoDataForDate(f"Frankfurter has no INR rates for {target_date}")

    # -- internals ---------------------------------------------------------

    def _fetch_all_bases(
        self, template: str, tag: str, target_date: dt.date
    ) -> list[Rate]:
        url = template.format(date=target_date.isoformat())
        joiner = "&" if "?" in url else "?"
        collected: list[Rate] = []

        for base in BASES:
            query = f"{url}{joiner}base={base}&symbols=INR"
            response = self.get(query, headers={"Accept": "application/json"})
            try:
                payload = response.json()
            except ValueError as exc:
                raise SourceUnavailable(f"{query} returned non-JSON: {exc}") from exc

            value = (payload.get("rates") or {}).get("INR")
            if value is None:
                log.debug("No INR in response for base=%s at %s", base, query)
                continue

            # Frankfurter reports the *nearest available* date, which on a
            # holiday is the previous business day. Silently accepting that
            # would file stale numbers under today's date.
            reported = payload.get("date")
            if reported and reported != target_date.isoformat():
                log.info(
                    "Frankfurter returned %s for a %s request — no fix published",
                    reported,
                    target_date,
                )
                return []

            # FBIL quotes JPY per 100 yen; Frankfurter quotes per 1.
            scaled = float(value) * CURRENCY_UNITS.get(base, 1)
            collected.append(make_rate(base, scaled, target_date, tag))

        return collected
