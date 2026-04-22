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


AveryMode = Literal["single", "fill"]

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


def _positions_for_single_label(
    label: LabelData,
    start_row: int,
    start_col: int,
    count: int,
    *,
    cols: int,
    human_readable: bool,
) -> list[dict[str, object]]:
    """Place the same ``label`` into ``count`` sequential slots starting at (start_row, start_col).

    Row-major; caller guarantees ``count`` fits on one page.
    """
    out: list[dict[str, object]] = []
    row, col = start_row, start_col
    for _ in range(count):
        out.append(_label_dict(label, row, col, human_readable=human_readable))
        col += 1
        if col > cols:
            col = 1
            row += 1
    return out


def render_avery(
    label: LabelData,
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
        label: The single :class:`LabelData` to place. mailwatch's product
            flow is one-mail-piece-at-a-time — a multi-label "batch" mode
            would require a UI that collects several recipients up front,
            which the product doesn't have today. Keeping this narrow
            avoids the dead-code liability the reviewers flagged.
        out: Binary sink for the rendered PDF bytes.
        part: Key into :data:`mailwatch.avery.AVERY` (default ``"8163"``).
        mode: How to distribute ``label`` across sheet positions.

            * ``"single"`` — exactly one label at (``start_row``, ``start_col``).
              For partial-sheet reuse where the rest of the sheet was already
              consumed on a previous print.
            * ``"fill"`` (default) — repeat the label across every remaining
              slot starting at (``start_row``, ``start_col``).
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

    if mode == "single":
        count = 1
    else:  # "fill"
        skip = (start_row - 1) * tpl.cols + (start_col - 1)
        count = max(0, tpl.slots_per_sheet - skip)

    labels = _positions_for_single_label(
        label,
        start_row,
        start_col,
        count,
        cols=tpl.cols,
        human_readable=human_readable,
    )
    # One page always — slot grid is bounded by start_{row,col}..tpl.slots_per_sheet.
    pages = [labels] if labels else []
    html_str = _jinja.get_template("avery.html").render(tpl=tpl, pages=pages)
    HTML(string=html_str, base_url=str(_templates_dir())).write_pdf(out)
