"""Business-day logic for the Mumbai FX market."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# FBIL publishes the reference rates at ~13:30 IST every Mumbai business day.
PUBLICATION_TIME_IST = dt.time(13, 30)


def now_ist() -> dt.datetime:
    """Current wall-clock time in Mumbai (runners are UTC — never use local)."""
    return dt.datetime.now(IST)


def today_ist() -> dt.date:
    return now_ist().date()


def load_holidays(path: str | Path) -> dict[dt.date, str]:
    """Read config/holidays.yml into {date: reason}.

    Missing or malformed files degrade to an empty dict — the holiday list is
    an optimisation, so a bad file must never take the pipeline down.
    """
    path = Path(path)
    if not path.exists():
        log.warning("Holiday file %s not found; relying on data-driven skip", path)
        return {}

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        log.exception("Could not parse %s; continuing without a holiday list", path)
        return {}

    holidays: dict[dt.date, str] = {}
    # File is nested by year: {2026: {"2026-01-26": "Republic Day", ...}}
    for year_block in raw.values():
        if not isinstance(year_block, dict):
            continue
        for key, reason in year_block.items():
            try:
                day = (
                    key
                    if isinstance(key, dt.date)
                    else dt.date.fromisoformat(str(key).strip())
                )
            except ValueError:
                log.warning("Skipping unparseable holiday key %r in %s", key, path)
                continue
            holidays[day] = str(reason)

    log.debug("Loaded %d holidays from %s", len(holidays), path)
    return holidays


def is_weekend(day: dt.date) -> bool:
    return day.weekday() >= 5  # 5 = Saturday, 6 = Sunday


def skip_reason(day: dt.date, holidays: dict[dt.date, str]) -> str | None:
    """Return why `day` should be skipped, or None if it is a business day."""
    if is_weekend(day):
        return f"{day.strftime('%A')} — market closed"
    if day in holidays:
        return f"Mumbai bank holiday — {holidays[day]}"
    return None


def rates_probably_published(when: dt.datetime | None = None) -> bool:
    """True once the ~13:30 IST publication window has passed for today.

    Used only to write a friendlier log line when someone runs the script at
    09:00 IST and gets nothing back.
    """
    when = when or now_ist()
    return when.timetz().replace(tzinfo=None) >= PUBLICATION_TIME_IST


def previous_business_day(day: dt.date, holidays: dict[dt.date, str]) -> dt.date:
    """Walk backwards to the most recent non-weekend, non-holiday date."""
    cursor = day - dt.timedelta(days=1)
    for _ in range(30):  # guard against a pathological holiday file
        if skip_reason(cursor, holidays) is None:
            return cursor
        cursor -= dt.timedelta(days=1)
    raise RuntimeError(f"No business day found within 30 days before {day}")
