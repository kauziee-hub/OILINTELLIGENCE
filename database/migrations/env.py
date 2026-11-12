"""Alembic environment for the main application database.

The simulator ground-truth database (TruthBase) is simulation-only scratch data and is
created directly by the backend; it is not versioned here.
"""
from __future__ import annotations

from alembic import context

from app.models.orm import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    conn = config.attributes.get("connection")       # supplied by app.core.db.init_db
    if conn is not None:
        _run(conn)
        return
    from app.core.db import engine                   # CLI: same DATABASE_URL as the backend
    with engine.begin() as c:
        _run(c)


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      render_as_batch=connection.dialect.name == "sqlite", compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    from app.core.config import get_settings
    context.configure(url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
