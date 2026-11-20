"""Engine + session factory. SQLite in WAL mode; single writer by design."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import URL, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .schema import Base


def sqlite_url(db_path: Path) -> URL:
    """Pass the filename literally so characters like '?' stay part of the path."""
    return URL.create("sqlite", database=str(db_path))


def make_engine(db_path: Path) -> Engine:
    db_path = db_path.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(sqlite_url(db_path), future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # pragma: no cover
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def make_session_factory(db_path: Path) -> sessionmaker[Session]:
    engine = make_engine(db_path)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(db_path: Path) -> None:
    """Create all tables (idempotent). Alembic owns migrations thereafter."""
    Base.metadata.create_all(make_engine(db_path))
