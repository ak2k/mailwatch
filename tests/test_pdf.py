"""Smoke tests for :mod:`mailwatch.pdf` (WeasyPrint backend).

WeasyPrint output isn't byte-stable (compressed object streams, randomised
IDs), so the invariants here are cheap and platform-independent:

* Render doesn't raise.
* Output is a syntactically valid PDF (magic + EOF).
* Page dimensions match the spec.
* Text block origins land where the spec says they should.
* Batch flows emit the expected number of pages.

Per-size WeasyPrint rendering is limited to 3 representative envelopes
(smallest, default, largest); the remaining sizes are verified via pure
geometry math in ``test_layouts.py`` — rendering them adds wall time
without new failure modes.
"""

from __future__ import annotations

import io

import pypdf
import pytest

from mailwatch.avery import AVERY
from mailwatch.layouts import ENVELOPES
from mailwatch.pdf import (
    EnvelopeData,
    LabelData,
    render_avery,
    render_envelope,
)

_PT_PER_INCH = 72.0


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def sender_lines() -> list[str]:
    return ["ADAM KIRBY", "881 OCEAN DR APT 26C", "KEY BISCAYNE FL 33149-2633"]


@pytest.fixture
def recipient_lines() -> list[str]:
    return ["JOHN DOE", "123 MAIN ST", "ANYTOWN NY 10001-2345"]


@pytest.fixture
def envelope(
    sender_lines: list[str],
    recipient_lines: list[str],
) -> EnvelopeData:
    return {
        "sender": sender_lines,
        "recipient": recipient_lines,
        "tracking": "00040904164589000001",
        "routing": "100012345",
    }


@pytest.fixture
def label(recipient_lines: list[str]) -> LabelData:
    return {
        "recipient": recipient_lines,
        "tracking": "00040904164589000001",
        "routing": "100012345",
    }


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _assert_valid_pdf(blob: bytes) -> None:
    """Cheap sanity: PDF magic header + EOF marker + non-trivial size."""
    assert blob.startswith(b"%PDF-"), f"no PDF magic in first bytes: {blob[:16]!r}"
    assert b"%%EOF" in blob[-1024:], "no %%EOF trailer within last 1KB"
    assert len(blob) > 512, f"PDF implausibly small: {len(blob)} bytes"


def _page_count(blob: bytes) -> int:
    reader = pypdf.PdfReader(io.BytesIO(blob))
    return len(reader.pages)


def _page_dims_inches(blob: bytes, page: int = 0) -> tuple[float, float]:
    """Return (width, height) of ``page`` in inches (MediaBox is in points)."""
    reader = pypdf.PdfReader(io.BytesIO(blob))
    mb = reader.pages[page].mediabox
    return round(float(mb.width) / _PT_PER_INCH, 3), round(float(mb.height) / _PT_PER_INCH, 3)


def _text_emit_positions(blob: bytes) -> list[tuple[float, float, str]]:
    """Return (x_pt, y_pt, sample_text) for every text-run emit on page 0.

    Uses pypdf's visitor hook, which fires once per ``Tj`` / ``TJ``
    operator. Each emit carries the *start* position of the run — enough
    to prove content began inside its declared block, not enough to prove
    it ended inside it (for that see the _validate() rectangle
    invariants in :mod:`mailwatch.layouts`).
    """
    out: list[tuple[float, float, str]] = []

    def visit(text: str, cm, tm, font_dict, font_size) -> None:
        if not text or text.isspace():
            return
        x = float(tm[4]) + float(cm[4])
        y = float(tm[5]) + float(cm[5])
        out.append((x, y, text[:20]))

    reader = pypdf.PdfReader(io.BytesIO(blob))
    reader.pages[0].extract_text(visitor_text=visit)
    return out


def _with_tracking(env: EnvelopeData, tracking: str) -> EnvelopeData:
    return {**env, "tracking": tracking}


def _with_label_tracking(lbl: LabelData, tracking: str) -> LabelData:
    return {**lbl, "tracking": tracking}


# --------------------------------------------------------------------------- #
# Envelope                                                                    #
# --------------------------------------------------------------------------- #


def test_envelope_default_renders(envelope: EnvelopeData) -> None:
    buf = io.BytesIO()
    render_envelope([envelope], buf)
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 1


def test_envelope_with_empty_routing(envelope: EnvelopeData) -> None:
    """Routing is optional — empty string must still produce a valid PDF."""
    buf = io.BytesIO()
    render_envelope([{**envelope, "routing": ""}], buf)
    _assert_valid_pdf(buf.getvalue())


def test_envelope_rejects_bad_tracking_length(envelope: EnvelopeData) -> None:
    with pytest.raises(ValueError, match="20 digits"):
        render_envelope([{**envelope, "tracking": "12345"}], io.BytesIO())


def test_envelope_empty_list_raises(envelope: EnvelopeData) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        render_envelope([], io.BytesIO())


# Smallest / default / largest envelope — the math-level dim checks live in
# test_layouts; no need to render all 9 through WeasyPrint.
@pytest.mark.parametrize("size", ["#6_3_4", "#10", "A10"])
def test_envelope_page_dims_match_spec(size: str, envelope: EnvelopeData) -> None:
    buf = io.BytesIO()
    render_envelope([envelope], buf, envelope_size=size)
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 1
    spec = ENVELOPES[size]
    assert _page_dims_inches(blob) == (round(spec.w, 3), round(spec.h, 3))


def test_envelope_text_emits_start_inside_declared_blocks(
    envelope: EnvelopeData,
) -> None:
    """Every text run on the envelope begins inside the sender or recipient block.

    The _validate() invariants prove the declared blocks don't collide with
    the barcode; this test proves the rendered content actually starts
    inside them. Together they close the 'long content spilled into the
    barcode' regression class.
    """
    buf = io.BytesIO()
    render_envelope([envelope], buf)
    spec = ENVELOPES["#10"]
    sender_x_pt = spec.sender.x * _PT_PER_INCH
    sender_right_pt = (spec.sender.x + spec.sender.w) * _PT_PER_INCH
    recipient_x_pt = spec.recipient.x * _PT_PER_INCH
    recipient_right_pt = (spec.recipient.x + spec.recipient.w) * _PT_PER_INCH
    barcode_x_pt = spec.barcode.x * _PT_PER_INCH
    barcode_right_pt = (spec.barcode.x + spec.barcode.w) * _PT_PER_INCH
    for x_pt, _y_pt, sample in _text_emit_positions(buf.getvalue()):
        # Barcode glyphs (F/A/D/T) render via the USPSIMBStandard font.
        is_barcode = set(sample).issubset({"F", "A", "D", "T"})
        if is_barcode:
            assert barcode_x_pt - 1 <= x_pt <= barcode_right_pt + 1, (
                f"barcode glyph {sample!r} at x={x_pt}pt outside bar column "
                f"({barcode_x_pt}..{barcode_right_pt})"
            )
            continue
        # Non-barcode text must begin inside either the sender or recipient
        # block. Both are 4" wide so the bounds are generous.
        in_sender = sender_x_pt <= x_pt <= sender_right_pt
        in_recipient = recipient_x_pt <= x_pt <= recipient_right_pt
        assert in_sender or in_recipient, (
            f"text {sample!r} started at x={x_pt}pt, outside sender "
            f"({sender_x_pt}..{sender_right_pt}) and recipient "
            f"({recipient_x_pt}..{recipient_right_pt})"
        )


def test_envelope_long_sender_still_renders_single_page(
    recipient_lines: list[str],
) -> None:
    """A 500-char sender line clips via CSS max-width+overflow; PDF stays one page."""
    env: EnvelopeData = {
        "sender": ["X" * 500],
        "recipient": recipient_lines,
        "tracking": "00040904164589000001",
        "routing": "100012345",
    }
    buf = io.BytesIO()
    render_envelope([env], buf)
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 1


def test_envelope_unknown_size_raises(envelope: EnvelopeData) -> None:
    with pytest.raises(KeyError):
        render_envelope([envelope], io.BytesIO(), envelope_size="#99")


def test_envelope_batch_emits_one_page_per_envelope(
    envelope: EnvelopeData,
) -> None:
    """N envelopes in → N-page PDF out, each with its own tracking."""
    envelopes = [_with_tracking(envelope, f"000409041645890000{i:02d}") for i in range(1, 6)]
    buf = io.BytesIO()
    render_envelope(envelopes, buf)
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 5


# --------------------------------------------------------------------------- #
# Avery                                                                       #
# --------------------------------------------------------------------------- #


def test_avery_one_label(label: LabelData) -> None:
    """Single label at an arbitrary slot → one page."""
    buf = io.BytesIO()
    render_avery([label], buf, start_row=3, start_col=2)
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 1


def test_avery_fills_full_sheet(label: LabelData) -> None:
    """10 labels on 8163 (2x5) → 1 page."""
    labels = [_with_label_tracking(label, f"000409041645890000{i:02d}") for i in range(10)]
    buf = io.BytesIO()
    render_avery(labels, buf)
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 1


def test_avery_overflows_to_second_sheet(label: LabelData) -> None:
    """11 labels on 8163 (10 slots) → 2 pages, overflow starts at (1,1)."""
    labels = [_with_label_tracking(label, f"000409041645890000{i:02d}") for i in range(11)]
    buf = io.BytesIO()
    render_avery(labels, buf)
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 2


def test_avery_last_slot_fits_one_label(label: LabelData) -> None:
    """Start at the final slot (row 5, col 2 on 8163) with exactly 1 label → 1 page."""
    buf = io.BytesIO()
    render_avery([label], buf, start_row=5, start_col=2)
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 1


def test_avery_rejects_out_of_range_start_position(label: LabelData) -> None:
    # 8163 is 2 cols x 5 rows — anything beyond that must fail at the library boundary.
    with pytest.raises(ValueError, match="start_row must be"):
        render_avery([label], io.BytesIO(), start_row=0)
    with pytest.raises(ValueError, match="start_col must be"):
        render_avery([label], io.BytesIO(), start_col=3)


def test_avery_empty_list_raises(label: LabelData) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        render_avery([], io.BytesIO())


# Cover one part from each geometry family (shipping / address / return-address)
# — running every part would add wall time with no new failure modes.
@pytest.mark.parametrize("part", ["5160", "5163", "5167", "8163"])
def test_avery_parts_smoke(label: LabelData, part: str) -> None:
    buf = io.BytesIO()
    render_avery([label], buf, part=part)
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    tpl = AVERY[part]
    assert _page_dims_inches(blob) == (round(tpl.page_w, 3), round(tpl.page_h, 3))


def test_avery_rejects_unknown_part(label: LabelData) -> None:
    with pytest.raises(KeyError):
        render_avery([label], io.BytesIO(), part="9999")


def test_avery_part_with_smaller_grid_rejects_out_of_range(label: LabelData) -> None:
    """5167 is a 4x20 grid — row=21 must be rejected, col=5 must be rejected."""
    with pytest.raises(ValueError, match="start_row must be"):
        render_avery([label], io.BytesIO(), part="5167", start_row=21)
    with pytest.raises(ValueError, match="start_col must be"):
        render_avery([label], io.BytesIO(), part="5167", start_col=5)
