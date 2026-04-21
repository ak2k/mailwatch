"""PDF rendering for USPS-compliant #10 envelopes and Avery 8163 label sheets.

Two public entry points, both synchronous — callers that live in an async
context are expected to wrap them in :func:`asyncio.to_thread`:

- :func:`render_envelope` — single #10 business envelope (9.5" x 4.125")
  with USPS-B-3200 / DMM 202 compliant layout: sender block top-left,
  recipient block inside the OCR Read Area, 4-state IMb barcode at the
  bottom-right corner and optional human-readable tracking-plus-routing
  line directly above the bars.
- :func:`render_avery8163` — 2 x 5 grid of 4" x 2" shipping labels on US
  Letter using ``pylabels2`` (imported as :mod:`pylabels`, despite the
  PyPI distribution name). Supports partial-page starts (``start_row`` /
  ``start_col``) for resuming from a sheet that already had labels peeled
  off.

Notes on dependency quirks:

* The PyPI distribution is ``pylabels2`` but it installs as the ``pylabels``
  top-level module. This is a known GPL-continuation fork of the original
  ``pylabels`` package.
* :class:`~reportlab.graphics.barcode.usps4s.USPS_4State` is a
  :class:`~reportlab.platypus.flowables.Flowable`, not a graphics
  :class:`~reportlab.graphics.shapes.Drawing`. Its :meth:`draw` method
  calls ``self.canv.rect(...)`` for each of the 65 bars. To embed the
  barcode in a pylabels drawing (which gives us a ``Drawing`` object, not
  a canvas) we provide a minimal "shape recorder" shim that records each
  ``rect`` call as a :class:`~reportlab.graphics.shapes.Rect` in a
  :class:`~reportlab.graphics.shapes.Group`. This lets us compose the
  barcode into the Avery label drawing without touching a real canvas.
"""

from __future__ import annotations

from typing import Any, BinaryIO, TypedDict

import pylabels
from reportlab.graphics import shapes
from reportlab.graphics.barcode.usps4s import USPS_4State
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as _canvas

# ---------------------------------------------------------------------------
# Page + element geometry (USPS-B-3200 / DMM 202)
# ---------------------------------------------------------------------------

# #10 business envelope: 9.5" x 4.125".
_ENV_W: float = 9.5 * inch
_ENV_H: float = 4.125 * inch

# Return address block (sender): Helvetica 9pt, 11pt leading.
_RETURN_X: float = 0.5 * inch
_RETURN_TOP_Y: float = _ENV_H - 0.5 * inch
_RETURN_FONT = "Helvetica"
_RETURN_FONT_SIZE: float = 9.0
_RETURN_LEADING: float = 11.0

# Delivery address block (recipient): Helvetica 11pt, 13pt leading.
# Must sit inside OCR Read Area: X in [0.5", 9.0"], Y in [0.625", 3.375"].
_DELIVERY_X: float = 3.5 * inch
_DELIVERY_TOP_Y: float = 2.5 * inch
_DELIVERY_FONT = "Helvetica"
_DELIVERY_FONT_SIZE: float = 11.0
_DELIVERY_LEADING: float = 13.0

# IMb barcode anchor (bottom-left corner of the barcode on the envelope).
# USPS_4State's defaults are spec-compliant: barWidth=0.02", barHeight=0.165",
# horizontalClearZone=0.125", verticalClearZone=0.028".
_IMB_X: float = 5.75 * inch
_IMB_Y: float = 0.3125 * inch

# Human-readable text directly above the bars (when human_readable=True).
# Positioned at: barcode baseline + vertical clear zone + bar height + 3pt gap.
# USPS_4State default barHeight is 0.165" = 11.88pt; vcz is 0.028" = 2.016pt.
# -> 0.3125" + 0.028" + 0.165" + a tiny gap. The plan gives ~0.4875" empirically.
_HR_Y: float = 0.4875 * inch
_HR_FONT = "Helvetica"
_HR_FONT_SIZE: float = 6.0

# Avery 8163 (4" x 2" labels, 2 cols x 5 rows on US Letter).
# All pylabels dimensions are in millimetres.
_AVERY_COLS = 2
_AVERY_ROWS = 5
_AVERY_SPEC = pylabels.Specification(
    sheet_width=215.9,
    sheet_height=279.4,  # 8.5" x 11" (mm)
    columns=_AVERY_COLS,
    rows=_AVERY_ROWS,
    label_width=101.6,
    label_height=50.8,  # 4" x 2" (mm)
    # Vertical: 5 * 50.8 = 254mm used; 279.4 - 254 = 25.4mm split 12.7 top / 12.7 bottom.
    top_margin=12.7,
    bottom_margin=12.7,  # 0.5" each
    # Horizontal: 2 * 101.6 = 203.2mm used; 215.9 - 203.2 = 12.7mm split 4.7625/4.7625/3.175.
    left_margin=4.7625,
    right_margin=4.7625,  # 0.1875" each
    column_gap=3.175,  # 0.125"
    row_gap=0,
    corner_radius=1.5,
)

# Inside a label drawing_callable, pylabels hands us dimensions in points.
# 2mm padding = ~5.67 pt; 10pt Helvetica recipient text.
_LABEL_PAD: float = 2.0 * 2.83465  # 2mm in pt
_LABEL_RECIP_FONT = "Helvetica"
_LABEL_RECIP_FONT_SIZE: float = 10.0
_LABEL_RECIP_LEADING: float = 12.0
_LABEL_HR_FONT = "Helvetica"
_LABEL_HR_FONT_SIZE: float = 6.0


# ---------------------------------------------------------------------------
# Public data shape
# ---------------------------------------------------------------------------


class LabelData(TypedDict):
    """Per-label payload for :func:`render_avery8163`.

    ``recipient`` is a list of pre-formatted address lines (UPPERCASE for
    scanner readability is applied internally). ``tracking`` is the 20-digit
    IMb tracking string (Barcode ID + STID + Mailer ID + Serial). ``routing``
    is a 0, 5, 9, or 11-digit delivery-point ZIP; empty string is valid.
    """

    recipient: list[str]
    tracking: str
    routing: str


# ---------------------------------------------------------------------------
# Envelope rendering
# ---------------------------------------------------------------------------


def render_envelope(
    sender: list[str],
    recipient: list[str],
    tracking: str,
    routing: str,
    out: BinaryIO,
    *,
    human_readable: bool = True,
) -> None:
    """Render one #10 envelope (9.5" x 4.125") to ``out``.

    Args:
        sender: Return-address block as a list of pre-formatted lines
            (rendered uppercase, 9pt Helvetica, top-left origin).
        recipient: Delivery-address block as a list of pre-formatted lines
            (rendered uppercase, 11pt Helvetica, positioned inside the USPS
            OCR Read Area).
        tracking: 20-digit IMb tracking string (Barcode ID + STID +
            Mailer ID + Serial).
        routing: 0, 5, 9, or 11 digit destination ZIP/ZIP+4/DPC. Empty
            string is valid (the barcode is still drawn, without routing
            digits).
        out: Binary sink for the rendered PDF bytes.
        human_readable: If True (default), draw the tracking + routing
            string in 6pt Helvetica directly above the barcode bars. USPS
            does not require the human-readable line; set False to omit it.
    """
    c = _canvas.Canvas(out, pagesize=(_ENV_W, _ENV_H))

    _draw_address_block(
        c,
        sender,
        x=_RETURN_X,
        top_y=_RETURN_TOP_Y,
        font=_RETURN_FONT,
        size=_RETURN_FONT_SIZE,
        leading=_RETURN_LEADING,
    )
    _draw_address_block(
        c,
        recipient,
        x=_DELIVERY_X,
        top_y=_DELIVERY_TOP_Y,
        font=_DELIVERY_FONT,
        size=_DELIVERY_FONT_SIZE,
        leading=_DELIVERY_LEADING,
    )

    # Pure K (0,0,0) for scanner contrast per USPS-B-3200.
    c.setFillColorRGB(0, 0, 0)
    barcode = USPS_4State(value=tracking, routing=routing)
    barcode.drawOn(c, _IMB_X, _IMB_Y)

    if human_readable:
        c.setFont(_HR_FONT, _HR_FONT_SIZE)
        text = f"{tracking} {routing}".rstrip()
        c.drawString(_IMB_X, _HR_Y, text)

    c.showPage()
    c.save()


def _draw_address_block(
    c: _canvas.Canvas,
    lines: list[str],
    *,
    x: float,
    top_y: float,
    font: str,
    size: float,
    leading: float,
) -> None:
    """Draw an UPPERCASE address block descending from ``top_y`` at ``x``."""
    c.setFont(font, size)
    c.setFillColorRGB(0, 0, 0)
    y = top_y
    for line in lines:
        c.drawString(x, y, line.upper())
        y -= leading


# ---------------------------------------------------------------------------
# Avery 8163 label-sheet rendering
# ---------------------------------------------------------------------------


class _ShapeRecorderCanvas:
    """Minimal canvas shim that records ``rect`` calls into a shapes Group.

    :class:`USPS_4State` (a :class:`~reportlab.platypus.flowables.Flowable`)
    calls ``self.canv.rect(x, y, w, h, stroke=..., fill=...)`` for each of
    the 65 bars. pylabels hands our drawing_callable a
    :class:`~reportlab.graphics.shapes.Drawing` — not a canvas — so we
    provide this shim as ``barcode.canv`` to capture the bars as
    :class:`~reportlab.graphics.shapes.Rect` objects instead.
    """

    def __init__(self) -> None:
        self.group: shapes.Group = shapes.Group()
        self._fill: Any = colors.black
        self._stroke: Any = colors.black

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        stroke: int = 0,
        fill: int = 1,
    ) -> None:
        r = shapes.Rect(x, y, w, h)
        r.fillColor = self._fill if fill else None
        r.strokeColor = self._stroke if stroke else None
        r.strokeWidth = 0
        self.group.add(r)

    def setFillColor(self, c: Any) -> None:  # noqa: N802 (reportlab convention)
        self._fill = c

    def setStrokeColor(self, c: Any) -> None:  # noqa: N802
        self._stroke = c

    def setFillColorRGB(self, r: float, g: float, b: float) -> None:  # noqa: N802
        self._fill = colors.Color(r, g, b)


def _barcode_group(tracking: str, routing: str) -> tuple[shapes.Group, float, float]:
    """Render a USPS_4State barcode into a shapes Group.

    Returns the (group, width, height) in points. The group's local origin
    is (0, 0) at the bottom-left of the barcode's bounding box.
    """
    recorder = _ShapeRecorderCanvas()
    barcode = USPS_4State(value=tracking, routing=routing)
    barcode.canv = recorder
    # USPS_4State.drawHumanReadable() also calls self.canv — we draw our own
    # human-readable line below, so stub it out here.
    barcode.drawHumanReadable = lambda: None
    barcode.draw()
    return recorder.group, float(barcode.width), float(barcode.height)


def _draw_label(
    label: shapes.Drawing,
    width: float,
    height: float,
    obj: LabelData,
) -> None:
    """pylabels drawing_callable — draws one label's contents.

    The ``label`` drawing's origin is (0, 0) at the label's bottom-left;
    ``width`` and ``height`` are the label's drawable area in points.
    """
    # Recipient address block (top-left, descending).
    text_x = _LABEL_PAD
    text_top = height - _LABEL_PAD
    y = text_top - _LABEL_RECIP_FONT_SIZE
    for line in obj["recipient"]:
        s = shapes.String(text_x, y, line.upper())
        s.fontName = _LABEL_RECIP_FONT
        s.fontSize = _LABEL_RECIP_FONT_SIZE
        s.fillColor = colors.black
        label.add(s)
        y -= _LABEL_RECIP_LEADING

    # Barcode (bottom-right).
    group, bc_w, bc_h = _barcode_group(obj["tracking"], obj["routing"])
    bc_x = width - _LABEL_PAD - bc_w
    bc_y = _LABEL_PAD + _LABEL_HR_FONT_SIZE + 2.0  # leave room for HR text below
    group.translate(bc_x, bc_y)
    label.add(group)

    # Human-readable tracking text below barcode.
    hr_text = f"{obj['tracking']} {obj['routing']}".rstrip()
    hr_width = stringWidth(hr_text, _LABEL_HR_FONT, _LABEL_HR_FONT_SIZE)
    hr = shapes.String(bc_x + bc_w - hr_width, _LABEL_PAD, hr_text)
    hr.fontName = _LABEL_HR_FONT
    hr.fontSize = _LABEL_HR_FONT_SIZE
    hr.fillColor = colors.black
    label.add(hr)


def render_avery8163(
    labels_data: list[LabelData],
    out: BinaryIO,
    *,
    start_row: int = 1,
    start_col: int = 1,
) -> None:
    """Render an Avery 8163 sheet (2 x 5 grid of 4" x 2" labels) to ``out``.

    Args:
        labels_data: Per-label payloads. Renders one label per entry, in
            row-major order starting at (``start_row``, ``start_col``).
        out: Binary sink for the rendered PDF bytes.
        start_row: 1-indexed row on page 1 to start at (skips earlier rows).
        start_col: 1-indexed column within ``start_row`` to start at.

    The first ``(start_row - 1) * 2 + (start_col - 1)`` label positions on
    page 1 are marked as used (so we can resume printing on a partially
    consumed sheet).
    """
    if not 1 <= start_row <= _AVERY_ROWS:
        raise ValueError(f"start_row must be in 1..{_AVERY_ROWS} (got {start_row})")
    if not 1 <= start_col <= _AVERY_COLS:
        raise ValueError(f"start_col must be in 1..{_AVERY_COLS} (got {start_col})")

    sheet = pylabels.Sheet(_AVERY_SPEC, _draw_label, border=False)

    skip_count = (start_row - 1) * _AVERY_COLS + (start_col - 1)
    if skip_count > 0:
        used: list[tuple[int, int]] = []
        for i in range(skip_count):
            r = i // _AVERY_COLS + 1
            col = i % _AVERY_COLS + 1
            used.append((r, col))
        sheet.partial_page(1, used)

    for item in labels_data:
        sheet.add_label(item)

    sheet.save(out)
