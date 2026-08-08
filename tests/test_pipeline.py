"""Offline tests — no network, no Google credentials.

These cover the parts that actually break in production: HTML parsing against
a realistic table, the plausibility guard that stops junk numbers becoming
"rates", holiday gating, and the master-history dedupe.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import calendar_utils, excel_writer  # noqa: E402
from src.models import NoDataForDate, SourceUnavailable, make_rate  # noqa: E402
from src.sources import build_chain  # noqa: E402
from src.sources.base import RateSource  # noqa: E402
from src.sources.html_parsing import (  # noqa: E402
    extract_aspnet_state,
    extract_rate_date,
    extract_rates_from_html,
    identify_currency,
)

TODAY = dt.date(2026, 8, 7)  # a Friday

FBIL_LIKE_HTML = """
<html><body>
  <div class="nav">Call 1800 200 3000 for support</div>
  <h3>FBIL Reference Rate as on 07 Aug 2026</h3>
  <table>
    <tr><th>Currency</th><th>Rate</th></tr>
    <tr><td>USD</td><td>87.4521</td></tr>
    <tr><td>EUR</td><td>101.2233</td></tr>
    <tr><td>GBP</td><td>117.8090</td></tr>
    <tr><td>JPY</td><td>59.3100</td></tr>
  </table>
  <input type="hidden" name="__VIEWSTATE" value="/wEPDwUKMTIz" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" value="CA0B0334" />
  <footer>Page rendered in 0.42 seconds. Visitor count 1948372</footer>
</body></html>
"""


# --------------------------------------------------------------------------
# HTML parsing
# --------------------------------------------------------------------------


def test_extracts_all_four_pairs():
    values = extract_rates_from_html(FBIL_LIKE_HTML)
    assert values == {
        "USD": 87.4521,
        "EUR": 101.2233,
        "GBP": 117.8090,
        "JPY": 59.3100,
    }


def test_extracts_the_rate_date():
    assert extract_rate_date(FBIL_LIKE_HTML) == TODAY


def test_ignores_numbers_outside_the_plausible_band():
    """A visitor counter or phone number must never be read as a USD rate."""
    html = """
    <table>
      <tr><td>USD</td><td>1800200300</td><td>0.0001</td></tr>
    </table>
    """
    assert extract_rates_from_html(html) == {}


def test_picks_the_plausible_number_when_the_row_has_several():
    html = "<table><tr><td>USD</td><td>1</td><td>87.4521</td></tr></table>"
    assert extract_rates_from_html(html) == {"USD": 87.4521}


def test_currency_identification_prefers_the_specific_match():
    assert identify_currency("US DOLLAR") == "USD"
    assert identify_currency("Pound Sterling") == "GBP"
    assert identify_currency("Japanese Yen (per 100)") == "JPY"
    assert identify_currency("Some unrelated heading") is None


def test_reads_viewstate_for_postbacks():
    state = extract_aspnet_state(FBIL_LIKE_HTML)
    assert state["__VIEWSTATE"] == "/wEPDwUKMTIz"
    assert state["__VIEWSTATEGENERATOR"] == "CA0B0334"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Reference Rate as on 07 Aug 2026", dt.date(2026, 8, 7)),
        ("Date: 07/08/2026", dt.date(2026, 8, 7)),
        ("as on 7th August 2026", dt.date(2026, 8, 7)),
        ("2026-08-07", dt.date(2026, 8, 7)),
    ],
)
def test_date_formats(text, expected):
    assert extract_rate_date(f"<p>{text}</p>") == expected


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------


def test_weekends_are_skipped():
    assert calendar_utils.skip_reason(dt.date(2026, 8, 8), {}) is not None  # Sat
    assert calendar_utils.skip_reason(dt.date(2026, 8, 9), {}) is not None  # Sun
    assert calendar_utils.skip_reason(TODAY, {}) is None  # Fri


def test_holidays_are_skipped():
    # 26 Jan 2026 is a Monday, so a skip here can only come from the holiday
    # list — not from the weekend rule.
    republic_day = dt.date(2026, 1, 26)
    assert republic_day.weekday() < 5
    reason = calendar_utils.skip_reason(republic_day, {republic_day: "Republic Day"})
    assert reason is not None and "Republic Day" in reason


def test_holiday_file_loads(tmp_path):
    path = tmp_path / "h.yml"
    path.write_text('2026:\n  "2026-01-26": "Republic Day"\n', encoding="utf-8")
    assert calendar_utils.load_holidays(path) == {
        dt.date(2026, 1, 26): "Republic Day"
    }


def test_missing_holiday_file_is_not_fatal(tmp_path):
    assert calendar_utils.load_holidays(tmp_path / "nope.yml") == {}


def test_previous_business_day_hops_the_weekend():
    # Monday 10 Aug 2026 -> Friday 7 Aug 2026
    assert calendar_utils.previous_business_day(dt.date(2026, 8, 10), {}) == TODAY


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------


def sample_rates(day=TODAY, source="fbil"):
    return [
        make_rate("USD", 87.4521, day, source),
        make_rate("EUR", 101.2233, day, source),
        make_rate("GBP", 117.8090, day, source),
        make_rate("JPY", 59.3100, day, source),
    ]


def test_jpy_carries_the_per_100_unit():
    jpy = [r for r in sample_rates() if r.base == "JPY"][0]
    assert jpy.unit == 100
    usd = [r for r in sample_rates() if r.base == "USD"][0]
    assert usd.unit == 1


def test_daily_workbook_is_named_and_readable(tmp_path):
    path = excel_writer.write_daily_workbook(sample_rates(), TODAY, tmp_path)
    assert path.name == "reference_rates_2026-08-07.xlsx"

    frame = pd.read_excel(path, sheet_name="Reference Rates", skiprows=2)
    assert len(frame) == 4
    assert list(frame["Pair"]) == ["USD/INR", "EUR/INR", "GBP/INR", "JPY/INR"]
    assert frame.loc[0, "Rate (INR)"] == pytest.approx(87.4521)

    meta = pd.read_excel(path, sheet_name="Metadata")
    assert "Official FBIL reference rate" in meta["Value"].astype(str).to_list()


def test_empty_workbook_is_refused(tmp_path):
    with pytest.raises(ValueError):
        excel_writer.write_daily_workbook([], TODAY, tmp_path)


def test_master_merge_appends_new_dates(tmp_path):
    master = tmp_path / "master.xlsx"

    _, added = excel_writer.merge_into_master(
        excel_writer.read_master(master), sample_rates(TODAY), master
    )
    assert added == 4

    monday = dt.date(2026, 8, 10)
    _, added = excel_writer.merge_into_master(
        excel_writer.read_master(master), sample_rates(monday), master
    )
    assert added == 4

    history = pd.read_excel(master, sheet_name="History")
    assert len(history) == 8
    assert history["Date"].nunique() == 2


def test_master_merge_is_idempotent(tmp_path):
    """Re-running the same day must overwrite, not duplicate."""
    master = tmp_path / "master.xlsx"
    excel_writer.merge_into_master(
        excel_writer.read_master(master), sample_rates(TODAY), master
    )
    _, added = excel_writer.merge_into_master(
        excel_writer.read_master(master), sample_rates(TODAY), master
    )
    assert added == 0
    assert len(pd.read_excel(master, sheet_name="History")) == 4


def test_master_rerun_corrects_a_stale_value(tmp_path):
    master = tmp_path / "master.xlsx"
    excel_writer.merge_into_master(
        excel_writer.read_master(master), sample_rates(TODAY), master
    )
    corrected = [make_rate("USD", 99.9999, TODAY, "rbi")]
    excel_writer.merge_into_master(
        excel_writer.read_master(master), corrected, master
    )

    history = pd.read_excel(master, sheet_name="History")
    usd = history[history["Pair"] == "USD/INR"]
    assert len(usd) == 1
    assert usd.iloc[0]["Rate (INR)"] == pytest.approx(99.9999)
    assert usd.iloc[0]["Source"] == "rbi"


def test_wide_sheet_has_one_row_per_date(tmp_path):
    master = tmp_path / "master.xlsx"
    excel_writer.merge_into_master(
        excel_writer.read_master(master), sample_rates(TODAY), master
    )
    wide = pd.read_excel(master, sheet_name="Wide")
    assert len(wide) == 1
    assert "USD/INR" in wide.columns


# --------------------------------------------------------------------------
# Source chain / orchestration
# --------------------------------------------------------------------------


class BrokenSource(RateSource):
    name = "broken"

    def fetch(self, target_date):
        raise SourceUnavailable("simulated outage")


class SilentSource(RateSource):
    name = "silent"

    def fetch(self, target_date):
        raise NoDataForDate("holiday")


class GoodSource(RateSource):
    name = "good"

    def fetch(self, target_date):
        return sample_rates(target_date, source="good")


def test_chain_falls_through_to_the_working_source(monkeypatch):
    from src import main, sources

    monkeypatch.setitem(sources.REGISTRY, "broken", BrokenSource)
    monkeypatch.setitem(sources.REGISTRY, "good", GoodSource)

    rates = main.collect_rates(["broken", "good"], TODAY, require_official=False)
    assert len(rates) == 4
    assert rates[0].source == "good"


def test_total_outage_raises(monkeypatch):
    from src import main, sources

    monkeypatch.setitem(sources.REGISTRY, "broken", BrokenSource)
    with pytest.raises(SourceUnavailable):
        main.collect_rates(["broken"], TODAY, require_official=False)


def test_holiday_across_all_sources_is_not_an_outage(monkeypatch):
    """Every source saying 'nothing published' must exit clean, not red."""
    from src import main, sources

    monkeypatch.setitem(sources.REGISTRY, "silent", SilentSource)
    assert main.collect_rates(["silent"], TODAY, require_official=False) == []


def test_unknown_source_name_is_rejected():
    with pytest.raises(KeyError):
        build_chain(["definitely-not-a-source"])


# --------------------------------------------------------------------------
# End-to-end exit codes
# --------------------------------------------------------------------------


def test_weekend_run_exits_zero_without_touching_the_network(tmp_path):
    from src import main

    code = main.main(
        [
            "--date", "2026-08-08",  # Saturday
            "--output-dir", str(tmp_path),
            "--no-upload",
        ]
    )
    assert code == 0
    assert not list(tmp_path.glob("*.xlsx"))


def test_happy_path_writes_both_workbooks(monkeypatch, tmp_path):
    from src import main, sources

    monkeypatch.setitem(sources.REGISTRY, "good", GoodSource)
    code = main.main(
        [
            "--date", TODAY.isoformat(),
            "--sources", "good",
            "--output-dir", str(tmp_path),
            "--no-upload",
        ]
    )
    assert code == 0
    assert (tmp_path / "reference_rates_2026-08-07.xlsx").exists()
    assert (tmp_path / "reference_rates_master.xlsx").exists()
