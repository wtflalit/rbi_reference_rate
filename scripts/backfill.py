#!/usr/bin/env python3
"""One-off historical backfill: every published rate from 2018 to today.

Why this does not scrape
------------------------
There are roughly 2,000 business days since FBIL took over reference-rate
publication in July 2018. Scraping fbil.org.in 2,000 times would be slow,
fragile, and rude — it is exactly the traffic pattern that gets an IP blocked,
and it would jeopardise the daily job that actually matters.

Instead this uses Frankfurter's *time-series* endpoint, which returns a whole
date range in a single request. The full backfill is about 32 requests (four
base currencies x eight year-chunks) rather than 8,000.

Provenance still matters
------------------------
The FBIL-specific endpoint is tried first; rows from it are tagged
`frankfurter-fbil` and are the official benchmark. If only the default
endpoint answers, the data is the ECB fixing — tagged `frankfurter-ecb` and
marked INDICATIVE in every file. Pass --require-official to abort instead of
silently filling years of history with numbers that are merely close.

Usage
-----
    python scripts/backfill.py                      # 2018-07-01 -> today
    python scripts/backfill.py --start 2024-01-01
    python scripts/backfill.py --no-per-date        # master workbook only
    python scripts/backfill.py --dry-run            # show what would be built
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from src.calendar_utils import today_ist  # noqa: E402
from src.excel_writer import merge_into_master, read_master, write_daily_workbook  # noqa: E402
from src.models import CURRENCY_UNITS, Rate, make_rate  # noqa: E402
from src.pdf_writer import write_daily_pdf  # noqa: E402
from src.sources.base import BROWSER_HEADERS  # noqa: E402

log = logging.getLogger("backfill")

# FBIL began publishing the reference rates on 10 July 2018.
FBIL_ERA_START = dt.date(2018, 7, 1)

BASES = ("USD", "EUR", "GBP", "JPY")

# Same precedence as the live source: FBIL data if available, ECB otherwise.
SERIES_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("frankfurter-fbil", "https://api.frankfurter.dev/fbil/{start}..{end}"),
    ("frankfurter-fbil", "https://api.frankfurter.dev/v1/{start}..{end}?provider=fbil"),
    ("frankfurter-ecb", "https://api.frankfurter.dev/v1/{start}..{end}"),
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start",
        default=FBIL_ERA_START.isoformat(),
        help=f"First date, YYYY-MM-DD. Default {FBIL_ERA_START} (FBIL era start).",
    )
    parser.add_argument("--end", help="Last date, YYYY-MM-DD. Default: today IST.")
    parser.add_argument("--output-dir", default="data")
    parser.add_argument(
        "--master-filename", default="reference_rates_master.xlsx"
    )
    parser.add_argument(
        "--no-per-date",
        action="store_true",
        help="Write only the master workbook, no per-date files.",
    )
    parser.add_argument(
        "--no-pdf", action="store_true", help="Skip the per-date PDFs."
    )
    parser.add_argument(
        "--require-official",
        action="store_true",
        help="Abort rather than backfill with indicative ECB data.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and report coverage without writing any files.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def fetch_series(
    start: dt.date, end: dt.date, require_official: bool
) -> tuple[dict[dt.date, list[Rate]], str]:
    """Return ({date: [Rate, ...]}, source_tag) for the whole range."""
    session = requests.Session()
    session.headers.update({**BROWSER_HEADERS, "Accept": "application/json"})

    for tag, template in SERIES_TEMPLATES:
        if tag == "frankfurter-ecb" and require_official:
            log.info("Skipping ECB endpoint: --require-official is set")
            continue

        log.info("Trying %s", template.format(start=start, end=end))
        collected: dict[dt.date, dict[str, float]] = {}
        ok = True

        for base in BASES:
            # Year-sized chunks keep responses small and give useful progress.
            for chunk_start, chunk_end in _year_chunks(start, end):
                url = template.format(
                    start=chunk_start.isoformat(), end=chunk_end.isoformat()
                )
                joiner = "&" if "?" in url else "?"
                query = f"{url}{joiner}base={base}&symbols=INR"

                try:
                    response = session.get(query, timeout=45)
                    response.raise_for_status()
                    payload = response.json()
                except (requests.RequestException, ValueError) as exc:
                    log.warning("  %s failed: %s", query, exc)
                    ok = False
                    break

                rates = payload.get("rates") or {}
                for day_str, values in rates.items():
                    value = values.get("INR")
                    if value is None:
                        continue
                    try:
                        day = dt.date.fromisoformat(day_str)
                    except ValueError:
                        continue
                    # FBIL quotes JPY per 100 yen; Frankfurter quotes per 1.
                    scaled = float(value) * CURRENCY_UNITS.get(base, 1)
                    collected.setdefault(day, {})[base] = scaled

                log.info(
                    "  %s %s..%s -> %d days",
                    base, chunk_start, chunk_end, len(rates),
                )
                time.sleep(0.4)  # be a good citizen

            if not ok:
                break

        if ok and collected:
            by_date = {
                day: [make_rate(b, v, day, tag) for b, v in sorted(values.items())]
                for day, values in sorted(collected.items())
            }
            return by_date, tag

    raise SystemExit(
        "Backfill failed: no endpoint returned a usable time series. "
        "Check connectivity, or run without --require-official."
    )


def _year_chunks(
    start: dt.date, end: dt.date
) -> list[tuple[dt.date, dt.date]]:
    chunks: list[tuple[dt.date, dt.date]] = []
    cursor = start
    while cursor <= end:
        year_end = dt.date(cursor.year, 12, 31)
        chunks.append((cursor, min(year_end, end)))
        cursor = year_end + dt.timedelta(days=1)
    return chunks


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # The writers log one line per file. Across a full backfill that is >4,000
    # lines of noise that buries the progress counter and bloats the Actions
    # log. Progress is reported every 100 days instead.
    if args.log_level != "DEBUG":
        logging.getLogger("src.excel_writer").setLevel(logging.WARNING)
        logging.getLogger("src.pdf_writer").setLevel(logging.WARNING)

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end) if args.end else today_ist()
    if start > end:
        raise SystemExit(f"--start {start} is after --end {end}")

    log.info("Backfilling %s .. %s", start, end)
    by_date, tag = fetch_series(start, end, args.require_official)

    if not by_date:
        log.warning("No data returned for that range")
        return 1

    days = sorted(by_date)
    total = sum(len(v) for v in by_date.values())
    log.info(
        "Retrieved %d rates across %d business days (%s .. %s), source=%s",
        total, len(days), days[0], days[-1], tag,
    )
    if tag == "frankfurter-ecb":
        log.warning(
            "This is ECB data, NOT the official FBIL benchmark. Every file "
            "will be marked INDICATIVE."
        )

    if args.dry_run:
        log.info("--dry-run: no files written")
        for day in days[:5]:
            log.info("  %s  %s", day, [f"{r.pair}={r.value:.4f}" for r in by_date[day]])
        if len(days) > 5:
            log.info("  ... and %d more days", len(days) - 5)
        return 0

    output_dir = Path(args.output_dir)

    if not args.no_per_date:
        for index, day in enumerate(days, start=1):
            write_daily_workbook(by_date[day], day, output_dir / "excel")
            if not args.no_pdf:
                write_daily_pdf(by_date[day], day, output_dir / "pdf")
            if index % 100 == 0 or index == len(days):
                log.info("  wrote %d/%d days", index, len(days))

    # One merge for the whole range. Calling merge_into_master per-day would
    # re-read and re-write the workbook 2,000 times.
    master_path = output_dir / args.master_filename
    all_rates = [rate for day in days for rate in by_date[day]]
    _, added = merge_into_master(read_master(master_path), all_rates, master_path)
    log.info("Master history at %s (+%d rows)", master_path, added)

    log.info("Backfill complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
