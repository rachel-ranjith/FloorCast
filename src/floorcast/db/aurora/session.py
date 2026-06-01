"""Aurora PostgreSQL session factory (sync engine, psycopg3).

DSN comes from settings (FLOORCAST_AURORA__DSN). For Lambda, reuse the engine
across invocations by keeping it module-level.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import Settings, get_settings

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def _init(settings: Settings) -> None:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(
            settings.aurora.dsn,
            pool_size=settings.aurora.pool_size,
            max_overflow=settings.aurora.max_overflow,
            pool_pre_ping=True,
            future=True,
        )
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, class_=Session)


def get_session(settings: Settings | None = None) -> Session:
    settings = settings or get_settings()
    _init(settings)
    assert _SessionLocal is not None
    return _SessionLocal()
