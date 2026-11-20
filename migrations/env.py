"""Alembic environment. DB path from $REGINALD_DB_PATH (set by `reginald db migrate`)."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import context
from sqlalchemy import URL, create_engine

from reginald.db.schema import Base
from reginald.db.session import sqlite_url

target_metadata = Base.metadata


def _url() -> URL:
    db_path = os.environ.get("REGINALD_DB_PATH")
    if not db_path:
        raise RuntimeError("REGINALD_DB_PATH not set (use 'reginald db migrate')")
    return sqlite_url(Path(db_path).expanduser())


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
