"""Database engines and sessions.

Two engines:
  * main  - assets, telemetry (TimescaleDB hypertable on PostgreSQL), records,
            balances, predictions, investigations, reviews, audit
  * truth - simulator ground truth and fault injections. Only the simulation
            lab and the evaluation pipeline use it; the diagnostic path never does.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings

log = logging.getLogger("oilintel.db")


def _make_engine(url: str):
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30}, pool_pre_ping=True)

        @event.listens_for(eng, "connect")
        def _pragma(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
        return eng
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)


_s = get_settings()
engine = _make_engine(_s.database_url)
truth_engine = _make_engine(_s.truth_database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
TruthSession = sessionmaker(bind=truth_engine, autoflush=False, expire_on_commit=False)


def migrate(eng=None) -> None:
    """Bring the main database to the latest Alembic revision (database/migrations).

    A database created before migrations existed (tables present, no alembic_version) is
    stamped at the initial revision, which describes exactly that schema, then upgraded."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    root = Path(__file__).resolve().parents[4]
    cfg = Config()
    cfg.set_main_option("script_location", str(root / "database" / "migrations"))
    with (eng or engine).begin() as conn:
        cfg.attributes["connection"] = conn
        tables = set(inspect(conn).get_table_names())
        if "alembic_version" not in tables and "telemetry" in tables:
            command.stamp(cfg, "0001")
        command.upgrade(cfg, "head")


def init_db() -> None:
    from app.models.orm import TruthBase
    migrate()
    # simulator ground truth is simulation-only scratch data: created, not versioned
    TruthBase.metadata.create_all(truth_engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as c:
            try:
                c.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
                c.execute(text("SELECT create_hypertable('telemetry', 'event_timestamp', if_not_exists => TRUE, "
                               "migrate_data => TRUE)"))
                c.execute(text("SELECT create_hypertable('balance_results', 't1', if_not_exists => TRUE, "
                               "migrate_data => TRUE)"))
                log.info("TimescaleDB hypertables ready")
            except Exception as e:  # plain PostgreSQL without the extension
                log.warning("TimescaleDB not available (%s); using plain tables", e)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
