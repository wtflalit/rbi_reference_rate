"""Common plumbing for every rate source."""

from __future__ import annotations

import abc
import datetime as dt
import logging
import re

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..models import Rate, SourceUnavailable

log = logging.getLogger(__name__)

# Both fbil.org.in and rbi.org.in serve a WAF challenge or an empty body to
# obviously-automated clients. A realistic browser fingerprint is required.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

DEFAULT_TIMEOUT = 30

# "1 USD = 83.4521" / "83.4521" / "8,345.21" / "83.4521 *"
_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")


class RateSource(abc.ABC):
    """A place reference rates can be obtained from.

    Contract for `fetch`:
      * return a non-empty list[Rate] on success
      * raise NoDataForDate  -> nothing published for that date (holiday etc.)
      * raise SourceUnavailable -> network/layout problem, try the next source
    """

    #: short identifier used in config, logs and the Excel "Source" column
    name: str = "base"

    #: human-readable, used in the README and the workbook metadata sheet
    description: str = ""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

    @abc.abstractmethod
    def fetch(self, target_date: dt.date) -> list[Rate]:
        """Return the reference rates published for `target_date`."""

    # -- helpers -----------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        reraise=True,
    )
    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """HTTP with retry/backoff, translating transport errors uniformly."""
        kwargs.setdefault("timeout", self.timeout)
        log.debug("%s %s", method, url)
        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
        except requests.RequestException as exc:
            log.warning("%s: %s %s failed: %s", self.name, method, url, exc)
            raise
        return response

    def get(self, url: str, **kwargs) -> requests.Response:
        try:
            return self._request("GET", url, **kwargs)
        except requests.RequestException as exc:
            raise SourceUnavailable(f"{self.name}: GET {url} failed: {exc}") from exc

    def post(self, url: str, **kwargs) -> requests.Response:
        try:
            return self._request("POST", url, **kwargs)
        except requests.RequestException as exc:
            raise SourceUnavailable(f"{self.name}: POST {url} failed: {exc}") from exc

    @staticmethod
    def parse_number(text: str) -> float | None:
        """Pull the first numeric token out of a messy table cell.

        Handles '83.4521', '1 USD = 83.4521', '83.4521*', '8,345.21', 'NA'.
        """
        if text is None:
            return None
        match = _NUMBER_RE.search(str(text).replace("\xa0", " "))
        if not match:
            return None
        try:
            return float(match.group(0).replace(",", ""))
        except ValueError:
            return None

    @staticmethod
    def parse_date(text: str) -> dt.date | None:
        """Parse the many date formats these sites use in table captions."""
        if not text:
            return None
        cleaned = re.sub(r"\s+", " ", str(text)).strip()
        # Strip ordinal suffixes: "1st April 2026" -> "1 April 2026"
        cleaned = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", cleaned, flags=re.I)
        formats = (
            "%Y-%m-%d",
            "%d-%m-%Y",
            "%d/%m/%Y",
            "%d %b %Y",
            "%d %B %Y",
            "%b %d, %Y",
            "%B %d, %Y",
            "%d-%b-%Y",
            "%d-%B-%Y",
            "%d.%m.%Y",
        )
        # Try the whole string, then any date-looking substring inside it.
        candidates = [cleaned]
        found = re.search(
            r"\d{1,2}[\s./-][A-Za-z0-9]{2,9}[\s./-]\d{2,4}|\d{4}-\d{2}-\d{2}",
            cleaned,
        )
        if found:
            candidates.append(found.group(0))
        for candidate in candidates:
            for fmt in formats:
                try:
                    return dt.datetime.strptime(candidate, fmt).date()
                except ValueError:
                    continue
        return None
