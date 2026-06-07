"""Central configuration loader for Floorcast.

Layering (later wins):
  1. Defaults baked into the pydantic models below.
  2. The YAML file at FLOORCAST_CONFIG_FILE (default: config/floorcast.yaml).
  3. Environment variables prefixed FLOORCAST_ (nested via "__").

Usage:
    from config.settings import get_settings
    cfg = get_settings()
    cfg.power.headroom_pct        # -> 0.20
    cfg.rack_catalog["ai-2025"].power_draw_kw

Nothing downstream should hardcode a constraint; everything reads from here.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


# --------------------------------------------------------------------------- #
# Domain config sub-models
# --------------------------------------------------------------------------- #
class RackSpec(BaseModel):
    """A single entry in the rack catalog.

    NOTE: cost / compute_capability / producer are catalog metadata only. The
    optimizer does NOT read them (it touches power_draw_kw, family, generation,
    is_replacement_target) — they exist for catalog browsing/filtering and for a
    future cost/compute-aware solver stage.
    """

    family: str
    generation: int
    power_draw_kw: float = Field(gt=0)
    rack_units: int = 42
    is_replacement_target: bool = False
    # ---- catalog metadata (inert to the current solver) ----
    cost: float = Field(gt=0)  # capital cost of the rack, in GBP (£)
    compute_capability: float = Field(gt=0)  # relative performance metric
    producer: str  # vendor / manufacturer name, for filtering


class PowerConfig(BaseModel):
    headroom_pct: float = Field(0.20, ge=0, lt=1)
    building_capacity_mw: float = Field(6.0, gt=0)
    suite_capacity_mw: float = Field(1.5, gt=0)
    row_capacity_kw: float = Field(90.0, gt=0)

    def usable(self, capacity: float) -> float:
        """Capacity remaining after the configured headroom carve-out."""
        return capacity * (1.0 - self.headroom_pct)


class CalendarConfig(BaseModel):
    """Optional real-calendar date range for the plan.

    When all four fields are set, plan steps map to consecutive calendar months
    from start..end inclusive (and gain standard fiscal quarters). When any is
    None the optimizer falls back to abstract horizon_months — identical to the
    pre-calendar behaviour.
    """

    start_year: int | None = None
    start_month: int | None = Field(default=None, ge=1, le=12)
    end_year: int | None = None
    end_month: int | None = Field(default=None, ge=1, le=12)

    @property
    def is_active(self) -> bool:
        return None not in (
            self.start_year,
            self.start_month,
            self.end_year,
            self.end_month,
        )


class BudgetCap(BaseModel):
    """A single "no more than `cap` £ per `period`" rule.

    period:
      total   -> one cap over the whole horizon         (no calendar needed)
      month   -> the cap applies to EACH month          (no calendar needed)
      quarter -> the cap applies to EACH fiscal quarter  (requires a calendar)
      year    -> the cap applies to EACH calendar year   (requires a calendar)
    Rules stack: set several at once (e.g. total + quarter + month).
    """

    period: Literal["total", "year", "quarter", "month"]
    cap: float = Field(gt=0)


class CountConstraint(BaseModel):
    """Optional bounds on how many swaps install a given target rack type.

    Both bounds are non-negative integers and optional; set either, both, or
    neither. `min` can make the model infeasible (if it can't be met); `max`
    alone never can (the solver can always install fewer). min > max is rejected
    at solve time with a clear error.
    """

    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)


class OptimizerConfig(BaseModel):
    horizon_months: int = Field(12, gt=0, le=60)
    max_swaps_per_month: int = Field(8, gt=0)
    max_swaps_per_suite_per_month: int = Field(3, gt=0)
    swap_cost_per_rack: float = 1.0
    deferral_penalty_per_month: float = 0.15
    # Soft row-tier headroom penalty per kW-month of overage (see floorcast.yaml).
    row_overage_penalty: float = Field(1000.0, ge=0)
    solver_time_limit_seconds: int = Field(60, gt=0)
    retire_generation_at_or_below: int = 2023
    # ---- Stage 2: selectable objective + optional total budget constraint ----
    # Which objective the solver optimizes. "maximize_racks_modernized" is the
    # default and reproduces the historical completion-reward behaviour. The
    # other names are recognised but not yet implemented (stage stubs).
    objective: str = "maximize_racks_modernized"
    # ---- Stage 4: value-aware sequencing (time-weighting) ----
    # Per-month retention FACTOR applied to objective value: value delivered in
    # month t is worth time_discount^(t-1) of month-1 value, so earlier delivery
    # scores higher. 1.0 = no discounting (INERT — the default, preserves the
    # historical results); 0.95 = a mild ~5%/month preference for sooner.
    # (A "rate" framing of r=5%/month is just factor = 1 - r = 0.95.)
    time_discount: float = Field(1.0, gt=0, le=1)
    # ---- Stage 3: calendar ----
    calendar: CalendarConfig = CalendarConfig()
    # ---- Budget caps: stackable "no more than £cap per period" rules ----
    # One unified mechanism (period in total/year/quarter/month). Empty => no
    # budget constraints (inert; default unchanged). Example:
    #   [{"period": "total", "cap": 2_000_000}, {"period": "quarter", "cap": 600_000}]
    budget_caps: list[BudgetCap] = []
    # Convenience SUGAR over budget_caps, kept for back-compat with existing
    # config/API/tests. budget_cap == a {period: total} rule;
    # per_quarter_budget_cap == a {period: quarter} rule. None => not added.
    # See all_budget_caps() — these are merged into the unified rule list.
    budget_cap: float | None = Field(default=None, gt=0)
    per_quarter_budget_cap: float | None = Field(default=None, gt=0)
    # ---- Count constraints: per-target-type "at least N" / "at most N" swaps ----
    # Keyed by target rack_type, each with optional min and/or max. Stackable
    # linear constraints that compose with any objective/budget/calendar. Empty
    # => no count constraints (inert; default unchanged). Example:
    #   {"ai-2025-max": {"min": 10}, "compute-2025-value": {"min": 5, "max": 50}}
    count_constraints: dict[str, CountConstraint] = {}

    def all_budget_caps(self) -> list[BudgetCap]:
        """Unified budget rules = budget_caps + the back-compat scalar fields.

        budget_cap -> a {period: total} rule; per_quarter_budget_cap -> a
        {period: quarter} rule. If both a scalar and an equivalent list entry are
        set, both constraints are added (the tighter binds) — harmless.
        """
        rules = list(self.budget_caps)
        if self.budget_cap is not None:
            rules.append(BudgetCap(period="total", cap=self.budget_cap))
        if self.per_quarter_budget_cap is not None:
            rules.append(BudgetCap(period="quarter", cap=self.per_quarter_budget_cap))
        return rules


class DynamoConfig(BaseModel):
    table_name: str = "floorcast"
    gsi_power: str = "gsi-power"
    gsi_generation: str = "gsi-generation"


class CorsConfig(BaseModel):
    """Browser CORS policy for the API.

    Defaults cover the usual frontend dev servers (Next.js :3000, Vite :5173).
    Override the full list via FLOORCAST_CORS__ALLOW_ORIGINS (a JSON array), e.g.
        FLOORCAST_CORS__ALLOW_ORIGINS='["https://app.example.com"]'
    """

    allow_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]
    allow_credentials: bool = True
    allow_methods: list[str] = ["*"]
    allow_headers: list[str] = ["*"]


class AuroraConfig(BaseModel):
    dsn: str = "postgresql+psycopg://floorcast:floorcast@localhost:5432/floorcast"
    schema_: str = Field("public", alias="schema")
    pool_size: int = 5
    max_overflow: int = 10

    model_config = SettingsConfigDict(populate_by_name=True)


# --------------------------------------------------------------------------- #
# YAML settings source — lower priority than env, higher than defaults.
# --------------------------------------------------------------------------- #
class YamlSettingsSource(PydanticBaseSettingsSource):
    """Loads values from the YAML file named by FLOORCAST_CONFIG_FILE."""

    def get_field_value(self, field, field_name):  # pragma: no cover - unused
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return _load_yaml(_resolve_config_path())


# --------------------------------------------------------------------------- #
# Root settings
# --------------------------------------------------------------------------- #
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FLOORCAST_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Priority (first wins): explicit init kwargs > env > .env > YAML > secrets.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlSettingsSource(settings_cls),
            file_secret_settings,
        )

    env: str = "local"
    config_file: str = "config/floorcast.yaml"
    aws_region: str = "us-east-1"
    dynamodb_endpoint_url: str | None = None

    dynamo: DynamoConfig = DynamoConfig()
    aurora: AuroraConfig = AuroraConfig()
    cors: CorsConfig = CorsConfig()
    power: PowerConfig = PowerConfig()
    optimizer: OptimizerConfig = OptimizerConfig()
    rack_catalog: dict[str, RackSpec] = {}

    # ---- helpers ----
    def replacement_targets(self) -> dict[str, RackSpec]:
        return {k: v for k, v in self.rack_catalog.items() if v.is_replacement_target}

    def is_retirement_candidate(self, rack_type: str) -> bool:
        spec = self.rack_catalog.get(rack_type)
        if spec is None:
            return False
        return spec.generation <= self.optimizer.retire_generation_at_or_below


def _resolve_config_path() -> Path:
    """Resolve which YAML config file to load. Precedence (first wins):

      1. FLOORCAST_CONFIG_FILE in the process environment (e.g. a shell export
         or `FLOORCAST_CONFIG_FILE=... uvicorn ...`);
      2. FLOORCAST_CONFIG_FILE in the .env file;
      3. the default config/floorcast.yaml.

    We read .env explicitly here, not just os.environ. pydantic loads .env for
    model FIELDS, but this YAML source runs BEFORE fields are resolved, so
    without this step a .env-configured path would be silently ignored and the
    default used instead (the Stage-6 config-loading bug).
    """
    value = os.environ.get("FLOORCAST_CONFIG_FILE")
    if not value:
        try:
            from dotenv import dotenv_values

            value = dotenv_values(".env").get("FLOORCAST_CONFIG_FILE")
        except Exception:  # pragma: no cover - dotenv optional / unreadable .env
            value = None
    return Path(value or "config/floorcast.yaml")


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build the merged Settings singleton.

    Precedence (high to low): env vars > .env > YAML config file > model defaults.
    The YAML is loaded by YamlSettingsSource, so env vars correctly override it.
    """
    return Settings()
