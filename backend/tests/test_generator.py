"""The seed generator builds the expected topology shape."""

from floorcast.services.generator import GeneratorConfig, generate_floor


def test_topology_counts(settings):
    gcfg = GeneratorConfig(buildings=2, suites_per_building=4, rows_per_suite=48,
                           positions_per_row=10, fill_ratio=0.12, seed=1)
    floor = generate_floor(settings, gcfg)

    assert len(floor.buildings) == 2
    for b in floor.buildings:
        assert len(b.suites) == 4
        for s in b.suites:
            assert len(s.rows) == 48
            for row in s.rows:
                assert len(row.positions) == 10


def test_seeded_racks_are_existing_generations(settings):
    floor = generate_floor(settings, GeneratorConfig(seed=7))
    assert floor.racks, "expected some racks to be generated"
    gens = {r.generation for r in floor.racks}
    assert gens <= {2023, 2024}  # seed only the existing fleet, never 2025


def test_deterministic(settings):
    a = generate_floor(settings, GeneratorConfig(seed=99))
    b = generate_floor(settings, GeneratorConfig(seed=99))
    assert len(a.racks) == len(b.racks)
    assert [r.rack_id for r in a.racks] == [r.rack_id for r in b.racks]
