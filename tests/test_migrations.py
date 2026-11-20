"""The release migration must work before or after Reginald's first startup."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from reginald.db.schema import Base
from reginald.db.session import init_db, make_engine


@pytest.mark.parametrize("app_started", [False, True], ids=["empty", "app-initialized"])
def test_fresh_migrations_match_models(tmp_path, monkeypatch, app_started):
    db = tmp_path / "release candidate?1.db"
    if app_started:
        init_db(db)
    monkeypatch.setenv("REGINALD_DB_PATH", str(db))
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    monkeypatch.chdir(tmp_path)

    # A table added to the ORM in a later release must never appear in 0001.
    future_table = sa.Table(
        "future_release_table", Base.metadata, sa.Column("id", sa.Integer, primary_key=True)
    )
    try:
        command.upgrade(config, "0001")
    finally:
        Base.metadata.remove(future_table)

    command.upgrade(config, "head")
    assert db.is_file()
    with make_engine(db).connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_server_default": True})
        head = ScriptDirectory.from_config(config).get_current_head()
        assert context.get_current_revision() == head
        assert compare_metadata(context, Base.metadata) == []
