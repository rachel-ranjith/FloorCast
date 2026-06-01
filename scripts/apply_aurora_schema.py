#!/usr/bin/env python
"""Apply src/floorcast/db/aurora/schema.sql to the configured Aurora database.

Usage:
    python scripts/apply_aurora_schema.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import text

from config.settings import get_settings
from floorcast.db.aurora.session import get_session

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src/floorcast/db/aurora/schema.sql"


def main() -> int:
    settings = get_settings()
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    print(f"Applying schema to {settings.aurora.dsn.split('@')[-1]} ...")
    with get_session(settings) as session:
        # Execute as a single batch; statements are guarded with IF NOT EXISTS.
        session.execute(text(sql))
        session.commit()
    print("✓ schema applied")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"✗ failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
