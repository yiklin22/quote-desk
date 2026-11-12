"""Render an approved quote as a PDF."""
from __future__ import annotations

import io
from datetime import timedelta

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .domain import FIELD_LABELS, PriceList, Priced, Requirements


def _n(x) -> str:
    return f"{x.normalize():f}" if x == x.to_integral() else f"{x:f}"


def render_quote_pdf(number: str, pl: PriceList, price_list_version: int, req: Requirements, priced: Priced,
                     priced_on, approved_by: str) -> bytes:
    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                            bottomMargin=18 * mm, title=f"Quote {number}", invariant=1)
    story = [Paragraph(pl.business_name, styles["Title"]),
             Paragraph(f"Quote {number}", styles["Heading2"]),
             Paragraph(f"Date: {priced_on.isoformat()} &nbsp;&nbsp; Valid until: "
                       f"{(priced_on + timedelta(days=pl.quote_valid_days)).isoformat()}", styles["Normal"])]
    who = " ".join(x for x in (req.text("customer_name"), req.text("customer_email")) if x)
    if who:
        story.append(Paragraph(f"Prepared for: {who}", styles["Normal"]))
    story.append(Spacer(1, 8 * mm))

    rows = [["Description", "Qty", "Unit price", "Amount"]]
    for ln in priced.lines:
        qty = "" if ln.kind in ("surcharge", "discount") else _n(ln.quantity)
        unit = "" if ln.kind in ("surcharge", "discount") else f"{ln.unit_price:,.2f}"
        rows.append([Paragraph(ln.description, styles["Normal"]), qty, unit, f"{ln.amount:,.2f}"])
    rows += [["", "", "Total before GST", f"{priced.total_ex_gst:,.2f}"],
             ["", "", f"GST {pl.gst_rate * 100:.0f}%", f"{priced.gst:,.2f}"],
             ["", "", f"Total {pl.currency}", f"{priced.total:,.2f}"]]
    table = Table(rows, colWidths=[92 * mm, 16 * mm, 34 * mm, 28 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEABOVE", (2, -3), (-1, -3), 0.5, colors.grey), ("FONTNAME", (2, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (2, -1), (-1, -1), 0.8, colors.black), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story += [table, Spacer(1, 8 * mm), Paragraph("This quote is based on", styles["Heading4"])]
    for fld in ("area_sqm", "bedrooms", "bathrooms", "carpet_rooms", "windows", "frequency", "weekend"):
        if (v := req.text(fld)) is not None:
            story.append(Paragraph(f"{FIELD_LABELS[fld]}: {v.replace('_', ' ')}", styles["Normal"]))
    if priced.excluded:
        story += [Spacer(1, 4 * mm), Paragraph("Not included", styles["Heading4"]),
                  Paragraph(", ".join(priced.excluded) + " (not a service we offer).", styles["Normal"])]
    story += [Spacer(1, 8 * mm),
              Paragraph(f"Prices from price list version {price_list_version}. Approved by {approved_by}. "
                        f"If the details above are different on the day, the price may change.", styles["Italic"])]
    doc.build(story)
    return buf.getvalue()
