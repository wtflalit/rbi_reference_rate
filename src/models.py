"""Domain objects shared across scrapers, Excel writer and Drive client."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable

# FBIL publishes four reference rates. JPY is quoted per 100 yen, the others
# per single unit of foreign currency. `unit` keeps that explicit so nobody
# silently compares a per-100 number against a per-1 number later.
CURRENCY_UNITS: dict[str, int] = {
    "USD": 1,
    "EUR": 1,
    "GBP": 1,
    "JPY": 100,
}

# Canonical ordering used in every output file.
EXPECTED_PAIRS: tuple[str, ...] = ("USD/INR", "EUR/INR", "GBP/INR", "JPY/INR")


class RateError(Exception):
    """Base class for anything that goes wrong while obtaining rates."""


class SourceUnavailable(RateError):
    """The source could not be reached or its layout no longer parses.

    This is a *retryable / fall-through* condition: the orchestrator moves on
    to the next source in the chain.
    """


class NoDataForDate(RateError):
    """The source responded fine but has published nothing for this date.

    This is the normal holiday / not-yet-published case and must NOT be
    treated as a failure by the orchestrator.
    """


@dataclass(frozen=True, slots=True)
class Rate:
    """One published reference rate."""

    pair: str  # e.g. "USD/INR"
    base: str  # e.g. "USD"
    quote: str  # always "INR" for FBIL reference rates
    unit: int  # 1, or 100 for JPY
    value: float  # rupees per `unit` of `base`
    rate_date: dt.date  # the business date the rate applies to
    source: str  # which scraper produced it, e.g. "fbil"
    retrieved_at: dt.datetime = field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc)
    )

    def as_row(self) -> dict[str, object]:
        """Flatten to the exact column layout used in the workbooks."""
        return {
            "Date": self.rate_date,
            "Pair": self.pair,
            "Base Currency": self.base,
            "Quote Currency": self.quote,
            "Unit": self.unit,
            "Rate (INR)": self.value,
            "Source": self.source,
            "Retrieved At (UTC)": self.retrieved_at.replace(tzinfo=None),
        }


def make_rate(
    base: str,
    value: float,
    rate_date: dt.date,
    source: str,
) -> Rate:
    """Build a Rate for an INR pair, filling in the conventional unit."""
    base = base.upper().strip()
    return Rate(
        pair=f"{base}/INR",
        base=base,
        quote="INR",
        unit=CURRENCY_UNITS.get(base, 1),
        value=float(value),
        rate_date=rate_date,
        source=source,
    )


def sort_rates(rates: Iterable[Rate]) -> list[Rate]:
    """Canonical ordering: known pairs first in EXPECTED_PAIRS order."""
    order = {p: i for i, p in enumerate(EXPECTED_PAIRS)}
    return sorted(rates, key=lambda r: (order.get(r.pair, 99), r.pair))
