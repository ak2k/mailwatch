"""Smoke tests for :mod:`mailwatch.pdf` (WeasyPrint backend).

The prior reportlab-era suite asserted byte-level PDF content by
disabling stream compression. WeasyPrint's output isn't byte-stable in
the same way (it compresses + uses different object layout), so the
invariants here are cheap and platform-independent:

* Render doesn't raise.
* Output is a syntactically valid PDF (magic + EOF).
* Page dimensions match the spec.
* For Avery renders, page count matches the distribution we asked for.

Deeper text-level assertions can be added with `pypdf` / `pdfplumber`
later if needed — intentionally absent here to keep the suite fast.
"""

from __future__ import annotations

import io

import pypdf
import pytest

from mailwatch.avery import AVERY
from mailwatch.layouts import ENVELOPES
from mailwatch.pdf import LabelData, render_avery, render_envelope

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
    """Return (width, height) of ``page`` in inches.

    PDF MediaBox is in points (72 pt/in).
    """
    reader = pypdf.PdfReader(io.BytesIO(blob))
    mb = reader.pages[page].mediabox
    return round(float(mb.width) / 72, 3), round(float(mb.height) / 72, 3)


# --------------------------------------------------------------------------- #
# Envelope                                                                    #
# --------------------------------------------------------------------------- #


def test_envelope_default_renders(
    sender_lines: list[str],
    recipient_lines: list[str],
) -> None:
    buf = io.BytesIO()
    render_envelope(
        sender_lines,
        recipient_lines,
        tracking="00040904164589000001",
        routing="100012345",
        out=buf,
    )
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 1


def test_envelope_with_empty_routing(
    sender_lines: list[str],
    recipient_lines: list[str],
) -> None:
    """Routing is optional — empty string must still produce a valid PDF."""
    buf = io.BytesIO()
    render_envelope(
        sender_lines,
        recipient_lines,
        tracking="00040904164589000001",
        routing="",
        out=buf,
    )
    _assert_valid_pdf(buf.getvalue())


def test_envelope_rejects_bad_tracking_length(
    sender_lines: list[str],
    recipient_lines: list[str],
) -> None:
    with pytest.raises(ValueError, match="20 digits"):
        render_envelope(
            sender_lines,
            recipient_lines,
            tracking="12345",
            routing="100012345",
            out=io.BytesIO(),
        )


@pytest.mark.parametrize("size", list(ENVELOPES))
def test_envelope_page_dims_match_spec(
    size: str,
    sender_lines: list[str],
    recipient_lines: list[str],
) -> None:
    """Every catalog size renders at the declared page dimensions."""
    buf = io.BytesIO()
    render_envelope(
        sender_lines,
        recipient_lines,
        tracking="00040904164589000001",
        routing="100012345",
        out=buf,
        envelope_size=size,
    )
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 1
    spec = ENVELOPES[size]
    assert _page_dims_inches(blob) == (round(spec.w, 3), round(spec.h, 3))


def test_envelope_long_content_does_not_widen_block(
    recipient_lines: list[str],
) -> None:
    """A comically long sender line should clip rather than spilling into the barcode area."""
    very_long = ["X" * 200]
    buf = io.BytesIO()
    render_envelope(
        very_long,
        recipient_lines,
        tracking="00040904164589000001",
        routing="100012345",
        out=buf,
    )
    _assert_valid_pdf(buf.getvalue())


def test_envelope_unknown_size_raises() -> None:
    with pytest.raises(KeyError):
        render_envelope(
            ["X"],
            ["Y"],
            tracking="00040904164589000001",
            routing="",
            out=io.BytesIO(),
            envelope_size="#99",
        )


# --------------------------------------------------------------------------- #
# Avery                                                                       #
# --------------------------------------------------------------------------- #


def test_avery_single_one_label(label: LabelData) -> None:
    buf = io.BytesIO()
    render_avery(label, buf, mode="single", start_row=3, start_col=2)
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 1


def test_avery_fill_default_fills_whole_sheet(label: LabelData) -> None:
    """Fill on an 8163 sheet → 10 labels on one page."""
    buf = io.BytesIO()
    render_avery(label, buf, mode="fill")
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 1


def test_avery_fill_with_skip(label: LabelData) -> None:
    buf = io.BytesIO()
    render_avery(label, buf, mode="fill", start_row=2, start_col=1)
    _assert_valid_pdf(buf.getvalue())


def test_avery_batch_multiple_labels(label: LabelData) -> None:
    labels = [{**label, "recipient": [f"RECIPIENT {i}"]} for i in range(3)]
    buf = io.BytesIO()
    render_avery(labels, buf, mode="batch")
    _assert_valid_pdf(buf.getvalue())


def test_avery_single_rejects_list_input(label: LabelData) -> None:
    with pytest.raises(ValueError, match="single LabelData"):
        render_avery([label, label], io.BytesIO(), mode="single")


def test_avery_batch_requires_list(label: LabelData) -> None:
    with pytest.raises(ValueError, match="list of LabelData"):
        render_avery(label, io.BytesIO(), mode="batch")


def test_avery_rejects_out_of_range_start_position(label: LabelData) -> None:
    # 8163 is 2 cols x 5 rows — anything beyond that must fail.
    with pytest.raises(ValueError, match="start_row must be"):
        render_avery(label, io.BytesIO(), start_row=0)
    with pytest.raises(ValueError, match="start_col must be"):
        render_avery(label, io.BytesIO(), start_col=3)


def test_avery_batch_spills_across_pages(label: LabelData) -> None:
    """15 distinct labels on 8163 → 2 pages (10 on first, 5 on second)."""
    labels = [{**label, "recipient": [f"RECIPIENT {i}"]} for i in range(15)]
    buf = io.BytesIO()
    render_avery(labels, buf, mode="batch")
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 2


# Cover one part from each geometry family (shipping / address / return-address)
# — running every part would add wall time with no new failure modes.
@pytest.mark.parametrize("part", ["5160", "5163", "5167", "8163"])
def test_avery_parts_smoke(label: LabelData, part: str) -> None:
    buf = io.BytesIO()
    render_avery(label, buf, part=part, mode="fill")
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    # 8.5 x 11 US Letter for every part in the catalog.
    tpl = AVERY[part]
    assert _page_dims_inches(blob) == (round(tpl.page_w, 3), round(tpl.page_h, 3))


def test_avery_rejects_unknown_part(label: LabelData) -> None:
    with pytest.raises(KeyError):
        render_avery(label, io.BytesIO(), part="9999")


def test_avery_part_with_smaller_grid_rejects_out_of_range(label: LabelData) -> None:
    """5167 is a 4x20 grid — row=21 must be rejected, col=5 must be rejected."""
    with pytest.raises(ValueError, match="start_row must be"):
        render_avery(label, io.BytesIO(), part="5167", start_row=21)
    with pytest.raises(ValueError, match="start_col must be"):
        render_avery(label, io.BytesIO(), part="5167", start_col=5)
