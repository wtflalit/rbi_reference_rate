"""Layout-tolerant extraction of reference rates from FBIL / RBI HTML.

Both sites are ASP.NET WebForms apps whose markup changes without notice —
column order moves, headers get renamed, extra wrapper tables appear. Rather
than hard-coding "table 3, row 2, column 4", this module walks *every* table
and keeps any row that looks like "<a currency> ... <a plausible INR rate>".

That plausibility check is what makes it safe: a stray number from a nav
banner or a footnote cannot masquerade as an exchange rate because it will
not land inside the expected band for that currency.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Substrings that identify a currency in a table cell, longest-first so that
# "US DOLLAR" wins over a bare "DOLLAR".
CURRENCY_PATTERNS: dict[str, tuple[str, ...]] = {
    "USD": ("USD", "US DOLLAR", "U.S. DOLLAR", "US$", "DOLLAR"),
    "EUR": ("EUR", "EURO"),
    "GBP": ("GBP", "POUND STERLING", "STERLING", "POUND", "GREAT BRITAIN"),
    "JPY": ("JPY", "JAPANESE YEN", "YEN"),
}

# Sanity bands in rupees, per the conventional unit (JPY per 100 yen).
# Deliberately wide — they exist to reject garbage, not to validate the market.
PLAUSIBLE_RANGES: dict[str, tuple[float, float]] = {
    "USD": (40.0, 200.0),
    "EUR": (45.0, 260.0),
    "GBP": (55.0, 320.0),
    "JPY": (20.0, 200.0),
}

_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*\.\d+|[-+]?\d[\d,]*")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def identify_currency(cell_text: str) -> str | None:
    """Map a table cell to a currency code, or None."""
    haystack = _clean(cell_text).upper()
    if not haystack or len(haystack) > 60:
        return None
    best: tuple[int, str] | None = None
    for code, patterns in CURRENCY_PATTERNS.items():
        for pattern in patterns:
            if pattern in haystack:
                # Prefer the most specific match across all currencies.
                if best is None or len(pattern) > best[0]:
                    best = (len(pattern), code)
    return best[1] if best else None


def _numbers_in(cells: list[str]) -> list[float]:
    values: list[float] = []
    for cell in cells:
        for token in _NUMBER_RE.findall(_clean(cell)):
            try:
                values.append(float(token.replace(",", "")))
            except ValueError:
                continue
    return values


def extract_rates_from_html(html: str) -> dict[str, float]:
    """Return {currency_code: rate_in_inr} for everything found in `html`.

    Scans all tables; for each row that names a currency, picks the first
    number in that row falling inside the currency's plausible band.
    """
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, float] = {}

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [_clean(c.get_text(" ")) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue

            # The currency label is normally the first non-empty cell.
            code = None
            label_index = 0
            for index, cell in enumerate(cells[:3]):
                code = identify_currency(cell)
                if code:
                    label_index = index
                    break
            if not code or code in found:
                continue

            low, high = PLAUSIBLE_RANGES[code]
            for value in _numbers_in(cells[label_index + 1 :]):
                if low <= value <= high:
                    found[code] = value
                    log.debug("Matched %s = %s from row %s", code, value, cells)
                    break

    return found


def extract_rate_date(html: str) -> dt.date | None:
    """Find the business date the table refers to.

    FBIL/RBI render it as a caption or heading such as
    "Reference Rate as on 08 Aug 2026" or "Date: 08/08/2026".
    """
    soup = BeautifulSoup(html, "lxml")
    text = _clean(soup.get_text(" "))
    # Drop ordinal suffixes up front — "7th August 2026" has no separator
    # between the day and the suffix, so the patterns below would miss it.
    text = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)

    patterns = (
        r"(?:as on|dated|date\s*[:\-]|for\s+the\s+date)\s*"
        r"([0-9]{1,2}[\s./\-][A-Za-z0-9]{2,9}[\s./\-][0-9]{2,4})",
        r"([0-9]{1,2}[\s./\-][A-Za-z]{3,9}[\s./\-][0-9]{4})",
        r"([0-9]{4}-[0-9]{2}-[0-9]{2})",
        r"([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        parsed = _parse_date_token(match.group(1))
        if parsed:
            return parsed
    return None


def _parse_date_token(token: str) -> dt.date | None:
    token = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", _clean(token), flags=re.I)
    for fmt in (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d.%m.%Y",
        "%d %b %y",
    ):
        try:
            return dt.datetime.strptime(token, fmt).date()
        except ValueError:
            continue
    return None


def extract_aspnet_state(html: str) -> dict[str, str]:
    """Pull __VIEWSTATE and friends out of a WebForms page.

    Required to POST a date filter back to fbil.org.in / rbi.org.in — the
    server rejects any postback that does not echo these hidden fields.
    """
    soup = BeautifulSoup(html, "lxml")
    state: dict[str, str] = {}
    for name in (
        "__VIEWSTATE",
        "__VIEWSTATEGENERATOR",
        "__EVENTVALIDATION",
        "__EVENTTARGET",
        "__EVENTARGUMENT",
        "__VIEWSTATEENCRYPTED",
    ):
        node = soup.find("input", {"name": name})
        if node is not None:
            state[name] = node.get("value", "") or ""
    return state
