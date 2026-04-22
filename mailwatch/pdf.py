"""PDF rendering for USPS-compliant envelopes and Avery label sheets.

Backed by WeasyPrint + Jinja2 against HTML templates in
``mailwatch/templates/pdf/``. The templates embed the USPSIMBStandard
TrueType font (base64 ``@font-face``) and render the IMb by dropping a
65-character ``F``/``A``/``D``/``T`` string into a ``<span>`` with that
font — each glyph is the appropriate ascender / descender / full /
tracker bar.

Page geometry is driven by catalog dicts:

* :data:`mailwatch.layouts.ENVELOPES` — 11 USPS letter envelope sizes,
  each with block positions proven collision-free at module load.
* :data:`mailwatch.avery.AVERY` — 11 curated Avery parts covering
  shipping / address / return-address families in laser + inkjet SKUs.

The two public entry points are synchronous — FastAPI callers wrap
them in :func:`asyncio.to_thread`.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import BinaryIO, Literal, TypedDict

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

from mailwatch.avery import AVERY, DEFAULT_AVERY
from mailwatch.imb import encode as _imb_encode
from mailwatch.layouts import DEFAULT_ENVELOPE, ENVELOPES

_TEMPLATE_PACKAGE = "mailwatch.templates.pdf"


# --------------------------------------------------------------------------- #
# Public data shape                                                           #
# --------------------------------------------------------------------------- #


class LabelData(TypedDict):
    """Per-label payload for :func:`render_avery`.

    ``recipient`` is a list of pre-formatted address lines (rendered
    verbatim — UPPERCASE-conversion is caller responsibility if wanted
    for scanner friendliness).  ``tracking`` is the 20-digit IMb
    tracking string. ``routing`` is a 0/5/9/11-digit delivery-point ZIP
    code; empty string is valid.
    """

    recipient: list[str]
    tracking: str
    routing: str


AveryMode = Literal["single", "fill", "batch"]

_TRACKING_LEN = 20  # USPS-B-3200 IMb tracking code length


# --------------------------------------------------------------------------- #
# Jinja environment (cached)                                                  #
# --------------------------------------------------------------------------- #


def _templates_dir() -> Path:
    with resources.as_file(resources.files(_TEMPLATE_PACKAGE)) as path:
        return Path(path)


_jinja = Environment(
    loader=FileSystemLoader(_templates_dir()),
    autoescape=select_autoescape(["html"]),
    trim_blocks=False,
    lstrip_blocks=False,
)


# --------------------------------------------------------------------------- #
# Tracking + routing helpers                                                  #
# --------------------------------------------------------------------------- #


def _tracking_to_components(tracking: str) -> tuple[int, int, int, int]:
    """Decompose a 20-digit IMb tracking string into its spec fields.

    Per USPS-B-3200 the 20-digit tracking code layout is::

        BB TTT MMMMMMMMM SSSSSS    (9-digit MID, 6-digit serial)
        BB TTT MMMMMM    SSSSSSSSS (6-digit MID, 9-digit serial)

    MID length is determined by the first MID digit: **9-digit MIDs
    begin with ``9``**, 6-digit MIDs begin with ``0``-``8``. This
    matches the inverse invariant used by :func:`mailwatch.imb.encode`
    when rebuilding ``tracking`` from its component fields.
    """
    if len(tracking) != _TRACKING_LEN or not tracking.isdigit():
        raise ValueError(f"tracking must be 20 digits, got {tracking!r}")
    barcode_id = int(tracking[0:2])
    service_type = int(tracking[2:5])
    if tracking[5] == "9":
        mailer_id = int(tracking[5:14])
        serial = int(tracking[14:20])
    else:
        mailer_id = int(tracking[5:11])
        serial = int(tracking[11:20])
    return barcode_id, service_type, mailer_id, serial


def _encode_bars(tracking: str, routing: str) -> str:
    """Return the 65-char F/A/D/T string ready for the USPSIMBStandard font."""
    barcode_id, service_type, mailer_id, serial = _tracking_to_components(tracking)
    return _imb_encode(barcode_id, service_type, mailer_id, serial, routing)


def _human_readable(tracking: str, routing: str) -> str:
    """Format the human-readable digit line that sits below the bars."""
    return f"{tracking} {routing}".rstrip()


# --------------------------------------------------------------------------- #
# Envelope rendering                                                          #
# --------------------------------------------------------------------------- #


def render_envelope(
    sender: list[str],
    recipient: list[str],
    tracking: str,
    routing: str,
    out: BinaryIO,
    *,
    envelope_size: str = DEFAULT_ENVELOPE,
    human_readable: bool = True,
) -> None:
    """Render a single-envelope PDF to ``out``.

    Args:
        envelope_size: Key into :data:`mailwatch.layouts.ENVELOPES`.
            Defaults to ``#10``. Unknown keys raise :class:`KeyError`.
        human_readable: If True (default), draw the 20-digit tracking
            + routing ZIP as small text directly above the barcode bars.
            USPS doesn't require it — pass False to omit.
    """
    spec = ENVELOPES[envelope_size]
    html_str = _jinja.get_template("envelope.html").render(
        spec=spec,
        sender_lines=sender,
        recipient_lines=recipient,
        barcode_bars=_encode_bars(tracking, routing),
        human_readable=human_readable,
        human_readable_text=_human_readable(tracking, routing),
    )
    HTML(string=html_str, base_url=str(_templates_dir())).write_pdf(out)


# --------------------------------------------------------------------------- #
# Avery rendering                                                             #
# --------------------------------------------------------------------------- #


def _label_dict(
    label: LabelData,
    row: int,
    col: int,
    *,
    human_readable: bool,
) -> dict[str, object]:
    """Build a template-renderable dict for one label at (row, col)."""
    return {
        "row": row,
        "col": col,
        "barcode_bars": _encode_bars(label["tracking"], label["routing"]),
        "human_readable": human_readable,
        "human_readable_text": _human_readable(label["tracking"], label["routing"]),
        "recipient_lines": label["recipient"],
    }


def _assign_positions(
    items: list[LabelData],
    start_row: int,
    start_col: int,
    *,
    cols: int,
    rows: int,
    human_readable: bool,
) -> list[list[dict[str, object]]]:
    """Distribute ``items`` across sheet positions starting at (start_row, start_col).

    Returns a list of pages; each page is a list of label-dicts with
    their (row, col) fixed. Row-major within each page; overflow
    spills to additional pages starting at (1, 1).
    """
    pages: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    row, col = start_row, start_col
    for item in items:
        current.append(_label_dict(item, row, col, human_readable=human_readable))
        col += 1
        if col > cols:
            col = 1
            row += 1
        if row > rows:
            pages.append(current)
            current = []
            row, col = 1, 1
    if current:
        pages.append(current)
    return pages


def render_avery(
    labels_data: list[LabelData] | LabelData,
    out: BinaryIO,
    *,
    part: str = DEFAULT_AVERY,
    mode: AveryMode = "fill",
    start_row: int = 1,
    start_col: int = 1,
    human_readable: bool = True,
) -> None:
    """Render an Avery label sheet to ``out``.

    Args:
        labels_data: A single :class:`LabelData` (``mode="single"`` or
            ``"fill"``) or a list (``mode="batch"``).
        out: Binary sink for the rendered PDF bytes.
        part: Key into :data:`mailwatch.avery.AVERY` (default ``"8163"``).
        mode: How to distribute ``labels_data`` across sheet positions.

            * ``"single"`` — exactly one label at
              (``start_row``, ``start_col``). Matches upstream's
              one-label-per-PDF behaviour for partial-sheet reuse.
            * ``"fill"`` (default) — repeat the single label across
              every remaining slot starting at
              (``start_row``, ``start_col``).
            * ``"batch"`` — ``labels_data`` is a list of distinct
              labels, placed row-major starting at
              (``start_row``, ``start_col``), spilling to additional
              pages if there are more than ``slots_per_sheet - skip``
              entries.
        start_row: 1-indexed row on page 1 (1..tpl.rows).
        start_col: 1-indexed column within that row (1..tpl.cols).
        human_readable: If True (default), draw the 20-digit tracking
            + routing ZIP as text directly below the barcode on each
            label. Pass False to show bars only.
    """
    tpl = AVERY[part]
    if not 1 <= start_row <= tpl.rows:
        raise ValueError(f"start_row must be 1..{tpl.rows}, got {start_row}")
    if not 1 <= start_col <= tpl.cols:
        raise ValueError(f"start_col must be 1..{tpl.cols}, got {start_col}")

    if mode == "batch":
        if not isinstance(labels_data, list):
            raise ValueError("mode='batch' requires a list of LabelData")
        items: list[LabelData] = labels_data
    else:
        if isinstance(labels_data, list):
            if len(labels_data) != 1:
                raise ValueError(
                    f"mode={mode!r} requires a single LabelData (got {len(labels_data)})"
                )
            single: LabelData = labels_data[0]
        else:
            single = labels_data
        if mode == "single":
            items = [single]
        else:  # "fill"
            skip = (start_row - 1) * tpl.cols + (start_col - 1)
            items = [single] * max(0, tpl.slots_per_sheet - skip)

    pages = _assign_positions(
        items,
        start_row,
        start_col,
        cols=tpl.cols,
        rows=tpl.rows,
        human_readable=human_readable,
    )
    html_str = _jinja.get_template("avery.html").render(tpl=tpl, pages=pages)
    HTML(string=html_str, base_url=str(_templates_dir())).write_pdf(out)
