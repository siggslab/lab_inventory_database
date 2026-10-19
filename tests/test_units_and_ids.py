"""Unit tests for unit arithmetic and id minting."""

from __future__ import annotations

import pytest

from inventory_mcp import ids, units


class TestUnits:
    def test_volume_converts_to_ul(self):
        assert units.to_canonical("volume", 1.0, "ml") == (1000.0, "ul")
        assert units.to_canonical("volume", 2.0, "ul") == (2.0, "ul")

    def test_every_metric_prefix_lands_where_the_prefix_says(self):
        """The prefix table is assembled by hand after a generated one, so pin it.

        ``_prefixed`` walks its prefix tuple in order and multiplies by 1000 each
        step, which is right for m->u->n only if the caller listed them ascending --
        and it isn't. The result used to be ``nl = 1e12``, accepted without complaint
        because an unknown unit raises while a wrong factor just quietly converts.
        """
        ladder = {"l": 1e6, "ml": 1e3, "ul": 1e0, "nl": 1e-3, "pl": 1e-6}
        for unit, factor in ladder.items():
            assert units.to_canonical("volume", 1.0, unit) == (factor, "ul"), unit
        for unit, factor in {"g": 1e9, "mg": 1e6, "ug": 1e3, "ng": 1e0, "pg": 1e-3}.items():
            assert units.to_canonical("mass", 1.0, unit) == (factor, "ng"), unit

    def test_micro_sign_variants_are_equivalent(self):
        assert units.to_canonical("volume", 5.0, "µl") == units.to_canonical("volume", 5.0, "ul")

    def test_mass_and_concentration_are_separate_dimensions(self):
        assert units.to_canonical("mass", 1.0, "ug") == (1000.0, "ng")
        # ug/ml and ng/ul are the same concentration -- this must hold or every
        # historical record becomes incomparable.
        assert units.to_canonical("concentration_mass", 1.0, "ug/ml") == (1.0, "ng/ul")

    def test_wrong_dimension_is_rejected_not_passed_through(self):
        with pytest.raises(units.UnitError):
            units.to_canonical("volume", 49.0, "ng/ul")

    def test_unknown_unit_lists_alternatives(self):
        with pytest.raises(units.UnitError) as e:
            units.to_canonical("mass", 1.0, "furlongs")
        assert "Known:" in str(e.value)

    def test_unitless_quantities_accept_their_placeholders(self):
        assert units.is_known_unit("cell_count", "")
        assert units.is_known_unit("viability", "%")
        value, canon = units.to_canonical("cell_count", 5.3, "")
        assert (value, canon) == (5.3, "cells")

    def test_compatible_quantity_hint(self):
        assert "volume" in units.compatible_quantity("ml")


class TestIds:
    def test_round_trip(self):
        value = ids.new_id("smp")
        assert ids.parse_id(value) == ("smp", value.split("_")[1])

    def test_ambiguous_characters_absent(self):
        for _ in range(200):
            value = ids.new_id("smp")
            suffix = value.split("_")[1]
            assert not set(suffix) & set("ilou"), value

    def test_rejects_human_labels(self):
        assert ids.parse_id("GML001631") is None
        assert ids.parse_id("-70C Misc") is None
        assert ids.parse_id("smp_short") is None
        assert ids.looks_like_human_label("GML001631")

    def test_prefix_check(self):
        value = ids.new_id("kit")
        assert ids.is_valid_id(value, "kit")
        assert not ids.is_valid_id(value, "smp")
