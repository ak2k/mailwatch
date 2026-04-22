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
from typing import BinaryIO, TypedDict

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


class EnvelopeData(TypedDict):
    """Per-envelope payload for :func:`render_envelope`.

    Mirrors :class:`LabelData` plus a per-envelope ``sender`` block,
    since envelopes need a return-address region but Avery labels don't.
    Passing a list of items (rather than a single recipient + list of
    trackings) keeps ``render_envelope`` and ``render_avery`` symmetric
    and leaves room for future batch mailings where each envelope
    targets a distinct recipient.
    """

    sender: list[str]
    recipient: list[str]
    tracking: str
    routing: str


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
    envelopes: list[EnvelopeData],
    out: BinaryIO,
    *,
    envelope_size: str = DEFAULT_ENVELOPE,
    human_readable: bool = True,
) -> None:
    """Render ``len(envelopes)`` envelopes (one per page) to ``out``.

    Each :class:`EnvelopeData` carries its own sender / recipient /
    tracking / routing — the common batch flow passes N items with the
    same sender/recipient/routing but distinct trackings, while future
    multi-recipient batches would vary sender/recipient per item.

    Args:
        envelopes: One entry per envelope page. Must be non-empty.
        envelope_size: Key into :data:`mailwatch.layouts.ENVELOPES`.
            Defaults to ``#10``. Unknown keys raise :class:`KeyError`.
            The same size applies to every envelope in ``envelopes``.
        human_readable: Draw the tiny numeric reference row above the
            barcode on every envelope. Pass False to omit.
    """
    if not envelopes:
        raise ValueError("envelopes must be non-empty")
    spec = ENVELOPES[envelope_size]
    env_dicts = [
        {
            "sender_lines": env["sender"],
            "recipient_lines": env["recipient"],
            "barcode_bars": _encode_bars(env["tracking"], env["routing"]),
            "human_readable_text": _human_readable(env["tracking"], env["routing"]),
        }
        for env in envelopes
    ]
    html_str = _jinja.get_template("envelope.html").render(
        spec=spec,
        envelopes=env_dicts,
        human_readable=human_readable,
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


def render_avery(
    labels: list[LabelData],
    out: BinaryIO,
    *,
    part: str = DEFAULT_AVERY,
    start_row: int = 1,
    start_col: int = 1,
    human_readable: bool = True,
) -> None:
    """Render ``len(labels)`` labels across one or more Avery sheets.

    Labels are placed row-major starting at (``start_row``, ``start_col``)
    on page 1. When page 1 runs out of slots, overflow continues on
    page 2 starting at (1, 1). Each :class:`LabelData` carries its own
    tracking + routing + recipient — callers pass a list of distinct
    labels for batch mailings (one IMb serial per physical letter), or a
    single-item list for the common one-letter flow.

    Args:
        labels: One entry per physical label. Must be non-empty.
        part: Key into :data:`mailwatch.avery.AVERY` (default ``"8163"``).
        start_row: 1-indexed row on page 1 (1..tpl.rows). Page 2+ always
            starts at row 1.
        start_col: 1-indexed column within start_row (1..tpl.cols).
            Page 2+ always starts at col 1.
        human_readable: Draw the tiny numeric reference row under each
            barcode. Pass False to show bars only.
    """
    if not labels:
        raise ValueError("labels must be non-empty")
    tpl = AVERY[part]
    if not 1 <= start_row <= tpl.rows:
        raise ValueError(f"start_row must be 1..{tpl.rows}, got {start_row}")
    if not 1 <= start_col <= tpl.cols:
        raise ValueError(f"start_col must be 1..{tpl.cols}, got {start_col}")

    pages: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    row, col = start_row, start_col
    for label in labels:
        current.append(_label_dict(label, row, col, human_readable=human_readable))
        col += 1
        if col > tpl.cols:
            col = 1
            row += 1
        if row > tpl.rows:
            # Page full; overflow starts a new page at (1, 1).
            pages.append(current)
            current = []
            row, col = 1, 1
    if current:
        pages.append(current)

    html_str = _jinja.get_template("avery.html").render(tpl=tpl, pages=pages)
    HTML(string=html_str, base_url=str(_templates_dir())).write_pdf(out)
