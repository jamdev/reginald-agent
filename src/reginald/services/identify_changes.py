"""IdentifyChangesService — detect changes in a multi-author vault.

One cursor-driven walk of git history notices everything that lands on main,
from any author: Reginald's runs, contributors' pushes, salvage commits. Per
commit it produces two things —

  * content events, attributed to the commit's git author (mailed by the
    outbox, subject to the substantive-change rule in publisher.diff), and
  * callout work items ([!todo]/[!question] — the durable queue an
    action-edits run consumes),

then advances the cursor and republishes the site. Deterministic and free:
no model is ever invoked here.

Locking is the caller's job (CLI/timer wrap in the run lock; the run
pipeline calls this with the lock already held). Everything is idempotent —
the events natural key and the callout content-hash ledger make re-walks
no-ops, so each trigger can resume from the same cursor.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..core.config import Config
from ..core.git import CommitInfo, VaultGit
from ..core.observability import logger, span
from ..db.schema import CalloutItem, Cursor, Escalation, Run
from ..domain.models import IdentifyChangesResult, PendingEvent
from ..publisher.callouts import ACTIONABLE_TYPES, content_hash, parse_callouts, walk
from ..publisher.diff import RUN_LOG_PREFIXES, events_from_diff
from ..publisher.frontmatter import parse_frontmatter
from .events import EventService
from .publish import PublishService

# The command, step and Git cursor share a name with the synthetic run that
# holds events from commits outside a task run.
IDENTIFY_CHANGES = "identify-changes"

# Callouts in these zones never create work: logs quote things, archives are
# frozen, and .obsidian is not content.
EXCLUDED_PREFIXES = ("Archive/", ".obsidian/", *RUN_LOG_PREFIXES)


class IdentifyChangesService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        config: Config,
        events: EventService,
        publish: PublishService,
    ):
        self._sessions = session_factory
        self._config = config
        self._events = events
        self._publish = publish
        self._git = VaultGit(
            config.vault_path,
            author_name=config.git.author_name,
            author_email=config.git.author_email,
        )

    # -- the walk ----------------------------------------------------------

    @span(f"vault.{IDENTIFY_CHANGES}", log_when_disabled=False)
    def run(self, pull: bool = True, dry_run: bool = False) -> IdentifyChangesResult:
        """Walk cursor..HEAD; record events and callout items; publish.

        pull=False when the caller already synced (the run pipeline does).
        RebaseConflictError from the sync propagates — never swallowed.
        """
        if pull and self._config.git.push:
            self._git.sync_with_remote(self._config.git.remote, self._config.git.branch)

        head = self._git.head()
        cursor = self._cursor()
        if cursor is None:
            # First enablement: start from here, do not trawl history.
            if not dry_run:
                self._set_cursor(head)
                logger.info("%s cursor initialised at %s", IDENTIFY_CHANGES, head[:10])
            return IdentifyChangesResult(dry_run=dry_run, initialized=True, cursor_to=head)

        result = IdentifyChangesResult(dry_run=dry_run, cursor_from=cursor, cursor_to=head)
        if cursor == head:
            return result

        commits = self._git.commits_between(cursor, head)
        result.commits_scanned = len(commits)

        with span(f"{IDENTIFY_CHANGES}.walk", commits=len(commits)):
            for commit in commits:
                self._process_commit(commit, result, dry_run=dry_run)

        if not dry_run:
            self._set_cursor(head)
            if commits and self._config.site.enabled:
                result.published = self._publish.publish().ok
        logger.info(
            "%s %s..%s: %d commit(s), %d event(s), %d new item(s), %d actioned",
            IDENTIFY_CHANGES, cursor[:10], head[:10], len(commits),
            len(result.events_recorded), result.items_new, result.items_actioned,
        )
        return result

    # -- per-commit --------------------------------------------------------

    def _process_commit(
        self, commit: CommitInfo, result: IdentifyChangesResult, dry_run: bool
    ) -> None:
        author_kind = (
            "reginald"
            if commit.author_email == self._config.git.author_email
            else "contributor"
        )
        parent = self._git.parent_of(commit.sha)
        entries = self._git.diff_name_status(parent, commit.sha)

        def read_post(path: str) -> str:
            return self._git.show_file(commit.sha, path)

        def read_pre(path: str) -> str | None:
            try:
                return self._git.show_file(parent, path)
            except Exception:  # noqa: BLE001 — absent at parent = newly created
                return None

        # A topic-scoped run's commit keeps its event filter: the restriction
        # is enforced here, not just suggested in the prompt.
        run = self._run_for_commit(commit) if author_kind == "reginald" else None
        producing_run_id, only_topic = run or (None, None)

        raws = events_from_diff(
            entries, commit.sha, read_post, read_pre,
            commit_subject=commit.subject, only_topic=only_topic,
        )
        if dry_run:
            fresh, skipped = self._events.partition_existing(raws)
            result.events_recorded += [
                PendingEvent(topic=r.topic, kind=r.kind, file_path=r.file_path, title=r.title)  # type: ignore[arg-type]
                for r in fresh
            ]
            result.events_skipped += skipped
        elif raws:
            run_id = producing_run_id or self._scan_run_id()
            recorded, skipped = self._events.persist(
                run_id, raws,
                author_name=commit.author_name,
                author_email=commit.author_email,
                author_kind=author_kind,
            )
            result.events_recorded += recorded
            result.events_skipped += skipped

        self._process_callouts(commit, entries, author_kind, producing_run_id, result, dry_run)

    def _process_callouts(
        self, commit: CommitInfo, entries, author_kind: str, producing_run_id: int | None,
        result: IdentifyChangesResult, dry_run: bool,
    ) -> None:
        with self._sessions.begin() as session:
            for entry in entries:
                if not entry.path.endswith(".md"):
                    continue
                if entry.status == "D":
                    if not dry_run:
                        result.items_not_actioned += self._close_items_for_file(
                            session, entry.path, state="not-actioned"
                        )
                    continue
                if entry.path.startswith(EXCLUDED_PREFIXES):
                    continue

                text = self._git.show_file(commit.sha, entry.path)
                note_uuid = self._note_uuid(text)
                note_id = note_uuid or entry.path
                callouts = parse_callouts(text)
                present: dict[str, tuple] = {}
                for callout, parent in walk(callouts):
                    if callout.type in ACTIONABLE_TYPES:
                        present[content_hash(note_id, callout)] = (callout, parent)

                if dry_run:
                    for chash in present:
                        if not self._item_by_hash(session, chash):
                            result.items_new += 1
                    continue

                # New work items
                for chash, (callout, parent) in present.items():
                    if self._item_by_hash(session, chash):
                        continue
                    parent_item = None
                    if parent is not None and parent.type in ACTIONABLE_TYPES:
                        parent_item = self._item_by_hash(session, content_hash(note_id, parent))
                    session.add(
                        CalloutItem(
                            content_hash=chash,
                            note_uuid=note_uuid,
                            file_path=entry.path,
                            escalation_id=self._escalation_id(session, entry.path, note_uuid),
                            parent_id=parent_item.id if parent_item else None,
                            callout_type=callout.type,
                            title_line=callout.title[:200],
                            body_excerpt=callout.body[:500],
                            author_name=commit.author_name,
                            author_email=commit.author_email,
                            author_kind=author_kind,
                            addressee=callout.addressee,
                            introduced_commit=commit.sha,
                        )
                    )
                    session.flush()
                    result.items_new += 1

                # Completion / disappearance of previously-pending items
                for item in self._pending_items_for_file(session, entry.path, note_uuid):
                    if entry.status == "R" and item.file_path != entry.path:
                        item.file_path = entry.path  # follow the rename
                    found = present.get(item.content_hash)
                    if found is None:
                        # A rename changes note_id only when the note has no
                        # uuid; recompute against the new identity to be sure.
                        item.state = "not-actioned"
                        item.actioned_at = datetime.now(UTC)
                        result.items_not_actioned += 1
                    elif found[0].children:
                        item.state = "actioned"
                        item.actioned_at = datetime.now(UTC)
                        item.actioned_run_id = producing_run_id
                        result.items_actioned += 1

    # -- manifest (consumed by the action-edits run) -----------------------

    def manifest(self, cap: int = 20) -> tuple[str | None, list[int]]:
        """Pending items for Reginald (addressed to its handle, or
        unaddressed), rendered for prompt injection. (text, item ids)."""
        handle = self._config.identity.handle
        with self._sessions() as session:
            items = session.scalars(
                select(CalloutItem)
                .where(
                    CalloutItem.state == "pending",
                    (CalloutItem.addressee.is_(None)) | (CalloutItem.addressee == handle),
                )
                .order_by(CalloutItem.created_at)
                .limit(cap)
            ).all()
            if not items:
                return None, []
            lines = []
            for n, item in enumerate(items, start=1):
                who = item.author_name or "unknown"
                addressed = f" (addressed @{item.addressee})" if item.addressee else ""
                lines.append(
                    f"{n}. [{item.callout_type}] in `{item.file_path}`"
                    f" — from {who}{addressed}\n"
                    f"   Title: {item.title_line or '(none)'}\n"
                    f"   Body: {(item.body_excerpt or '').strip()[:300] or '(none)'}"
                )
            return "\n".join(lines), [item.id for item in items]

    def verify_actioned(self, item_ids: list[int], run_id: int) -> int:
        """Post-agent check: an item counts as actioned only if a nested reply
        now exists in the working tree. Trust nothing."""
        actioned = 0
        with self._sessions.begin() as session:
            for item_id in item_ids:
                item = session.get(CalloutItem, item_id)
                if item is None or item.state != "pending":
                    continue
                path = self._config.vault_path / item.file_path
                if not path.exists():
                    continue
                text = path.read_text()
                note_id = item.note_uuid or item.file_path
                for callout, _parent in walk(parse_callouts(text)):
                    if (
                        callout.type in ACTIONABLE_TYPES
                        and content_hash(note_id, callout) == item.content_hash
                        and callout.children
                    ):
                        item.state = "actioned"
                        item.actioned_at = datetime.now(UTC)
                        item.actioned_run_id = run_id
                        actioned += 1
                        break
        return actioned

    def items(self, state: str | None = "pending", addressee: str | None = None) -> list[CalloutItem]:
        with self._sessions() as session:
            stmt = select(CalloutItem).order_by(CalloutItem.created_at)
            if state:
                stmt = stmt.where(CalloutItem.state == state)
            if addressee:
                stmt = stmt.where(CalloutItem.addressee == addressee)
            items = session.scalars(stmt).all()
            session.expunge_all()
            return list(items)

    # -- plumbing ----------------------------------------------------------

    def _cursor(self) -> str | None:
        with self._sessions() as session:
            row = session.get(Cursor, IDENTIFY_CHANGES)
            return row.commit_hash if row else None

    def _set_cursor(self, sha: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(Cursor, IDENTIFY_CHANGES)
            if row is None:
                session.add(Cursor(name=IDENTIFY_CHANGES, commit_hash=sha))
            else:
                row.commit_hash = sha

    def _run_for_commit(self, commit: CommitInfo) -> tuple[int, str | None] | None:
        """(run_id, topic name) of the run that produced this commit, if any."""
        with self._sessions() as session:
            if commit.run_trailer:
                run_id, _, pre_commit = commit.run_trailer.partition(" ")
                run = session.scalar(
                    select(Run).where(Run.id == run_id, Run.pre_commit == pre_commit)
                )
            else:
                # Older commits have no trailer. A no-op run merely observed this HEAD.
                run = session.scalar(select(Run).where(
                    Run.post_commit == commit.sha, Run.pre_commit != Run.post_commit,
                ))
            if run is None:
                return None
            return run.id, (run.topic.name if run.topic else None)

    def _scan_run_id(self) -> int:
        if getattr(self, "_synthetic_run_id", None) is not None:
            return self._synthetic_run_id
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            run = Run(
                task=IDENTIFY_CHANGES, status="success",
                started_at=now, finished_at=now,
            )
            session.add(run)
            session.flush()
            self._synthetic_run_id = run.id
            return run.id

    @staticmethod
    def _note_uuid(text: str) -> str | None:
        try:
            value = parse_frontmatter(text).get("uuid")
            return str(value) if value else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _item_by_hash(session: Session, chash: str) -> CalloutItem | None:
        return session.scalar(select(CalloutItem).where(CalloutItem.content_hash == chash))

    @staticmethod
    def _pending_items_for_file(session: Session, path: str, note_uuid: str | None):
        clause = CalloutItem.file_path == path
        if note_uuid:
            clause = clause | (CalloutItem.note_uuid == note_uuid)
        return session.scalars(
            select(CalloutItem).where(CalloutItem.state == "pending", clause)
        ).all()

    @staticmethod
    def _close_items_for_file(session: Session, path: str, state: str) -> int:
        items = session.scalars(
            select(CalloutItem).where(
                CalloutItem.state == "pending", CalloutItem.file_path == path
            )
        ).all()
        for item in items:
            item.state = state
            item.actioned_at = datetime.now(UTC)
        return len(items)

    @staticmethod
    def _escalation_id(session: Session, path: str, note_uuid: str | None) -> int | None:
        if not path.startswith("Escalations/"):
            return None
        esc = session.scalar(select(Escalation).where(Escalation.file_path == path))
        if esc is None and note_uuid:
            esc = session.scalar(select(Escalation).where(Escalation.uuid == note_uuid))
        return esc.id if esc else None
