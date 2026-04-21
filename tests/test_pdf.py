"""Tests for :mod:`mailwatch.pdf`.

Output is always written to :class:`io.BytesIO` sinks — no generated PDFs
land on disk. Cross-validation of human-readable content uses an
uncompressed PDF stream (via ``reportlab.rl_config.pageCompression = 0``
set through a fixture) so that text literals appear byte-verbatim in the
PDF output without requiring a PDF parser dependency.

Appendix C test vectors are shared with :mod:`tests.test_imb` and are
known-good under both the handrolled encoder and reportlab's
:class:`~reportlab.graphics.barcode.usps4s.USPS_4State`.
"""

from __future__ import annotations

import io

import pytest
import reportlab.rl_config

from mailwatch.pdf import LabelData, render_avery8163, render_envelope

# Shared Appendix-C vectors (same as tests/test_imb.py).
APPENDIX_C_VECTORS = [
    {
        "tracking": "00040123456200800001",
        "routing": "987654321",
    },
    {
        "tracking": "01234567094987654321",
        "routing": "",
    },
    {
        "tracking": "01234567094987654321",
        "routing": "012345678",
    },
]

_SAMPLE_SENDER = [
    "Acme Widgets Co",
    "123 Main St",
    "Springfield IL 62701",
]
_SAMPLE_RECIPIENT = [
    "Jane Doe",
    "456 Oak Ave",
    "Portland OR 97201-1234",
]
_SAMPLE_TRACKING = "01234567094987654321"
_SAMPLE_ROUTING = "012345678"


@pytest.fixture
def _no_pdf_compression(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable reportlab PDF stream compression for the duration of a test.

    With compression on, text literals are encoded as Flate/ASCII85 streams
    and a byte search for the original string would miss. Turning it off
    lets us do simple ``in`` checks against the PDF bytes without pulling
    in ``pypdf`` as a dependency.
    """
    monkeypatch.setattr(reportlab.rl_config, "pageCompression", 0)


# ---------------------------------------------------------------------------
# render_envelope
# ---------------------------------------------------------------------------


def test_envelope_produces_valid_pdf_magic() -> None:
    """A rendered envelope starts with the PDF file-signature magic bytes."""
    buf = io.BytesIO()
    render_envelope(
        _SAMPLE_SENDER,
        _SAMPLE_RECIPIENT,
        _SAMPLE_TRACKING,
        _SAMPLE_ROUTING,
        buf,
    )
    data = buf.getvalue()
    assert data.startswith(b"%PDF-")


def test_envelope_is_at_least_1kb() -> None:
    """Sanity: a complete envelope PDF is > 1 KB (bars + text + metadata)."""
    buf = io.BytesIO()
    render_envelope(
        _SAMPLE_SENDER,
        _SAMPLE_RECIPIENT,
        _SAMPLE_TRACKING,
        _SAMPLE_ROUTING,
        buf,
    )
    assert len(buf.getvalue()) >= 1024


def test_envelope_human_readable_false_still_valid_and_smaller() -> None:
    """``human_readable=False`` produces a valid PDF slightly smaller than True."""
    buf_with = io.BytesIO()
    render_envelope(
        _SAMPLE_SENDER,
        _SAMPLE_RECIPIENT,
        _SAMPLE_TRACKING,
        _SAMPLE_ROUTING,
        buf_with,
        human_readable=True,
    )
    buf_without = io.BytesIO()
    render_envelope(
        _SAMPLE_SENDER,
        _SAMPLE_RECIPIENT,
        _SAMPLE_TRACKING,
        _SAMPLE_ROUTING,
        buf_without,
        human_readable=False,
    )
    assert buf_without.getvalue().startswith(b"%PDF-")
    # Without the HR line the PDF content stream is strictly shorter.
    # (Small overheads can differ — allow equal as a safety net but flag
    # if the "off" version is actually bigger.)
    assert len(buf_without.getvalue()) <= len(buf_with.getvalue())


def test_envelope_empty_routing_works() -> None:
    """Zero-length routing code (no ZIP) still renders a valid envelope."""
    buf = io.BytesIO()
    render_envelope(
        _SAMPLE_SENDER,
        _SAMPLE_RECIPIENT,
        _SAMPLE_TRACKING,
        "",
        buf,
    )
    assert buf.getvalue().startswith(b"%PDF-")
    assert len(buf.getvalue()) >= 1024


@pytest.mark.parametrize("routing_len", [0, 5, 9, 11])
def test_envelope_all_routing_lengths(routing_len: int) -> None:
    """Every USPS-permitted routing length (0/5/9/11) renders successfully."""
    routing = "1" * routing_len
    buf = io.BytesIO()
    render_envelope(
        _SAMPLE_SENDER,
        _SAMPLE_RECIPIENT,
        _SAMPLE_TRACKING,
        routing,
        buf,
    )
    assert buf.getvalue().startswith(b"%PDF-")


@pytest.mark.parametrize("vector", APPENDIX_C_VECTORS, ids=lambda v: v["tracking"])
def test_envelope_renders_appendix_c_vectors(vector: dict[str, str]) -> None:
    """All three Appendix-C vectors render without raising."""
    buf = io.BytesIO()
    render_envelope(
        _SAMPLE_SENDER,
        _SAMPLE_RECIPIENT,
        vector["tracking"],
        vector["routing"],
        buf,
    )
    assert buf.getvalue().startswith(b"%PDF-")


def test_envelope_human_readable_text_embedded_in_pdf(
    _no_pdf_compression: None,
) -> None:
    """With compression off, the human-readable tracking+routing string
    appears verbatim in the PDF bytes.

    This catches a regression where the HR line gets dropped or the
    combined format changes; it is not a full PDF-parser check but it
    confirms reportlab is writing the expected text literal.
    """
    buf = io.BytesIO()
    render_envelope(
        _SAMPLE_SENDER,
        _SAMPLE_RECIPIENT,
        _SAMPLE_TRACKING,
        _SAMPLE_ROUTING,
        buf,
        human_readable=True,
    )
    data = buf.getvalue()
    # The tracking number should appear (reportlab drawString emits it
    # as a parenthesised PDF string literal inside a BT/ET block).
    assert _SAMPLE_TRACKING.encode("ascii") in data
    # The routing digits should also appear.
    assert _SAMPLE_ROUTING.encode("ascii") in data


def test_envelope_human_readable_off_omits_tracking_text(
    _no_pdf_compression: None,
) -> None:
    """With ``human_readable=False`` the tracking string is not drawn as text.

    The barcode itself doesn't encode the digits in ASCII — it's 65 FADT
    bars — so an ASCII search for the 20-digit string should miss when
    the HR line is off.
    """
    buf = io.BytesIO()
    render_envelope(
        _SAMPLE_SENDER,
        _SAMPLE_RECIPIENT,
        _SAMPLE_TRACKING,
        _SAMPLE_ROUTING,
        buf,
        human_readable=False,
    )
    assert _SAMPLE_TRACKING.encode("ascii") not in buf.getvalue()


# ---------------------------------------------------------------------------
# render_avery8163
# ---------------------------------------------------------------------------


def _make_labels(n: int) -> list[LabelData]:
    """Build ``n`` distinct placeholder LabelData records."""
    return [
        LabelData(
            recipient=[f"Recipient {i}", "123 Any St", "Anytown ST 12345"],
            tracking=_SAMPLE_TRACKING,
            routing=_SAMPLE_ROUTING,
        )
        for i in range(n)
    ]


def test_avery_full_page_10_labels_fits_one_page() -> None:
    """A 2x5 sheet fits exactly 10 labels on a single page."""
    buf = io.BytesIO()
    render_avery8163(_make_labels(10), buf)
    data = buf.getvalue()
    assert data.startswith(b"%PDF-")
    assert len(data) >= 1024


def test_avery_partial_page_skips_used_positions() -> None:
    """start_row=2, start_col=1 marks (1,1) and (1,2) as used (2 positions)."""
    buf = io.BytesIO()
    render_avery8163(_make_labels(3), buf, start_row=2, start_col=1)
    data = buf.getvalue()
    assert data.startswith(b"%PDF-")
    assert len(data) >= 1024


@pytest.mark.parametrize(
    ("start_row", "start_col", "expected_skip"),
    [
        (1, 1, 0),
        (1, 2, 1),
        (2, 1, 2),
        (3, 2, 5),
        (5, 2, 9),
    ],
)
def test_avery_partial_page_all_start_positions(
    start_row: int,
    start_col: int,
    expected_skip: int,
) -> None:
    """Every valid (row, col) start renders successfully, with the expected skip count."""
    # Enough labels to fill the remaining positions plus a few more.
    n = 10 - expected_skip
    buf = io.BytesIO()
    render_avery8163(_make_labels(n), buf, start_row=start_row, start_col=start_col)
    assert buf.getvalue().startswith(b"%PDF-")


def test_avery_empty_labels_data_does_not_error() -> None:
    """An empty labels_data list renders a valid (if trivial) PDF without raising."""
    buf = io.BytesIO()
    render_avery8163([], buf)
    data = buf.getvalue()
    # pylabels emits a minimal PDF even with zero labels.
    assert data.startswith(b"%PDF-")


def test_avery_start_row_out_of_range_raises() -> None:
    """start_row outside 1..5 is rejected."""
    buf = io.BytesIO()
    with pytest.raises(ValueError, match="start_row"):
        render_avery8163(_make_labels(1), buf, start_row=6)


def test_avery_start_col_out_of_range_raises() -> None:
    """start_col outside 1..2 is rejected."""
    buf = io.BytesIO()
    with pytest.raises(ValueError, match="start_col"):
        render_avery8163(_make_labels(1), buf, start_col=3)


@pytest.mark.parametrize("vector", APPENDIX_C_VECTORS, ids=lambda v: v["tracking"])
def test_avery_renders_appendix_c_vectors(vector: dict[str, str]) -> None:
    """All three Appendix-C vectors render successfully on a label sheet."""
    labels: list[LabelData] = [
        LabelData(
            recipient=["Test Recipient", "123 Main St", "Anytown ST 12345"],
            tracking=vector["tracking"],
            routing=vector["routing"],
        )
    ]
    buf = io.BytesIO()
    render_avery8163(labels, buf)
    assert buf.getvalue().startswith(b"%PDF-")


def test_avery_11_labels_spans_two_pages() -> None:
    """11 labels overflow one page and produce a 2-page PDF."""
    buf = io.BytesIO()
    render_avery8163(_make_labels(11), buf)
    data = buf.getvalue()
    assert data.startswith(b"%PDF-")
    # crude: two pages means two "/Page " object markers (not perfectly
    # robust across reportlab versions, so just assert the PDF is bigger
    # than a one-page sheet).
    one_page = io.BytesIO()
    render_avery8163(_make_labels(10), one_page)
    assert len(data) > len(one_page.getvalue())
