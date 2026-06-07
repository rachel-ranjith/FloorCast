"""Config loads from YAML and constraints are present (nothing hardcoded).

Anchored to the single standard catalog (config/floorcast.yaml): three diverging
replacement targets per family (-value / -max / -eff), all gen 2025, with the
2023 + 2024 fleet retirable (retire_generation_at_or_below: 2024).
"""

import os

from collections import Counter


def test_yaml_loads_rack_catalog(settings):
    assert "compute-2023" in settings.rack_catalog
    assert "ai-2025-max" in settings.rack_catalog
    assert settings.rack_catalog["ai-2025-max"].power_draw_kw == 65.0


def test_power_envelope(settings):
    assert 0 <= settings.power.headroom_pct < 1
    # usable() applies headroom
    assert settings.power.usable(1000.0) == 1000.0 * (1 - settings.power.headroom_pct)


def test_replacement_targets_are_all_2025(settings):
    targets = settings.replacement_targets()
    assert targets, "expected replacement targets in the catalog"
    assert all(spec.is_replacement_target for spec in targets.values())
    assert all(spec.generation == 2025 for spec in targets.values())


def test_three_targets_per_family(settings):
    # The catalog deliberately exposes a value / max / efficiency target per
    # family so the value-aware objectives have something to choose between.
    by_family = Counter(spec.family for spec in settings.replacement_targets().values())
    assert by_family == {"compute": 3, "storage": 3, "ai": 3}


def test_catalog_metadata_fields_load_and_are_consistent(settings):
    # cost / compute_capability / producer load and validate on every entry.
    for name, spec in settings.rack_catalog.items():
        assert spec.cost > 0, name
        assert spec.compute_capability > 0, name
        assert isinstance(spec.producer, str) and spec.producer, name

    cat = settings.rack_catalog
    assert cat["ai-2025-max"].producer == "Cirrus AI"
    # a 2025 target out-computes (and out-costs) the fleet it replaces
    assert cat["compute-2023"].compute_capability < cat["compute-2025-max"].compute_capability
    assert cat["compute-2023"].cost < cat["compute-2025-max"].cost
    # within a generation: storage < compute < ai on raw capability
    assert (
        cat["storage-2025-max"].compute_capability
        < cat["compute-2025-max"].compute_capability
        < cat["ai-2025-max"].compute_capability
    )


def test_value_max_eff_targets_diverge(settings):
    # The whole point of the catalog: per family the three argmaxes differ.
    for family in ("compute", "storage", "ai"):
        targets = {
            name: spec
            for name, spec in settings.replacement_targets().items()
            if spec.family == family
        }
        best_value = max(targets.values(), key=lambda s: s.compute_capability / s.cost)
        best_compute = max(targets.values(), key=lambda s: s.compute_capability)
        best_eff = max(targets.values(), key=lambda s: s.compute_capability / s.power_draw_kw)
        # the three picks are three different racks
        assert len({id(best_value), id(best_compute), id(best_eff)}) == 3, family
        assert best_value.producer  # sanity touch


def test_current_fleet_is_retirable_but_not_a_target(settings):
    cat = settings.rack_catalog
    for name in ("compute-2023", "storage-2023", "ai-2023",
                 "compute-2024", "storage-2024", "ai-2024"):
        assert name in cat, name
        assert cat[name].is_replacement_target is False, name
        assert settings.is_retirement_candidate(name) is True, name


def test_rackspec_requires_new_fields():
    # cost / compute_capability / producer are required: omitting them is an error.
    import pytest
    from pydantic import ValidationError

    from config.settings import RackSpec

    with pytest.raises(ValidationError):
        RackSpec(family="compute", generation=2025, power_draw_kw=10.0)


def test_retirement_candidate(settings):
    assert settings.is_retirement_candidate("compute-2023") is True
    assert settings.is_retirement_candidate("compute-2024") is True   # retire<=2024
    assert settings.is_retirement_candidate("ai-2025-max") is False   # a 2025 target
    assert settings.is_retirement_candidate("does-not-exist") is False


def test_env_override(monkeypatch):
    from config.settings import get_settings

    monkeypatch.setenv("FLOORCAST_POWER__HEADROOM_PCT", "0.35")
    get_settings.cache_clear()
    cfg = get_settings()
    assert cfg.power.headroom_pct == 0.35
    get_settings.cache_clear()
    os.environ.pop("FLOORCAST_POWER__HEADROOM_PCT", None)
