"""Envelope layout specs — page sizes + block positions for USPS letter mail.

One spec per supported envelope size. Each spec describes three positioned
blocks (sender, recipient, barcode) in a PostScript-style coordinate frame
with origin at the envelope's bottom-left corner. The WeasyPrint template
reads ``.x``/``.y``/``.w``/``.h`` straight into CSS ``left``/``bottom``/
``width``/``height``.

A :func:`_validate` pass runs at import time and fails loudly if any spec
violates USPS geometry rules:

* recipient must fit inside the OCR Read Area (DMM 202.2.0)
* barcode must fit inside the Barcode Clear Zone (DMM 708.4.2.5)
* sender / recipient / barcode must not overlap each other
* every block must fit on the page

That single sweep catches the common "I tweaked a coordinate and something
collides with the barcode" class of bug at startup, with zero runtime cost
and no need for a test file to import the module explicitly — any test
that touches ``mailwatch`` triggers the check.
"""

from __future__ import annotations

from dataclasses import dataclass

from mailwatch._catalog import check


@dataclass(frozen=True)
class Block:
    """An axis-aligned rectangle in inches, origin at bottom-left."""

    x: float
    y: float
    w: float
    h: float

    @property
    def _right(self) -> float:
        return self.x + self.w

    @property
    def _top(self) -> float:
        return self.y + self.h

    def overlaps(self, other: Block) -> bool:
        return (
            self.x < other._right
            and other.x < self._right
            and self.y < other._top
            and other.y < self._top
        )

    def inside(self, outer: Block) -> bool:
        return (
            self.x >= outer.x
            and self.y >= outer.y
            and self._right <= outer._right
            and self._top <= outer._top
        )


@dataclass(frozen=True)
class EnvelopeSpec:
    """Page size + block positions for one envelope size.

    ``display`` is the human-readable label for UI dropdowns (e.g.
    ``#10 — 9.5" x 4.125"``). Catalog keys are ASCII-safe ids.
    """

    display: str
    w: float
    h: float
    sender: Block
    recipient: Block
    barcode: Block

    def page(self) -> Block:
        return Block(0, 0, self.w, self.h)

    def ocr_read_area(self) -> Block:
        """USPS DMM 202.2.0 OCR Read Area.

        0.5" clearance from each side, 0.625"..2.75" from the bottom edge.
        """
        return Block(0.5, 0.625, self.w - 1.0, 2.125)

    def bcz(self) -> Block:
        """USPS DMM 708.4.2.5 Barcode Clear Zone: 4.75" x 0.625" at bottom-right."""
        return Block(self.w - 4.75, 0.0, 4.75, 0.625)


def _landscape(display: str, w: float, h: float) -> EnvelopeSpec:
    """Standard layout for landscape letter envelopes.

    Sender sits top-left in a 4"x1" box. Recipient sits right-of-center in a
    4"x1.25" box above the BCZ, fully inside the OCR Read Area. Barcode
    is 4.5"x0.5" centered inside the 4.75"x0.625" BCZ, with 0.0625"
    clearance on each side so the rendered bars have headroom over the
    actual USPSIMBStandard glyph width.

    These numbers are chosen so the same three blocks fit every envelope
    from #6¾ (the smallest) up through A10 without per-size overrides.
    Invitation envelopes (A2, A6) are NOT included: their narrow aspect
    ratios and common addressing conventions don't match this
    recipient-right / barcode-bottom-right layout. Add them only after
    verifying a real printed piece against USPS OCR/BCZ requirements.
    """
    return EnvelopeSpec(
        display=display,
        w=w,
        h=h,
        sender=Block(x=0.25, y=h - 1.25, w=4.0, h=1.0),
        recipient=Block(x=w - 4.5, y=1.0, w=4.0, h=1.25),
        barcode=Block(x=w - 4.625, y=0.0625, w=4.5, h=0.5),
    )


ENVELOPES: dict[str, EnvelopeSpec] = {
    "#6_3_4": _landscape('#6¾ — 6.5" x 3.625"', 6.5, 3.625),
    "#7_3_4": _landscape('#7¾ (Monarch) — 7.5" x 3.875"', 7.5, 3.875),
    "#9": _landscape('#9 — 8.875" x 3.875"', 8.875, 3.875),
    "#10": _landscape('#10 — 9.5" x 4.125" (default)', 9.5, 4.125),
    "#11": _landscape('#11 — 10.375" x 4.5"', 10.375, 4.5),
    "#12": _landscape('#12 — 11" x 4.75"', 11.0, 4.75),
    "A7": _landscape('A7 — 7.25" x 5.25"', 7.25, 5.25),
    "A8": _landscape('A8 — 8.125" x 5.5"', 8.125, 5.5),
    "A10": _landscape('A10 — 9.5" x 6"', 9.5, 6.0),
}
"""Catalog keyed by a URL-safe ASCII id.

Keys avoid both fragment (``#``) and path-separator (``/``) tension by
using the form ``#N`` or ``#N_D_D`` — ``#`` is the only non-alphanumeric
character, and it's consistently percent-encoded via ``urlencode`` at
every route boundary.

Invitation envelopes (A2, A6) are intentionally absent — see
:func:`_landscape` docstring.
"""

DEFAULT_ENVELOPE = "#10"


DISPLAY_NAMES: dict[str, str] = {k: v.display for k, v in ENVELOPES.items()}


def _validate() -> None:
    """Fail at import time if any spec violates USPS geometry."""
    for name, spec in ENVELOPES.items():
        page = spec.page()
        ocr = spec.ocr_read_area()
        bcz = spec.bcz()
        check(spec.sender.inside(page), f"{name}: sender outside page")
        check(spec.recipient.inside(page), f"{name}: recipient outside page")
        check(spec.barcode.inside(page), f"{name}: barcode outside page")
        check(
            not spec.sender.overlaps(spec.recipient),
            f"{name}: sender/recipient overlap",
        )
        check(
            not spec.sender.overlaps(spec.barcode),
            f"{name}: sender/barcode overlap",
        )
        check(
            not spec.recipient.overlaps(spec.barcode),
            f"{name}: recipient/barcode overlap",
        )
        check(spec.recipient.inside(ocr), f"{name}: recipient outside OCR Read Area")
        check(spec.barcode.inside(bcz), f"{name}: barcode outside BCZ")
    check(DEFAULT_ENVELOPE in ENVELOPES, "DEFAULT_ENVELOPE missing from catalog")


_validate()
