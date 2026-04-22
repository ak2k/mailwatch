"""Avery label-sheet template catalog.

A curated subset of ~11 parts covering every shipping / address /
return-address decision a user would actually make. Numbers are from
Avery's own part specs; cross-checked against the glabels-qt XML
(``jimevins/glabels-qt/templates/avery-us-templates.xml``) before any
change here.

We intentionally do not parse the full glabels catalog (~250 parts):
most users only care about the laser-vs-inkjet SKU split inside three
or four label-geometry families.
"""

from __future__ import annotations

from dataclasses import dataclass

from mailwatch._catalog import check


@dataclass(frozen=True)
class AveryTemplate:
    """Label-sheet geometry for one Avery part.

    All distances are in inches. The sheet is portrait US Letter
    (8.5" x 11"). ``(x0, y0)`` is the top-left corner of the first
    label's rectangle; ``(dx, dy)`` is the column / row pitch;
    ``(label_w, label_h)`` is the printable label size.
    """

    part: str
    description: str
    page_w: float
    page_h: float
    cols: int
    rows: int
    x0: float
    y0: float
    dx: float
    dy: float
    label_w: float
    label_h: float

    @property
    def slots_per_sheet(self) -> int:
        return self.cols * self.rows

    @property
    def display(self) -> str:
        return f"Avery {self.part} — {self.description}"


AVERY: dict[str, AveryTemplate] = {
    "5160": AveryTemplate(
        "5160",
        'Address labels, 1" x 2⅝", 30/sheet',
        8.5,
        11,
        3,
        10,
        0.15625,
        0.5,
        2.75,
        1.0,
        2.625,
        1.0,
    ),
    "5161": AveryTemplate(
        "5161",
        'Address labels, 1" x 4", 20/sheet',
        8.5,
        11,
        2,
        10,
        0.15625,
        0.5,
        4.1875,
        1.0,
        4.0,
        1.0,
    ),
    "5162": AveryTemplate(
        "5162",
        'Address labels, 1⅓" x 4", 14/sheet',
        8.5,
        11,
        2,
        7,
        0.15625,
        0.84375,
        4.1875,
        1.333,
        4.0,
        1.333,
    ),
    "5163": AveryTemplate(
        "5163",
        'Shipping labels, 2" x 4", 10/sheet',
        8.5,
        11,
        2,
        5,
        0.15625,
        0.5,
        4.1875,
        2.0,
        4.0,
        2.0,
    ),
    "5164": AveryTemplate(
        "5164",
        'Shipping labels, 3⅓" x 4", 6/sheet',
        8.5,
        11,
        2,
        3,
        0.15625,
        0.5,
        4.1875,
        3.333,
        4.0,
        3.333,
    ),
    "5167": AveryTemplate(
        "5167",
        'Return address labels, ½" x 1¾", 80/sheet',
        8.5,
        11,
        4,
        20,
        0.3,
        0.5,
        2.0625,
        0.5,
        1.75,
        0.5,
    ),
    "8160": AveryTemplate(
        "8160",
        'Address labels, 1" x 2⅝", 30/sheet (inkjet)',
        8.5,
        11,
        3,
        10,
        0.15625,
        0.5,
        2.75,
        1.0,
        2.625,
        1.0,
    ),
    "8162": AveryTemplate(
        "8162",
        'Address labels, 1⅓" x 4", 14/sheet (inkjet)',
        8.5,
        11,
        2,
        7,
        0.15625,
        0.84375,
        4.1875,
        1.333,
        4.0,
        1.333,
    ),
    "8163": AveryTemplate(
        "8163",
        'Shipping labels, 2" x 4", 10/sheet (inkjet)',
        8.5,
        11,
        2,
        5,
        0.15625,
        0.5,
        4.1875,
        2.0,
        4.0,
        2.0,
    ),
    "8164": AveryTemplate(
        "8164",
        'Shipping labels, 3⅓" x 4", 6/sheet (inkjet)',
        8.5,
        11,
        2,
        3,
        0.15625,
        0.5,
        4.1875,
        3.333,
        4.0,
        3.333,
    ),
    "8167": AveryTemplate(
        "8167",
        'Return address labels, ½" x 1¾", 80/sheet (inkjet)',
        8.5,
        11,
        4,
        20,
        0.3,
        0.5,
        2.0625,
        0.5,
        1.75,
        0.5,
    ),
}


DEFAULT_AVERY = "8163"


DISPLAY_NAMES: dict[str, str] = {part: tpl.display for part, tpl in AVERY.items()}


def _validate() -> None:
    for part, tpl in AVERY.items():
        check(tpl.cols >= 1 and tpl.rows >= 1, f"{part}: cols/rows must be positive")
        check(tpl.label_w > 0 and tpl.label_h > 0, f"{part}: label dims must be positive")
        last_right = tpl.x0 + (tpl.cols - 1) * tpl.dx + tpl.label_w
        last_bottom = tpl.y0 + (tpl.rows - 1) * tpl.dy + tpl.label_h
        check(
            last_right <= tpl.page_w + 1e-6,
            f"{part}: last column spills past page width ({last_right} > {tpl.page_w})",
        )
        check(
            last_bottom <= tpl.page_h + 1e-6,
            f"{part}: last row spills past page height ({last_bottom} > {tpl.page_h})",
        )
    check(DEFAULT_AVERY in AVERY, "DEFAULT_AVERY missing from catalog")


_validate()
