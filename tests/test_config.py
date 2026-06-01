"""Config loads from YAML and constraints are present (nothing hardcoded)."""

import os


def test_yaml_loads_rack_catalog(settings):
    assert "compute-2023" in settings.rack_catalog
    assert "ai-2025" in settings.rack_catalog
    assert settings.rack_catalog["ai-2025"].power_draw_kw == 48.0


def test_power_envelope(settings):
    assert 0 <= settings.power.headroom_pct < 1
    # usable() applies headroom
    assert settings.power.usable(1000.0) == 1000.0 * (1 - settings.power.headroom_pct)


def test_replacement_targets_only_2025(settings):
    targets = settings.replacement_targets()
    assert all(spec.is_replacement_target for spec in targets.values())
    assert all(spec.generation == 2025 for spec in targets.values())


def test_retirement_candidate(settings):
    assert settings.is_retirement_candidate("compute-2023") is True
    assert settings.is_retirement_candidate("compute-2025") is False


def test_env_override(monkeypatch):
    from config.settings import get_settings

    monkeypatch.setenv("FLOORCAST_POWER__HEADROOM_PCT", "0.35")
    get_settings.cache_clear()
    cfg = get_settings()
    assert cfg.power.headroom_pct == 0.35
    get_settings.cache_clear()
    os.environ.pop("FLOORCAST_POWER__HEADROOM_PCT", None)
