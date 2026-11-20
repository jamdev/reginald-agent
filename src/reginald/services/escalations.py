"""EscalationService — stewardship of Escalations/ and its DB mirror.

The vault file is the escalation; these rows are bookkeeping. All repairs
here are frontmatter or file-location changes, never prose. The repair
matrix enforces one invariant in both directions:

    status: pending      <=>  file lives in Escalations/
    any other status     <=>  file lives in Escalations/Resolved/

so a human can change either the status or the location and the other
follows. File moves are code, not agent: the agent toolset cannot delete
files, so it sets `status:` and this service moves.

Numbers (ESC-NNN) are assigned here and only here — contributors raising
escalations from their own clones omit `number:`, which is what keeps two
clones from minting the same one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..core.config import Config
from ..core.git import VaultGit
from ..core.observability import logger
from ..db.schema import Escalation
from ..publisher.frontmatter import parse_frontmatter, upsert_frontmatter_key

ESCALATIONS_DIR = "Escalations"
RESOLVED_DIR = "Escalations/Resolved"
STATUSES = ("pending", "actioned-by-agent", "actioned-by-human", "ignored")
LEGACY_FILE = "Setup/Escalations.md"
MIGRATED_MARKER = "<!-- migrated to Escalations/ -->"

_LEGACY_ENTRY = re.compile(r"^### (ESC-(\d+)) — (.+)$", re.MULTILINE)
_LEGACY_DATE = re.compile(r"\*\*(?:Resolution|Update) \((\d{4}-\d{2}-\d{2})")


class EscalationError(RuntimeError):
    pass


@dataclass
class EscalationSyncReport:
    scanned: int = 0
    repaired: list[str] = field(default_factory=list)  # human-readable repair lines
    adopted: int = 0            # new vault files mirrored into the DB
    missing: int = 0            # DB rows whose file has vanished
    anomalies: list[str] = field(default_factory=list)  # unknown statuses etc.

    @property
    def changed(self) -> bool:
        return bool(self.repaired) or self.adopted > 0


@dataclass
class MigrationReport:
    dry_run: bool = False
    already_migrated: bool = False
    entries: int = 0
    open_entries: int = 0
    resolved_entries: int = 0
    files: list[str] = field(default_factory=list)


class EscalationService:
    def __init__(self, session_factory: sessionmaker[Session], config: Config):
        self._sessions = session_factory
        self._config = config
        self._git = VaultGit(
            config.vault_path,
            author_name=config.git.author_name,
            author_email=config.git.author_email,
        )

    # -- sync: the repair matrix ------------------------------------------

    def sync(self) -> EscalationSyncReport:
        """Reconcile vault <-> DB and enforce the status/folder invariant.

        Idempotent; leaves its writes uncommitted (the run pipeline's commit,
        or the CLI wrapper, owns the commit).
        """
        report = EscalationSyncReport()
        vault = self._config.vault_path
        root = vault / ESCALATIONS_DIR
        if not root.is_dir():
            return report

        seen_uuids: set[str] = set()
        with self._sessions.begin() as session:
            # number -> uuid, seeded from the DB and extended as files are seen.
            # A file whose `number:` is already held by a *different* escalation
            # — a duplicate in the vault (the legacy log had two ESC-006), or a
            # number the DB assigned to another escalation on a different clone
            # or run — is reassigned rather than crashing on the UNIQUE(number)
            # constraint.
            claimed: dict[int, str] = {
                row.number: row.uuid
                for row in session.scalars(select(Escalation)).all()
                if row.number is not None
            }
            for path in sorted(root.rglob("*.md")):
                rel = str(path.relative_to(vault))
                if path.name == "Index.md":
                    continue
                report.scanned += 1
                self._sync_file(session, path, rel, report, seen_uuids, claimed)

            # rows whose file has gone (and no file re-adopted their uuid)
            for row in session.scalars(select(Escalation)).all():
                if row.uuid in seen_uuids:
                    continue
                if not (vault / row.file_path).exists():
                    if row.missing_since is None:
                        row.missing_since = datetime.now(UTC)
                        report.repaired.append(f"{row.file_path}: file missing — flagged")
                    report.missing += 1

        if report.changed:
            logger.info(
                "escalations sync: %d repair(s), %d adopted", len(report.repaired), report.adopted
            )
        return report

    def _sync_file(
        self,
        session: Session,
        path: Path,
        rel: str,
        report: EscalationSyncReport,
        seen_uuids: set[str],
        claimed: dict[int, str],
    ) -> None:
        vault = self._config.vault_path
        text = path.read_text()
        fm = parse_frontmatter(text)
        today = date.today().isoformat()  # noqa: DTZ011 — operator's calendar day

        # uuid: every note carries one; fresh if missing or invalid
        file_uuid = str(fm.get("uuid") or "")
        try:
            UUID(file_uuid)
        except ValueError:
            file_uuid = str(uuid4())
            text = upsert_frontmatter_key(text, "uuid", file_uuid)
            report.repaired.append(f"{rel}: uuid assigned")

        status = str(fm.get("status") or "")
        if not status:
            status = "pending"
            text = upsert_frontmatter_key(text, "status", status)
            report.repaired.append(f"{rel}: status defaulted to pending")
        elif status not in STATUSES:
            report.anomalies.append(f"{rel}: unknown status '{status}' — left untouched")

        raised = str(fm.get("raised_date") or "")
        if not raised:
            raised = self._git.first_added_date(rel) or today
            text = upsert_frontmatter_key(text, "raised_date", raised)
            report.repaired.append(f"{rel}: raised_date backfilled ({raised})")

        row = session.scalar(select(Escalation).where(Escalation.uuid == file_uuid))
        if row is None:
            row = session.scalar(select(Escalation).where(Escalation.file_path == rel))

        # status_changed_date: stamp on observed transitions the author didn't
        # stamp themselves; a human-set date always wins
        changed_date = str(fm.get("status_changed_date") or "")
        if row is not None and status in STATUSES and row.status != status:
            if not changed_date or changed_date == (row.status_changed_date or ""):
                changed_date = today
                text = upsert_frontmatter_key(text, "status_changed_date", changed_date)
                report.repaired.append(f"{rel}: status_changed_date stamped ({today})")
        elif not changed_date:
            changed_date = raised
            text = upsert_frontmatter_key(text, "status_changed_date", changed_date)

        # number: assigned here and only here. A missing number gets the next
        # free one; a number already claimed by a *different* escalation this
        # pass (the legacy log had two ESC-006 entries) is reassigned rather
        # than colliding on the unique constraint.
        number = fm.get("number")
        if not isinstance(number, int):
            number = self._next_number(session, claimed)
            text = upsert_frontmatter_key(text, "number", str(number))
            report.repaired.append(f"{rel}: number assigned (ESC-{number:03d})")
        elif number in claimed and claimed[number] != file_uuid:
            new_number = self._next_number(session, claimed)
            report.repaired.append(
                f"{rel}: number {number} already used — reassigned to ESC-{new_number:03d}"
            )
            number = new_number
            text = upsert_frontmatter_key(text, "number", str(number))
        claimed[number] = file_uuid

        # the invariant, both directions
        in_resolved = rel.startswith(RESOLVED_DIR + "/")
        if status in STATUSES:
            if status == "pending" and in_resolved:
                # a human moved it: moving IS acting
                status = "actioned-by-human"
                text = upsert_frontmatter_key(text, "status", status)
                changed_date = today
                text = upsert_frontmatter_key(text, "status_changed_date", changed_date)
                report.repaired.append(f"{rel}: moved to Resolved/ by hand — status follows")
            elif status != "pending" and not in_resolved:
                new_rel = f"{RESOLVED_DIR}/{path.name}"
                target = vault / new_rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if text != path.read_text():
                    path.write_text(text)
                path.rename(target)
                report.repaired.append(f"{rel}: {status} — moved to Resolved/")
                rel, path = new_rel, target

        if text != (path.read_text() if path.exists() else None):
            path.write_text(text)

        # mirror
        if row is None:
            row = Escalation(uuid=file_uuid)
            session.add(row)
            report.adopted += 1
        row.file_path = rel
        row.number = number
        row.title = str(fm.get("title") or path.stem)
        if status in STATUSES:
            row.status = status
        row.raised_date = raised
        row.status_changed_date = changed_date
        row.raised_by = str(fm.get("raised_by") or "") or row.raised_by
        row.priority = str(fm.get("priority") or "") or None
        row.missing_since = None
        row.last_synced_at = datetime.now(UTC)
        session.flush()
        seen_uuids.add(file_uuid)

    def _next_number(self, session: Session, claimed: dict[int, str] | None = None) -> int:
        db_max = 0
        for (n,) in session.execute(select(Escalation.number)).all():
            if isinstance(n, int):
                db_max = max(db_max, n)
        vault_max = 0
        root = self._config.vault_path / ESCALATIONS_DIR
        if root.is_dir():
            for path in root.rglob("*.md"):
                value = parse_frontmatter(path.read_text()).get("number")
                if isinstance(value, int):
                    vault_max = max(vault_max, value)
        claimed_max = max(claimed) if claimed else 0
        return max(db_max, vault_max, claimed_max) + 1

    # -- queries / admin ---------------------------------------------------

    def list(self, status: str | None = None) -> list[Escalation]:
        with self._sessions() as session:
            stmt = select(Escalation).order_by(Escalation.number)
            if status:
                stmt = stmt.where(Escalation.status == status)
            rows = session.scalars(stmt).all()
            session.expunge_all()
            return list(rows)

    def get(self, identifier: str) -> Escalation:
        row = self._find(identifier)
        if row is None:
            raise EscalationError(f"no escalation matching {identifier!r}")
        return row

    def set_status(self, identifier: str, status: str) -> Escalation:
        """Vault-first: write the frontmatter, then let sync move + mirror."""
        if status not in STATUSES:
            raise EscalationError(f"invalid status {status!r} (one of: {', '.join(STATUSES)})")
        row = self.get(identifier)
        path = self._config.vault_path / row.file_path
        if not path.exists():
            raise EscalationError(f"vault file missing: {row.file_path}")
        text = upsert_frontmatter_key(path.read_text(), "status", status)
        # deliberate operator action: stamp the date now, not at next sync
        text = upsert_frontmatter_key(
            text, "status_changed_date", date.today().isoformat()  # noqa: DTZ011
        )
        path.write_text(text)
        self.sync()
        return self.get(identifier)

    def _find(self, identifier: str) -> Escalation | None:
        ident = identifier.strip()
        number = None
        match = re.fullmatch(r"(?:ESC-?)?(\d+)", ident, re.IGNORECASE)
        if match:
            number = int(match.group(1))
        with self._sessions() as session:
            if number is not None:
                row = session.scalar(select(Escalation).where(Escalation.number == number))
            else:
                row = session.scalar(select(Escalation).where(Escalation.uuid == ident))
            if row is not None:
                session.expunge(row)
            return row

    # -- legacy migration --------------------------------------------------

    def migrate(self, dry_run: bool = False) -> MigrationReport:
        """One-time split of Setup/Escalations.md into per-file records.

        Idempotent: once the tombstone marker is in place, re-running only
        re-syncs the DB (which is how a fresh host DB seeds from an
        already-migrated vault).
        """
        vault = self._config.vault_path
        legacy = vault / LEGACY_FILE
        report = MigrationReport(dry_run=dry_run)
        if not legacy.exists():
            report.already_migrated = True
            if not dry_run:
                self.sync()
            return report
        text = legacy.read_text()
        if MIGRATED_MARKER in text:
            report.already_migrated = True
            if not dry_run:
                self.sync()
            return report

        open_start = text.find("\n## Open")
        resolved_start = text.find("\n## Resolved")
        for section_start, section_end, resolved in (
            (open_start, resolved_start if resolved_start > -1 else len(text), False),
            (resolved_start, len(text), True),
        ):
            if section_start < 0:
                continue
            section = text[section_start:section_end]
            matches = list(_LEGACY_ENTRY.finditer(section))
            for i, match in enumerate(matches):
                body_end = matches[i + 1].start() if i + 1 < len(matches) else len(section)
                body = section[match.end():body_end].strip().strip("-").strip()
                self._migrate_entry(
                    number=int(match.group(2)),
                    title=match.group(3).strip(),
                    body=body,
                    resolved=resolved,
                    report=report,
                    dry_run=dry_run,
                )
            report.open_entries += 0 if resolved else len(matches)
            report.resolved_entries += len(matches) if resolved else 0
        report.entries = report.open_entries + report.resolved_entries

        if not dry_run:
            fm_end = text.find("---", 3)
            frontmatter = text[: fm_end + 4] if text.startswith("---") and fm_end > 0 else ""
            legacy.write_text(
                frontmatter
                + f"\n# Escalations\n\n{MIGRATED_MARKER}\n\n"
                f"Escalations now live in [[{ESCALATIONS_DIR}/Index|Escalations/]] — "
                "one file per escalation, resolved ones under Resolved/. "
                "This file is retained so old links keep working.\n"
            )
            self.sync()
        return report

    def _migrate_entry(
        self, number: int, title: str, body: str, resolved: bool,
        report: MigrationReport, dry_run: bool,
    ) -> None:
        raised = _first(r"\*\*Raised:\*\*\s*(\d{4}-\d{2}-\d{2})", body) or "2026-01-01"
        task = _first(r"\*\*Task:\*\*\s*(.+)", body)
        priority = _first(r"\*\*Priority:\*\*\s*([A-Za-z-]+)", body)
        status = "actioned-by-agent" if resolved else "pending"
        dates = _LEGACY_DATE.findall(body)
        status_changed = (dates[-1] if dates else raised) if resolved else raised

        slug = re.sub(r"[^A-Za-z0-9 -]", "", title).strip()
        slug = re.sub(r"\s+", " ", slug)[:60].strip()
        folder = RESOLVED_DIR if resolved else ESCALATIONS_DIR
        rel = f"{folder}/{raised} - {slug}.md"
        # Two legacy entries can share a date and truncate to the same slug
        # (real case: the duplicated ESRS entries). Disambiguate so the second
        # never silently overwrites the first — losing an escalation is exactly
        # the kind of quiet data loss this migration must not cause.
        if rel in report.files or (self._config.vault_path / rel).exists():
            rel = f"{folder}/{raised} - {slug} (ESC-{number:03d}).md"
        report.files.append(rel)
        if dry_run:
            return

        # Titles routinely contain colons ("Possible vault gap: FCA CP26/21"),
        # which are invalid in unquoted YAML scalars — always quote.
        lines = [
            "---",
            f"title: {_yaml_quote(title)}",
            "type: escalation",
            f"number: {number}",
            f"status: {status}",
            f"raised_date: {raised}",
            f"status_changed_date: {status_changed}",
            f"raised_by: {_yaml_quote(task or 'unknown')}",
        ]
        if priority:
            lines.append(f"priority: {priority.lower()}")
        lines += [f"uuid: {uuid4()}", "---", "", f"# ESC-{number:03d} — {title}", "", body, ""]
        target = self._config.vault_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines))


def _first(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else None


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
