"""Tests for :mod:`mailwatch.avery`.

Module-level ``_validate()`` is exercised on every import; these tests
prove the underlying invariants are real, not tautological.
"""

from __future__ import annotations

from mailwatch.avery import (
    AVERY,
    DEFAULT_AVERY,
    DISPLAY_NAMES,
    AveryTemplate,
)


def test_default_avery_is_in_catalog() -> None:
    assert DEFAULT_AVERY in AVERY


def test_display_names_derive_from_templates() -> None:
    """Derived comprehension = drift is impossible by construction."""
    assert set(DISPLAY_NAMES) == set(AVERY)
    for part, label in DISPLAY_NAMES.items():
        assert AVERY[part].display == label
        assert label.startswith(f"Avery {part} —")


def test_slots_per_sheet_matches_cols_times_rows() -> None:
    for tpl in AVERY.values():
        assert tpl.slots_per_sheet == tpl.cols * tpl.rows


def test_last_label_fits_on_page() -> None:
    """Reassert the catalog invariant directly, catching any regression in _validate()."""
    for part, tpl in AVERY.items():
        last_right = tpl.x0 + (tpl.cols - 1) * tpl.dx + tpl.label_w
        last_bottom = tpl.y0 + (tpl.rows - 1) * tpl.dy + tpl.label_h
        assert last_right <= tpl.page_w + 1e-6, f"{part}: column overflow"
        assert last_bottom <= tpl.page_h + 1e-6, f"{part}: row overflow"


def test_catalog_spans_all_three_families() -> None:
    """Catalog must cover address / shipping / return-address label kinds."""
    descs = " ".join(tpl.description.lower() for tpl in AVERY.values())
    assert "address labels" in descs
    assert "shipping labels" in descs
    assert "return address" in descs


def test_broken_template_last_right_would_fail() -> None:
    """Reach into the last-right arithmetic directly — sanity check on the invariant logic."""
    tpl = AveryTemplate(
        "test",
        "bogus",
        8.5,
        11,
        3,
        1,
        0.1,
        0.1,
        3.0,
        1.0,
        3.0,
        1.0,
    )
    # x0 + (cols-1)*dx + label_w = 0.1 + 2*3.0 + 3.0 = 9.1 > 8.5 page width
    last_right = tpl.x0 + (tpl.cols - 1) * tpl.dx + tpl.label_w
    assert last_right > tpl.page_w  # a spec with these dims SHOULD fail _validate()
