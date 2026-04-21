"""Tests for :mod:`mailwatch.imb`.

Cross-validation strategy: every known-good vector is checked against
reportlab's :class:`USPS_4State` encoder (``.barcodes`` property), so the
handrolled implementation is only trusted when a fully independent encoder
agrees on the same 65-character FADT string.
"""

from __future__ import annotations

import random
import string

import pytest
from reportlab.graphics.barcode.usps4s import USPS_4State

from mailwatch import imb

# USPS-B-3200 Appendix-C-style vectors.
#
# NOTE on vector 3: the expected value given in the Wave 1A spec
# ("DTTAFADDTTFTDTFTFDTDDADADAFADFATDDFTAAAFDTTADFAAATDFDTDFADDDTDFFT")
# does NOT match either the handrolled encoder or reportlab. Both independent
# encoders produce the string used below. Reported to the reviewer.
APPENDIX_C_VECTORS = [
    {
        "tracking": "00040123456200800001",
        "routing": "987654321",
        "inputs": (0, 40, 123456, 200800001, "987654321"),
        "expected": "ADTTTATTTFTDFADTDTFTAATATADDDDFTTDTDFFDFTTATAFFDDADDTFFADFDFTTTAD",
    },
    {
        "tracking": "01234567094987654321",
        "routing": "",
        "inputs": (1, 234, 567094, 987654321, ""),
        "expected": "ATTFATTDTTADTAATTDTDTATTDAFDDFADFDFTFFFFFTATFAAAATDFFTDAADFTFDTDT",
    },
    {
        "tracking": "01234567094987654321",
        "routing": "012345678",
        "inputs": (1, 234, 567094, 987654321, "012345678"),
        # Both imb.encode and reportlab's USPS_4State agree on this output.
        # The Wave 1A spec's expected value differs; see module docstring.
        "expected": "ADFTTAFDTTTTFATTADTAAATFTFTATDAAAFDDADATATDTDTTDFDTDATADADTDFFTFA",
    },
]


# ---------------------------------------------------------------------------
# Cross-validation against reportlab
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vector", APPENDIX_C_VECTORS, ids=lambda v: v["tracking"])
def test_handrolled_matches_expected(vector: dict[str, object]) -> None:
    """imb.encode produces the expected FADT string for each spec vector."""
    inputs = vector["inputs"]
    expected = vector["expected"]
    assert isinstance(inputs, tuple)
    assert isinstance(expected, str)
    assert imb.encode(*inputs) == expected


@pytest.mark.parametrize("vector", APPENDIX_C_VECTORS, ids=lambda v: v["tracking"])
def test_reportlab_matches_expected(vector: dict[str, object]) -> None:
    """reportlab's USPS_4State.barcodes agrees with the expected FADT string."""
    tracking = vector["tracking"]
    routing = vector["routing"]
    expected = vector["expected"]
    assert isinstance(tracking, str)
    assert isinstance(routing, str)
    assert isinstance(expected, str)
    rl = USPS_4State(value=tracking, routing=routing).barcodes
    assert rl == expected


@pytest.mark.parametrize("vector", APPENDIX_C_VECTORS, ids=lambda v: v["tracking"])
def test_handrolled_matches_reportlab(vector: dict[str, object]) -> None:
    """Both independent encoders produce byte-identical FADT strings."""
    inputs = vector["inputs"]
    tracking = vector["tracking"]
    routing = vector["routing"]
    assert isinstance(inputs, tuple)
    assert isinstance(tracking, str)
    assert isinstance(routing, str)
    ours = imb.encode(*inputs)
    theirs = USPS_4State(value=tracking, routing=routing).barcodes
    assert ours == theirs


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_len", [1, 2, 3, 4, 6, 7, 8, 10, 12, 15])
def test_routing_code_invalid_length_raises(bad_len: int) -> None:
    """Routing code length outside {0, 5, 9, 11} is rejected."""
    bad_routing = "1" * bad_len
    with pytest.raises(ValueError, match="routing code"):
        imb.encode(0, 40, 123456, 200800001, bad_routing)


@pytest.mark.parametrize("length", [0, 5, 9, 11])
def test_routing_code_valid_lengths(length: int) -> None:
    """Every permitted routing-code length produces a 65-char FADT string."""
    routing = "1" * length
    result = imb.encode(0, 40, 123456, 200800001, routing)
    assert len(result) == 65
    assert set(result) <= set("FADT")


@pytest.mark.parametrize("bad_id", [-1, 95, 100, 1000])
def test_barcode_id_out_of_range_raises(bad_id: int) -> None:
    """Barcode IDs outside 0..94 are rejected (spec: STID ≤ 94)."""
    with pytest.raises(ValueError, match="barcode_id"):
        imb.encode(bad_id, 40, 123456, 200800001, "")


@pytest.mark.parametrize("bad_st", [-1, 1000, 10000])
def test_service_type_out_of_range_raises(bad_st: int) -> None:
    """Service Type Identifier outside 0..999 is rejected."""
    with pytest.raises(ValueError, match="service_type"):
        imb.encode(0, bad_st, 123456, 200800001, "")


def test_negative_mailer_id_raises() -> None:
    """Negative mailer IDs are rejected."""
    with pytest.raises(ValueError, match="mailer_id"):
        imb.encode(0, 40, -1, 200800001, "")


def test_negative_serial_raises() -> None:
    """Negative serial numbers are rejected."""
    with pytest.raises(ValueError, match="serial"):
        imb.encode(0, 40, 123456, -1, "")


def test_nine_digit_mailer_id_swaps_serial_width() -> None:
    """Mailer IDs beginning with 9 trigger the 9/6 tracking layout."""
    # 9-digit mailer (starts with 9), 6-digit serial.
    result = imb.encode(0, 40, 912345678, 123456, "")
    assert len(result) == 65
    assert set(result) <= set("FADT")


# ---------------------------------------------------------------------------
# Property-style randomised sanity check
# ---------------------------------------------------------------------------


def test_random_valid_inputs_produce_fadt_strings() -> None:
    """Well-formed random inputs always produce 65-char strings over FADT."""
    rng = random.Random(0xB3200)  # noqa: S311  (not cryptographic)
    fadt = set("FADT")
    routing_lengths = [0, 5, 9, 11]
    for _ in range(64):
        barcode_id = rng.randint(0, 94)
        service_type = rng.randint(0, 999)
        use_9_mailer = rng.random() < 0.25
        if use_9_mailer:
            mailer_id = rng.randint(900000000, 999999999)
            serial = rng.randint(0, 999999)
        else:
            mailer_id = rng.randint(0, 899999)
            serial = rng.randint(0, 999999999)
        rlen = rng.choice(routing_lengths)
        routing = "".join(rng.choices(string.digits, k=rlen))
        result = imb.encode(barcode_id, service_type, mailer_id, serial, routing)
        assert len(result) == 65
        assert set(result) <= fadt


def test_random_valid_inputs_cross_validate_against_reportlab() -> None:
    """Random well-formed inputs agree byte-for-byte with reportlab."""
    rng = random.Random(0xC0DE)  # noqa: S311  (not cryptographic)
    routing_lengths = [0, 5, 9, 11]
    for _ in range(16):
        barcode_id = rng.randint(0, 94)
        service_type = rng.randint(0, 999)
        use_9_mailer = rng.random() < 0.25
        if use_9_mailer:
            mailer_id = rng.randint(900000000, 999999999)
            serial = rng.randint(0, 999999)
            tracking = f"{barcode_id:02d}{service_type:03d}{mailer_id:09d}{serial:06d}"
        else:
            mailer_id = rng.randint(0, 899999)
            serial = rng.randint(0, 999999999)
            tracking = f"{barcode_id:02d}{service_type:03d}{mailer_id:06d}{serial:09d}"
        rlen = rng.choice(routing_lengths)
        routing = "".join(rng.choices(string.digits, k=rlen))
        ours = imb.encode(barcode_id, service_type, mailer_id, serial, routing)
        theirs = USPS_4State(value=tracking, routing=routing).barcodes
        assert ours == theirs, (
            f"divergence at inputs=({barcode_id}, {service_type}, {mailer_id}, "
            f"{serial}, {routing!r}):\n  ours   ={ours}\n  reportlab={theirs}"
        )
