"""Render one date's reference rates as a single-page PDF.

Deliberately kept to one page per business date, mirroring the daily xlsx, so
the two output folders line up file-for-file:

    data/excel/reference_rates_2026-08-07.xlsx
    data/pdf/reference_rates_2026-08-07.pdf

Note on ReportLab fonts: never put Unicode subscript/superscript characters in
here. The built-in Type 1 fonts have no glyphs for them and they render as
solid black boxes. The rupee sign is avoided for the same reason — Helvetica
predates it, so "INR" is spelled out instead.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .models import EXPECTED_PAIRS, Rate, sort_rates

log = logging.getLogger(__name__)

NAVY = colors.HexColor("#1F3864")
LIGHT = colors.HexColor("#EEF2F8")
GREY = colors.HexColor("#BFBFBF")

CURRENCY_NAMES = {
    "USD": "US Dollar",
    "EUR": "Euro",
    "GBP": "Pound Sterling",
    "JPY": "Japanese Yen",
}


def write_daily_pdf(
    rates: list[Rate], target_date: dt.date, output_dir: str | Path
) -> Path:
    """Write reference_rates_YYYY-MM-DD.pdf and return its path."""
    if not rates:
        raise ValueError("Refusing to write an empty PDF")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"reference_rates_{target_date.isoformat()}.pdf"

    ordered = sort_rates(rates)
    indicative = any(r.source.endswith("-ecb") for r in ordered)

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=22 * mm,
        rightMargin=22 * mm,
        topMargin=20 * mm,
        bottomMargin=18 * mm,
        title=f"Reference Rates {target_date.isoformat()}",
        author="FBIL / RBI reference rate pipeline",
        subject="Daily INR reference rates",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleNavy",
        parent=styles["Title"],
        textColor=NAVY,
        fontSize=18,
        spaceAfter=2,
    )
    subtitle_style = ParagraphStyle(
        "Subtitle",
        parent=styles["Normal"],
        fontSize=11,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#444444"),
        spaceAfter=14,
    )
    note_style = ParagraphStyle(
        "Note", parent=styles["Normal"], fontSize=8.5, leading=12,
        textColor=colors.HexColor("#555555"),
    )
    warn_style = ParagraphStyle(
        "Warn", parent=note_style, textColor=colors.HexColor("#9C2B2B"),
        fontName="Helvetica-Bold",
    )

    story: list = [
        Paragraph("FBIL / RBI Reference Rates", title_style),
        Paragraph(target_date.strftime("%A, %d %B %Y"), subtitle_style),
        _rate_table(ordered),
        Spacer(1, 16),
    ]

    if indicative:
        story.append(
            Paragraph(
                "INDICATIVE RATES — sourced from the ECB daily fixing because "
                "the FBIL and RBI publications were unavailable. These are "
                "close to, but not the same as, the official Indian benchmark. "
                "Do not use for accounting or contractual purposes.",
                warn_style,
            )
        )
        story.append(Spacer(1, 10))

    story.append(Paragraph(_provenance_text(ordered, target_date), note_style))

    doc.build(story)
    log.info("Wrote %s (%d rates)", path.name, len(ordered))
    return path


def _rate_table(rates: list[Rate]) -> Table:
    header = ["Currency Pair", "Currency", "Unit", "Rate (INR)"]
    rows = [header]
    for rate in rates:
        rows.append(
            [
                rate.pair,
                CURRENCY_NAMES.get(rate.base, rate.base),
                f"per {rate.unit}" if rate.unit != 1 else "per 1",
                f"{rate.value:,.4f}",
            ]
        )

    table = Table(rows, colWidths=[38 * mm, 46 * mm, 26 * mm, 36 * mm],
                  hAlign="CENTER")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, GREY),
        ("FONTNAME", (3, 1), (3, -1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 1), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
    ]
    # Zebra striping, applied after the header row.
    for index in range(1, len(rows)):
        if index % 2 == 1:
            style.append(("BACKGROUND", (0, index), (-1, index), LIGHT))
    table.setStyle(TableStyle(style))
    return table


def _provenance_text(rates: list[Rate], target_date: dt.date) -> str:
    sources = ", ".join(sorted({r.source for r in rates}))
    missing = [p for p in EXPECTED_PAIRS if p not in {r.pair for r in rates}]
    retrieved = max(r.retrieved_at for r in rates)

    lines = [
        f"<b>Rate date:</b> {target_date.isoformat()}",
        f"<b>Source:</b> {sources}",
        f"<b>Retrieved:</b> {retrieved.strftime('%Y-%m-%d %H:%M:%S')} UTC",
    ]
    if missing:
        lines.append(f"<b>Not published:</b> {', '.join(missing)}")
    lines.append(
        "JPY/INR is quoted per 100 yen, following FBIL convention. "
        "FBIL publishes reference rates at approximately 13:30 IST on every "
        "Mumbai business day."
    )
    return "<br/>".join(lines)
