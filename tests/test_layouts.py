"""Tests for :mod:`mailwatch.layouts`.

The module-level ``_validate()`` pass is implicitly exercised by any test
that imports ``mailwatch`` — but "implicitly" doesn't prove the assertion
mechanism actually fires. These tests construct deliberately-broken specs
and call the same primitives that ``_validate()`` uses, so a silent
``_check = lambda *a: None`` refactor would trip a red test.
"""

from __future__ import annotations

import pytest

from mailwatch._catalog import check
from mailwatch.layouts import (
    DEFAULT_ENVELOPE,
    DISPLAY_NAMES,
    ENVELOPES,
    Block,
    EnvelopeSpec,
)

# --------------------------------------------------------------------------- #
# Shared helper                                                               #
# --------------------------------------------------------------------------- #


def test_check_raises_on_false() -> None:
    with pytest.raises(AssertionError, match="boom"):
        check(False, "boom")


def test_check_noop_on_true() -> None:
    check(True, "ignored")


# --------------------------------------------------------------------------- #
# Block geometry                                                              #
# --------------------------------------------------------------------------- #


def test_block_overlaps_detects_partial_overlap() -> None:
    a = Block(0, 0, 2, 2)
    b = Block(1, 1, 2, 2)
    assert a.overlaps(b)
    assert b.overlaps(a)


def test_block_overlaps_rejects_touching_edges() -> None:
    """Edge-touching is NOT overlap — matches strict `<` in the impl."""
    a = Block(0, 0, 2, 2)
    b = Block(2, 0, 2, 2)  # shares right edge with a
    assert not a.overlaps(b)
    assert not b.overlaps(a)


def test_block_inside_is_inclusive_on_boundary() -> None:
    outer = Block(0, 0, 10, 10)
    flush = Block(0, 0, 10, 10)
    assert flush.inside(outer)
    spill = Block(0, 0, 10.001, 10)
    assert not spill.inside(outer)


# --------------------------------------------------------------------------- #
# Catalog invariants — prove the _validate() machinery fires                  #
# --------------------------------------------------------------------------- #


def _with_block(spec: EnvelopeSpec, **overrides: Block) -> EnvelopeSpec:
    """Return a copy of ``spec`` with one or more blocks replaced."""
    return EnvelopeSpec(
        display=spec.display,
        w=spec.w,
        h=spec.h,
        sender=overrides.get("sender", spec.sender),
        recipient=overrides.get("recipient", spec.recipient),
        barcode=overrides.get("barcode", spec.barcode),
    )


def test_validate_catches_barcode_outside_bcz() -> None:
    """A barcode block shifted out of the BCZ should fail the BCZ check."""
    spec = ENVELOPES[DEFAULT_ENVELOPE]
    # Move the barcode up past the BCZ top (0.625in).
    broken = _with_block(spec, barcode=Block(spec.w - 4.625, 1.0, 4.5, 0.5))
    assert not broken.barcode.inside(broken.bcz())


def test_validate_catches_recipient_outside_ocr() -> None:
    spec = ENVELOPES[DEFAULT_ENVELOPE]
    # Move recipient above the OCR Read Area (top at 2.75in).
    broken = _with_block(spec, recipient=Block(spec.w - 4.5, 2.0, 4.0, 1.25))
    assert not broken.recipient.inside(broken.ocr_read_area())


def test_validate_catches_sender_recipient_overlap() -> None:
    spec = ENVELOPES[DEFAULT_ENVELOPE]
    # Shrink sender's y so it collides with recipient (both now at y=1.0..2.25).
    broken = _with_block(spec, sender=Block(4.0, 1.0, 4.0, 1.0))
    assert broken.sender.overlaps(broken.recipient)


# --------------------------------------------------------------------------- #
# Catalog contents                                                            #
# --------------------------------------------------------------------------- #


def test_display_names_derive_from_specs() -> None:
    """Making DISPLAY_NAMES a comprehension means drift is impossible."""
    assert set(DISPLAY_NAMES) == set(ENVELOPES)
    for key, label in DISPLAY_NAMES.items():
        assert ENVELOPES[key].display == label


def test_default_envelope_is_in_catalog() -> None:
    assert DEFAULT_ENVELOPE in ENVELOPES


def test_a2_a6_intentionally_absent() -> None:
    """Invitation envelopes were removed until real-piece USPS validation."""
    assert "A2" not in ENVELOPES
    assert "A6" not in ENVELOPES
