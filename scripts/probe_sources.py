#!/usr/bin/env python3
"""Diagnostic: show exactly what each source returns, and why parsing failed.

Run this first, and run it again whenever the daily job starts returning
nothing. It is the fastest way to tell the three failure modes apart:

    * the site is unreachable from here (network / geo-block / WAF)
    * the page loads but the markup changed (rates found: {} )
    * the page loads fine and simply has no rate for that date (holiday)

    python scripts/probe_sources.py
    python scripts/probe_sources.py --date 2026-08-07 --dump-html
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.calendar_utils import today_ist  # noqa: E402
from src.models import NoDataForDate, SourceUnavailable  # noqa: E402
from src.sources import DEFAULT_CHAIN, build_chain  # noqa: E402
from src.sources.html_parsing import (  # noqa: E402
    extract_rate_date,
    extract_rates_from_html,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="YYYY-MM-DD, defaults to today IST")
    parser.add_argument("--sources", default=",".join(DEFAULT_CHAIN))
    parser.add_argument(
        "--dump-html",
        action="store_true",
        help="Save each fetched page to debug/ for inspection.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)-7s %(name)s  %(message)s",
        stream=sys.stdout,
    )

    target = dt.date.fromisoformat(args.date) if args.date else today_ist()
    print(f"\nProbing for {target} ({target:%A})\n" + "=" * 60)

    debug_dir = Path("debug")
    if args.dump_html:
        debug_dir.mkdir(exist_ok=True)

    try:
        chain = build_chain(args.sources.split(","))
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}")
        return 2

    worked = 0
    for source in chain:
        print(f"\n--- {source.name}: {source.description}")

        # Show the raw page separately from fetch(), so a parsing failure is
        # visibly different from a connection failure.
        if args.dump_html and hasattr(source, "__module__"):
            for url in _urls_for(source):
                try:
                    html = source.get(url).text
                except SourceUnavailable as exc:
                    print(f"    UNREACHABLE {url}\n      {exc}")
                    continue
                path = debug_dir / f"{source.name}_{_slug(url)}.html"
                path.write_text(html, encoding="utf-8")
                print(f"    saved {path}  ({len(html):,} bytes)")
                print(f"    rates found: {extract_rates_from_html(html)}")
                print(f"    date found:  {extract_rate_date(html)}")

        try:
            rates = source.fetch(target)
        except NoDataForDate as exc:
            print(f"    NO DATA  {exc}")
            continue
        except SourceUnavailable as exc:
            print(f"    FAILED   {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"    ERROR    {type(exc).__name__}: {exc}")
            continue

        if not rates:
            print("    returned nothing (date mismatch — see logs above)")
            continue

        worked += 1
        for rate in rates:
            unit = f"per {rate.unit} " if rate.unit != 1 else ""
            print(f"    {rate.pair:<9} {rate.value:>12.4f}  ({unit}{rate.source})")

    print("\n" + "=" * 60)
    print(f"{worked} of the configured sources returned data for {target}.")
    if not worked:
        print(
            "\nIf this is a Mumbai business day after 13:30 IST, re-run with\n"
            "--dump-html and inspect debug/*.html. A large file with an empty\n"
            "'rates found' dict means the markup moved; see the notes in\n"
            "src/sources/fbil.py."
        )
    return 0 if worked else 1


def _urls_for(source) -> tuple[str, ...]:
    from src.sources import fbil, rbi

    if source.name == "fbil":
        return fbil.FBIL_URLS
    if source.name == "rbi":
        return rbi.RBI_MODERN_URLS + (rbi.RBI_LEGACY_ARCHIVE,)
    return ()


def _slug(url: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in url)[-60:]


if __name__ == "__main__":
    sys.exit(main())
