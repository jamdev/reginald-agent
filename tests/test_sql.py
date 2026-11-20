import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from reginald.db.schema import Topic
from reginald.services.sql import SqlService, is_read_only


@pytest.fixture
def sql(sessions, config) -> SqlService:
    return SqlService(sessions, config)


# -- read/write classification ------------------------------------------------


@pytest.mark.parametrize("statement", [
    "select * from topics",
    "  SELECT 1",
    "with x as (select 1) select * from x",
    "pragma table_info(topics)",
    "explain query plan select * from topics",
    "-- a leading comment\nselect * from topics",
])
def test_reads_are_classified_read_only(statement):
    assert is_read_only(statement)


@pytest.mark.parametrize("statement", [
    "update topics set active=0",
    "delete from topics",
    "insert into topics (uuid, name, slug) values ('u','n','s')",
    "drop table topics",
    "-- fix the flag-derived rows\nupdate content_events set kind='brief_created'",
])
def test_writes_are_classified_as_writes(statement):
    """A comment line must not disguise a write — the reason classification
    skips comments rather than looking at the first line."""
    assert not is_read_only(statement)


# -- reads --------------------------------------------------------------------


def test_read_returns_columns_and_rows(sql, topics):
    result = sql.run("select name, slug from topics order by name")
    assert result.columns == ["name", "slug"]
    assert result.write is False
    assert ["AI and Algorithmic Systems", "ai-and-algorithmic-systems"] in result.rows
    assert result.backup_path is None


def test_read_respects_limit_and_flags_truncation(sql, topics):
    result = sql.run("select name from topics", limit=2)
    assert len(result.rows) == 2
    assert result.rowcount == 2
    assert result.truncated is True


def test_a_read_inside_the_limit_is_not_truncated(sql, topics):
    result = sql.run("select name from topics", limit=500)
    assert result.truncated is False


def test_a_read_takes_no_backup(sql, topics, config):
    sql.run("select 1")
    assert list(config.db_path.parent.glob("*.backup-*.db")) == []


# -- writes -------------------------------------------------------------------


def test_write_applies_and_reports_rowcount(sql, sessions, topics):
    result = sql.run("update topics set active=0 where slug='payments-and-e-money'")
    assert result.write is True
    assert result.rowcount == 1
    with sessions() as session:
        row = session.scalar(select(Topic).where(Topic.slug == "payments-and-e-money"))
        assert row.active is False


def test_write_backs_the_database_up_first(sql, topics, config):
    result = sql.run("update topics set active=0")
    backups = list(config.db_path.parent.glob("*.backup-*.db"))
    assert len(backups) == 1
    assert result.backup_path == str(backups[0])


def test_a_multi_statement_script_is_rejected_whole(sql, sessions, topics):
    """The driver takes one statement per call, so a semicolon-separated
    script raises instead of applying its first half."""
    with pytest.raises(SQLAlchemyError):
        sql.run("update topics set active=0; drop table topics")
    with sessions() as session:
        rows = session.scalars(select(Topic)).all()
        assert rows                                          # table intact
        # …and the first half did not land either (sentinel topics are
        # inactive by design, so check a real one).
        assert session.scalar(
            select(Topic).where(Topic.slug == "payments-and-e-money")
        ).active is True


def test_a_failed_write_rolls_back(sql, sessions, topics):
    with pytest.raises(SQLAlchemyError):
        sql.run("update topics set nope=1")
    with sessions() as session:
        assert session.scalars(select(Topic)).all()


def test_empty_statement_is_rejected(sql):
    with pytest.raises(ValueError, match="empty statement"):
        sql.run("   ;  ")
