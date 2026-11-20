"""SqlService — the admin escape hatch onto the database.

A deliberate hole in the "no SQL outside services" rule, for the cases a
purpose-built command does not cover: inspecting a row, correcting data a
past bug wrote, one-off surgery during an incident. The same spirit as
`vault git` — a passthrough for an operator who knows what they are doing.

Two rails, both about making a mistake survivable rather than preventing one:

  * a write is preceded by an automatic backup of the database file, and
  * a write runs under the run lock, so it cannot interleave with a live task.

Reads take neither: they are safe and must stay usable while a run is going.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ..core.config import Config
from ..core.locking import run_lock
from ..core.observability import logger, span
from ..domain.models import SqlResult

# Statements that only read. Anything else is treated as a write — the
# conservative direction, since the cost of being wrong is a needless backup.
READ_ONLY_PREFIXES = ("select", "with", "pragma", "explain")


@span("db.backup", log_when_disabled=False)
def backup_database(db_path: Path, to: Path | None = None) -> Path:
    """Copy the SQLite database via its own backup API (consistent under
    concurrent readers, unlike a file copy)."""
    import sqlite3

    # Local civil date: the backup filename should match the operator's day.
    dest = to or db_path.with_suffix(f".backup-{date.today().isoformat()}.db")  # noqa: DTZ011
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return dest


def is_read_only(statement: str) -> bool:
    """Whether a statement only reads. Comment lines are skipped so a pasted
    `-- fix the flag-derived rows\\nUPDATE …` is not mistaken for a read."""
    for line in statement.strip().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        return stripped.lower().startswith(READ_ONLY_PREFIXES)
    return True  # nothing but comments: harmless


class SqlService:
    def __init__(self, session_factory: sessionmaker[Session], config: Config):
        self._sessions = session_factory
        self._config = config

    def run(self, statement: str, limit: int = 200) -> SqlResult:
        """Execute one statement. Reads return up to `limit` rows (flagging
        truncation); writes report the affected row count.

        The sqlite3 driver executes exactly one statement per call and raises
        on a second, so a semicolon-separated script is rejected rather than
        half-applied.
        """
        statement = statement.strip().rstrip(";")
        if not statement:
            raise ValueError("empty statement")

        if is_read_only(statement):
            with self._sessions() as session:
                return self._read(session, statement, limit)

        backup = backup_database(self._config.db_path)
        logger.warning("sql write: %s (backup at %s)", statement, backup)
        with run_lock(self._config.lock_path), self._sessions.begin() as session:
            result = session.execute(text(statement))
            return SqlResult(
                statement=statement,
                write=True,
                rowcount=result.rowcount,
                backup_path=str(backup),
            )

    @staticmethod
    def _read(session: Session, statement: str, limit: int) -> SqlResult:
        result = session.execute(text(statement))
        columns = list(result.keys())
        rows = result.fetchmany(limit + 1)  # one extra: detect truncation
        truncated = len(rows) > limit
        return SqlResult(
            statement=statement,
            columns=columns,
            rows=[[_cell(v) for v in row] for row in rows[:limit]],
            rowcount=min(len(rows), limit),
            truncated=truncated,
        )


def _cell(value):
    """JSON-safe scalars: --json output is a contract, and sqlite hands back
    datetimes and bytes that json.dumps will not take."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value
