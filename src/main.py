"""Entry point: scrape -> Excel -> Google Drive.

Exit codes (the workflow depends on these):
    0  success, OR a legitimate no-op (weekend, holiday, nothing published yet)
    1  a real failure — every source broke, or the upload failed

That split is the whole idempotency story. A holiday must look identical to a
green build, otherwise you get a red inbox every public holiday and stop
reading the alerts that matter.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from pathlib import Path

from .calendar_utils import (
    load_holidays,
    rates_probably_published,
    skip_reason,
    today_ist,
)
from .excel_writer import merge_into_master, read_master, write_daily_workbook
from .models import EXPECTED_PAIRS, NoDataForDate, Rate, SourceUnavailable
from .pdf_writer import write_daily_pdf
from .sources import DEFAULT_CHAIN, build_chain

log = logging.getLogger("rates")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOLIDAYS = PROJECT_ROOT / "config" / "holidays.yml"

EXIT_OK = 0
EXIT_FAIL = 1


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # These libraries are extremely chatty at DEBUG.
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rates",
        description="Scrape FBIL/RBI reference rates, build Excel, upload to Drive.",
    )
    parser.add_argument(
        "--date",
        help="Business date to fetch, YYYY-MM-DD. Defaults to today in IST.",
    )
    parser.add_argument(
        "--sources",
        default=os.environ.get("RATE_SOURCES", ",".join(DEFAULT_CHAIN)),
        help="Comma-separated source chain, tried in order.",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("OUTPUT_DIR", "output"),
        help="Where workbooks are written before upload.",
    )
    parser.add_argument(
        "--holidays",
        default=os.environ.get("HOLIDAY_FILE", str(DEFAULT_HOLIDAYS)),
        help="Path to holidays.yml.",
    )
    parser.add_argument(
        "--master-filename",
        default=os.environ.get("MASTER_FILENAME", "reference_rates_master.xlsx"),
        help="Name of the cumulative history workbook in Drive.",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Build the workbooks locally and skip Google Drive entirely.",
    )
    parser.add_argument(
        "--no-master",
        action="store_true",
        help="Skip the cumulative history workbook; write the daily file only.",
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="Skip the date-wise PDF; write the Excel files only.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore weekend/holiday gating and attempt the fetch anyway.",
    )
    parser.add_argument(
        "--require-official",
        action="store_true",
        help=(
            "Fail rather than fall back to indicative (ECB) rates. Use when the "
            "output feeds accounting or contractual work."
        ),
    )
    parser.add_argument(
        "--check-drive",
        action="store_true",
        help="Verify Drive credentials and folder access, then exit.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def resolve_date(raw: str | None) -> dt.date:
    if not raw:
        return today_ist()
    try:
        return dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"--date must be YYYY-MM-DD, got {raw!r}") from exc


def collect_rates(
    source_names: list[str], target_date: dt.date, require_official: bool
) -> list[Rate]:
    """Walk the source chain and return the first non-empty result.

    Distinguishes "this source is broken" (keep going, and complain loudly at
    the end) from "nobody published anything today" (a normal no-op).
    """
    chain = build_chain(source_names)
    broke: list[str] = []
    silent: list[str] = []

    for source in chain:
        if require_official:
            setattr(source, "allow_indicative", False)

        log.info("Trying source: %s (%s)", source.name, source.description)
        try:
            rates = source.fetch(target_date)
        except NoDataForDate as exc:
            log.info("  no data: %s", exc)
            silent.append(source.name)
            continue
        except SourceUnavailable as exc:
            log.warning("  unavailable: %s", exc)
            broke.append(source.name)
            continue
        except Exception as exc:  # noqa: BLE001 — one bad source must not kill the run
            log.exception("  unexpected error in %s: %s", source.name, exc)
            broke.append(source.name)
            continue

        if rates:
            missing = [p for p in EXPECTED_PAIRS if p not in {r.pair for r in rates}]
            if missing:
                log.warning("%s returned nothing for %s", source.name, missing)
            log.info("Got %d rates from %s", len(rates), source.name)
            return rates

        silent.append(source.name)

    if broke and not silent:
        # Everything we tried is down — that is an outage, not a holiday.
        raise SourceUnavailable(
            f"Every source failed for {target_date}: {', '.join(broke)}"
        )

    return []


def run(args: argparse.Namespace) -> int:
    target_date = resolve_date(args.date)
    holidays = load_holidays(args.holidays)

    log.info("=" * 66)
    log.info("Reference rates for %s (%s)", target_date, target_date.strftime("%A"))
    log.info("=" * 66)

    if args.check_drive:
        from .drive_client import client_from_env

        name = client_from_env().check_access()
        log.info("Drive OK — folder %r is reachable and writable", name)
        return EXIT_OK

    # --- calendar gate ----------------------------------------------------
    reason = skip_reason(target_date, holidays)
    if reason and not args.force:
        log.info("Skipping: %s. Nothing to do.", reason)
        return EXIT_OK
    if reason:
        log.warning("--force set; ignoring calendar gate (%s)", reason)

    # --- scrape -----------------------------------------------------------
    source_names = [s for s in args.sources.split(",") if s.strip()]
    try:
        rates = collect_rates(source_names, target_date, args.require_official)
    except SourceUnavailable as exc:
        log.error("%s", exc)
        return EXIT_FAIL
    except KeyError as exc:
        log.error("%s", exc)
        return EXIT_FAIL

    if not rates:
        if not rates_probably_published():
            log.info(
                "No rates for %s — FBIL publishes at ~13:30 IST and it is only "
                "%s IST now. Exiting cleanly.",
                target_date,
                dt.datetime.now().strftime("%H:%M"),
            )
        else:
            log.info(
                "No rates published for %s. Most likely an unlisted Mumbai bank "
                "holiday — consider adding it to %s. Exiting cleanly.",
                target_date,
                args.holidays,
            )
        return EXIT_OK

    # --- build the daily outputs -----------------------------------------
    # Excel and PDF go into sibling folders so each format can be synced,
    # shared or archived independently:
    #     data/excel/reference_rates_YYYY-MM-DD.xlsx
    #     data/pdf/reference_rates_YYYY-MM-DD.pdf
    output_dir = Path(args.output_dir)
    daily_path = write_daily_workbook(rates, target_date, output_dir / "excel")

    pdf_path = None
    if not args.no_pdf:
        pdf_path = write_daily_pdf(rates, target_date, output_dir / "pdf")

    if args.no_upload:
        log.info("--no-upload set; wrote %s", daily_path)
        if pdf_path:
            log.info("--no-upload set; wrote %s", pdf_path)
        if not args.no_master:
            local_master = output_dir / args.master_filename
            existing = read_master(local_master)
            merge_into_master(existing, rates, local_master)
            log.info("Local master updated: %s", local_master)
        return EXIT_OK

    # --- upload -----------------------------------------------------------
    from .drive_client import DriveError, client_from_env

    try:
        drive = client_from_env()
        folder_name = drive.check_access()
        log.info("Drive folder: %s", folder_name)

        drive.upload_or_replace(daily_path)
        if pdf_path:
            drive.upload_or_replace(pdf_path)

        if not args.no_master:
            local_master = output_dir / args.master_filename
            # Pull the current history down first so the merge is against
            # what is actually in Drive, not a stale local copy. Runners are
            # ephemeral, so there is never a usable local copy in CI.
            drive.download_if_exists(args.master_filename, local_master)
            existing = read_master(local_master)
            master_path, added = merge_into_master(existing, rates, local_master)
            if added or existing.empty:
                drive.upload_or_replace(master_path)
            else:
                log.info("Master history unchanged — skipping re-upload")
    except DriveError as exc:
        log.error("Drive step failed: %s", exc)
        return EXIT_FAIL

    log.info("Done.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    try:
        return run(args)
    except KeyboardInterrupt:
        log.warning("Interrupted")
        return EXIT_FAIL
    except Exception:  # noqa: BLE001
        log.exception("Unhandled error")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
