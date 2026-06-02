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
from typing import Any

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
    """A single entry in the rack catalog."""

    family: str
    generation: int
    power_draw_kw: float = Field(gt=0)
    rack_units: int = 42
    is_replacement_target: bool = False


class PowerConfig(BaseModel):
    headroom_pct: float = Field(0.20, ge=0, lt=1)
    building_capacity_mw: float = Field(6.0, gt=0)
    suite_capacity_mw: float = Field(1.5, gt=0)
    row_capacity_kw: float = Field(90.0, gt=0)

    def usable(self, capacity: float) -> float:
        """Capacity remaining after the configured headroom carve-out."""
        return capacity * (1.0 - self.headroom_pct)


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
        path = os.environ.get("FLOORCAST_CONFIG_FILE", "config/floorcast.yaml")
        return _load_yaml(Path(path))


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
