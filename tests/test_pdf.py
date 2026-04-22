"""Smoke tests for :mod:`mailwatch.pdf` (WeasyPrint backend).

The prior reportlab-era suite asserted byte-level PDF content by
disabling stream compression. WeasyPrint's output isn't byte-stable in
the same way (it compresses + uses different object layout), so the
invariants here are cheap and platform-independent:

* Render doesn't raise.
* Output is a syntactically valid PDF (magic + EOF).
* For Avery renders, page count matches the distribution we asked for.

Deeper text-level assertions can be added with `pypdf` / `pdfplumber`
later if needed — intentionally absent here to keep the suite fast.
"""

from __future__ import annotations

import io

import pypdf
import pytest

from mailwatch.pdf import LabelData, render_avery8163, render_envelope

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
    """Return the number of pages in a PDF blob.

    Parses the object tree via ``pypdf`` rather than regexing for
    ``/Type /Page``; WeasyPrint compresses object streams so the
    literal marker doesn't appear in the raw bytes.
    """
    reader = pypdf.PdfReader(io.BytesIO(blob))
    return len(reader.pages)


# --------------------------------------------------------------------------- #
# Envelope                                                                    #
# --------------------------------------------------------------------------- #


def test_envelope_renders(
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


# --------------------------------------------------------------------------- #
# Avery 8163                                                                  #
# --------------------------------------------------------------------------- #


def test_avery_single_one_label(label: LabelData) -> None:
    buf = io.BytesIO()
    render_avery8163(label, buf, mode="single", start_row=3, start_col=2)
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 1


def test_avery_fill_default_fills_whole_sheet(label: LabelData) -> None:
    """mode='fill' with no skip → one page (10 labels on the same sheet)."""
    buf = io.BytesIO()
    render_avery8163(label, buf, mode="fill")
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 1


def test_avery_fill_with_skip(label: LabelData) -> None:
    """Start at (2, 1) — first 2 slots blank, 8 filled, still one sheet."""
    buf = io.BytesIO()
    render_avery8163(label, buf, mode="fill", start_row=2, start_col=1)
    _assert_valid_pdf(buf.getvalue())


def test_avery_batch_multiple_labels(label: LabelData) -> None:
    labels = [{**label, "recipient": [f"RECIPIENT {i}"]} for i in range(3)]
    buf = io.BytesIO()
    render_avery8163(labels, buf, mode="batch")
    _assert_valid_pdf(buf.getvalue())


def test_avery_single_rejects_list_input(label: LabelData) -> None:
    with pytest.raises(ValueError, match="single LabelData"):
        render_avery8163([label, label], io.BytesIO(), mode="single")


def test_avery_batch_requires_list(label: LabelData) -> None:
    with pytest.raises(ValueError, match="list of LabelData"):
        render_avery8163(label, io.BytesIO(), mode="batch")


def test_avery_rejects_out_of_range_start_position(label: LabelData) -> None:
    with pytest.raises(ValueError, match="start_row must be"):
        render_avery8163(label, io.BytesIO(), start_row=0)
    with pytest.raises(ValueError, match="start_col must be"):
        render_avery8163(label, io.BytesIO(), start_col=3)


def test_avery_batch_spills_across_pages(label: LabelData) -> None:
    """15 distinct labels → 2 pages (10 on first, 5 on second)."""
    labels = [{**label, "recipient": [f"RECIPIENT {i}"]} for i in range(15)]
    buf = io.BytesIO()
    render_avery8163(labels, buf, mode="batch")
    blob = buf.getvalue()
    _assert_valid_pdf(blob)
    assert _page_count(blob) == 2
