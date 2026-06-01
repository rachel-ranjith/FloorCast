-- =============================================================================
-- Floorcast — Aurora PostgreSQL schema
-- -----------------------------------------------------------------------------
-- Stores optimization runs, the schedules they produce, what-if scenarios, and
-- point-in-time history. Live rack/topology state lives in DynamoDB; this DB is
-- the system of record for everything the optimizer *decides* and *remembers*.
--
-- Apply with: scripts/apply_aurora_schema.py  (or psql -f this file)
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid()

-- --------------------------------------------------------------------------- --
-- Scenarios: a named set of config overrides for what-if analysis.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             TEXT NOT NULL,
    description      TEXT,
    -- JSON patch applied on top of config/floorcast.yaml for this scenario.
    config_overrides JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_baseline      BOOLEAN NOT NULL DEFAULT FALSE,
    created_by       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------- --
-- Optimization runs: one row per execution of the ILP engine.
-- --------------------------------------------------------------------------- --
CREATE TYPE run_status AS ENUM (
    'pending', 'running', 'optimal', 'feasible', 'infeasible', 'failed'
);

CREATE TABLE IF NOT EXISTS optimization_runs (
    run_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                 TEXT NOT NULL,
    scenario_id          UUID REFERENCES scenarios(scenario_id) ON DELETE SET NULL,
    status               run_status NOT NULL DEFAULT 'pending',

    horizon_months       INTEGER NOT NULL,
    objective_value      DOUBLE PRECISION,
    total_swaps          INTEGER,
    solver_wall_time_ms  INTEGER,

    -- Exact, fully-resolved config the solver used (YAML + scenario overrides
    -- + env). Snapshotted so a run is always reproducible and auditable.
    resolved_config      JSONB NOT NULL,
    -- Snapshot of the live fleet (from DynamoDB) the run optimized against.
    fleet_snapshot       JSONB,

    error_message        TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_runs_scenario ON optimization_runs(scenario_id);
CREATE INDEX IF NOT EXISTS idx_runs_status   ON optimization_runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_created  ON optimization_runs(created_at DESC);

-- --------------------------------------------------------------------------- --
-- Schedules: the 12-month plan produced by a run (1:1 with a successful run).
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        UUID NOT NULL REFERENCES optimization_runs(run_id) ON DELETE CASCADE,
    horizon_months INTEGER NOT NULL,
    total_swaps   INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id)
);

-- --------------------------------------------------------------------------- --
-- Schedule items: one row per scheduled rack swap.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS schedule_items (
    item_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id    UUID NOT NULL REFERENCES schedules(schedule_id) ON DELETE CASCADE,

    month          INTEGER NOT NULL CHECK (month >= 1),
    position_id    TEXT NOT NULL,
    row_id         TEXT,
    suite_id       TEXT NOT NULL,
    building_id    TEXT NOT NULL,

    from_rack_type TEXT NOT NULL,
    to_rack_type   TEXT NOT NULL,
    from_power_kw  DOUBLE PRECISION NOT NULL,
    to_power_kw    DOUBLE PRECISION NOT NULL,

    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_items_schedule ON schedule_items(schedule_id);
CREATE INDEX IF NOT EXISTS idx_items_month    ON schedule_items(schedule_id, month);
CREATE INDEX IF NOT EXISTS idx_items_suite    ON schedule_items(suite_id);

-- --------------------------------------------------------------------------- --
-- Per-month / per-tier power utilization captured at solve time, for charts
-- and for verifying headroom constraints held across the whole plan.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS power_utilization (
    util_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id  UUID NOT NULL REFERENCES schedules(schedule_id) ON DELETE CASCADE,
    month        INTEGER NOT NULL CHECK (month >= 1),
    tier         TEXT NOT NULL,            -- 'building' | 'suite' | 'row'
    tier_id      TEXT NOT NULL,
    load_kw      DOUBLE PRECISION NOT NULL,
    capacity_kw  DOUBLE PRECISION NOT NULL,
    utilization  DOUBLE PRECISION NOT NULL  -- load_kw / capacity_kw
);

CREATE INDEX IF NOT EXISTS idx_util_schedule ON power_utilization(schedule_id, month);
CREATE INDEX IF NOT EXISTS idx_util_tier     ON power_utilization(tier, tier_id);

-- --------------------------------------------------------------------------- --
-- Fleet history: append-only log of rack-state changes (installs, swaps,
-- decommissions) mirrored out of DynamoDB for long-term analytics.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS rack_history (
    history_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rack_id       TEXT NOT NULL,
    position_id   TEXT NOT NULL,
    suite_id      TEXT NOT NULL,
    building_id   TEXT NOT NULL,
    event_type    TEXT NOT NULL,           -- 'install' | 'swap_out' | 'swap_in' | 'decommission'
    rack_type     TEXT,
    power_draw_kw DOUBLE PRECISION,
    run_id        UUID REFERENCES optimization_runs(run_id) ON DELETE SET NULL,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_history_rack     ON rack_history(rack_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_position ON rack_history(position_id, occurred_at DESC);
