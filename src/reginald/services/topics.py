"""TopicService — seed and expose the topics table from the vault's Topics/ folders.

Topic identity is a UUID stored in the topic's Index.md frontmatter (`uuid:`)
and mirrored in the database. Names remain human identifiers, but UUIDs are
preferred: a folder rename with the same frontmatter UUID updates the existing
row (subscriptions and events follow) instead of creating a duplicate.

Reconciliation order on sync, per folder:
  1. frontmatter UUID matches a DB row -> same topic (rename-aware)
  2. name matches a DB row            -> same topic; adopt/refresh the UUID
  3. neither                          -> new topic; generate UUID, write it back
Folders without an Index.md still get a DB row; the UUID is written to the
frontmatter the first time an Index.md exists at sync time.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..core.config import Config
from ..core.observability import logger
from ..db.schema import Topic
from ..domain.models import TopicView
from ..publisher.diff import UNROUTED_TOPIC
from ..publisher.frontmatter import parse_frontmatter, upsert_frontmatter_key

OPERATIONAL_TOPIC = "__operational__"
# __unrouted__ is where an escalation lands when it names no topic, or names one
# outside the taxonomy. It has no audience of its own, so the outbox routes it
# to the all-topics subscribers rather than dropping it. Inactive like
# __operational__, so it never appears in a subscriber's effective topics.
SENTINEL_TOPICS = (OPERATIONAL_TOPIC, UNROUTED_TOPIC)

# The canonical topic list lives here. It is the source of truth for which
# topic names are valid; folders are materialised from it on demand.
TAXONOMY_TOPICS_PATH = "Setup/Taxonomy/Taxonomy - Regulatory topics.md"
# A numbered list item: `9. **Topic Name** — one-line scope`.
_TAXONOMY_ITEM = re.compile(
    r"^\d+\.\s+\*\*(?P<name>[^*]+?)\*\*(?:\s*[—–-]\s*(?P<desc>.*))?\s*$", re.M
)


_UNSET = object()  # "canonical list not yet parsed" sentinel


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "topic"


def _as_uuid(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(UUID(str(value)))
    except ValueError:
        return None


class TopicService:
    def __init__(self, session_factory: sessionmaker[Session], config: Config):
        self._sessions = session_factory
        self._config = config
        self._canonical: dict[str, str] | None | object = _UNSET

    # -- canonical topics (the taxonomy is the source of truth) ------------

    def canonical_topics(self) -> dict[str, str] | None:
        """Canonical topic names -> one-line scope, parsed from the taxonomy
        file. Returns None when that file is absent, in which case callers fall
        back to accepting any name (scratch/test vaults have no taxonomy)."""
        if self._canonical is _UNSET:
            path = self._config.vault_path / TAXONOMY_TOPICS_PATH
            if path.exists():
                self._canonical = {
                    m.group("name").strip(): (m.group("desc") or "").strip()
                    for m in _TAXONOMY_ITEM.finditer(path.read_text())
                }
            else:
                self._canonical = None
        return self._canonical  # type: ignore[return-value]

    def _materialise_topic(self, name: str, scope: str) -> str:
        """Create Topics/<name>/ + a skeleton Index.md for a canonical topic
        that has no folder yet, and return its uuid. Idempotent: an existing
        Index.md keeps its own uuid."""
        folder = self._config.vault_path / "Topics" / name
        (folder / "Briefs").mkdir(parents=True, exist_ok=True)
        index = folder / "Index.md"
        if index.exists():
            return _as_uuid(parse_frontmatter(index.read_text()).get("uuid")) or str(uuid4())
        new_uuid = str(uuid4())
        index.write_text(
            f"---\ntitle: {name} - Topic Index\ntype: index\njurisdiction: UK-EU\n"
            f"topic: {name}\nstatus: live\nlast_verified: {datetime.now(UTC).date().isoformat()}\n"
            f"owner_agent: eu-uk-regdev-paralegal\nuuid: {new_uuid}\npublish: true\n---\n\n"
            f"# {name}\n\n*{scope or (name + '.')}*\n\n"
            f"## Summary\n\n_No developments filed yet._\n\n"
            f"## Developments\n\nReverse-chronological list of all briefs filed under this topic.\n\n"
            f"## Cross-references\n"
        )
        logger.info("materialised canonical topic folder: %r", name)
        return new_uuid

    # -- sync --------------------------------------------------------------

    def sync(self) -> list[TopicView]:
        """Reconcile the topics table with Topics/ folders (see module docstring).
        Vanished topics are deactivated, never deleted — events reference them."""
        topics_dir = self._config.vault_path / "Topics"
        folder_names = sorted(
            p.name for p in topics_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
        ) if topics_dir.is_dir() else []

        with self._sessions.begin() as session:
            rows = session.scalars(select(Topic)).all()
            by_uuid = {t.uuid: t for t in rows}
            by_name = {t.name: t for t in rows}
            seen_ids: set[int] = set()

            for name in folder_names:
                file_uuid = self._read_index_uuid(name)

                if file_uuid and file_uuid in by_uuid:
                    topic = by_uuid[file_uuid]
                    if topic.name != name:
                        logger.info("topic renamed via uuid: %r -> %r", topic.name, name)
                        topic.name = name
                        topic.slug = slugify(name)
                elif name in by_name:
                    topic = by_name[name]
                    if file_uuid and topic.uuid != file_uuid:
                        topic.uuid = file_uuid  # frontmatter is authoritative
                else:
                    topic = Topic(name=name, slug=slugify(name), uuid=file_uuid or str(uuid4()))
                    session.add(topic)
                    session.flush()

                topic.active = True
                seen_ids.add(topic.id)
                if not file_uuid:
                    self._write_index_uuid(name, topic.uuid)

            for topic in rows:
                if topic.id not in seen_ids and topic.name not in SENTINEL_TOPICS:
                    topic.active = False
            if OPERATIONAL_TOPIC not in by_name:
                session.add(
                    Topic(name=OPERATIONAL_TOPIC, slug="operational", active=False)
                )
            if UNROUTED_TOPIC not in by_name:
                session.add(Topic(name=UNROUTED_TOPIC, slug="unrouted", active=False))
        return self.list()

    def _index_path(self, name: str):
        return self._config.vault_path / "Topics" / name / "Index.md"

    def _read_index_uuid(self, name: str) -> str | None:
        path = self._index_path(name)
        if not path.exists():
            return None
        return _as_uuid(parse_frontmatter(path.read_text()).get("uuid"))

    def _write_index_uuid(self, name: str, value: str) -> None:
        path = self._index_path(name)
        if not path.exists():
            logger.warning("no Index.md for topic %r; uuid stored in DB only", name)
            return
        path.write_text(upsert_frontmatter_key(path.read_text(), "uuid", value))
        logger.info("wrote uuid to %s", path)

    # -- resolution ---------------------------------------------------------

    def resolve(self, session: Session, identifier: str) -> Topic | None:
        """Resolve a topic by UUID (preferred) or exact name."""
        canonical = _as_uuid(identifier)
        if canonical:
            topic = session.scalar(select(Topic).where(Topic.uuid == canonical))
            if topic:
                return topic
        return session.scalar(select(Topic).where(Topic.name == identifier))

    def resolve_name(self, identifier: str) -> str | None:
        """Resolve an identifier to the topic's current name (for path scoping)."""
        with self._sessions() as session:
            topic = self.resolve(session, identifier)
            return topic.name if topic else None

    # -- queries ------------------------------------------------------------

    def list(self, include_inactive: bool = True) -> list[TopicView]:
        with self._sessions() as session:
            stmt = select(Topic).order_by(Topic.name)
            if not include_inactive:
                stmt = stmt.where(Topic.active.is_(True))
            return [TopicView.model_validate(t) for t in session.scalars(stmt).all()]

    def get_or_create(self, session: Session, name: str) -> Topic | None:
        """Resolve a topic name to a row when filing events, honouring the
        canonical topic list. Returns None for a non-canonical name with no
        folder (a typo / invented topic) so the caller skips it rather than
        creating a phantom. A canonical topic with no folder yet is materialised
        (folder + skeleton Index.md) on first use."""
        topic = session.scalar(select(Topic).where(Topic.name == name))
        if topic is not None:
            return topic

        if name in SENTINEL_TOPICS:  # internal routing buckets, not vault folders
            topic = Topic(name=name, slug=slugify(name), uuid=str(uuid4()), active=False)
            session.add(topic)
            session.flush()
            return topic

        file_uuid = self._read_index_uuid(name)
        if file_uuid:
            topic = session.scalar(select(Topic).where(Topic.uuid == file_uuid))
            if topic:  # rename observed mid-run
                topic.name = name
                topic.slug = slugify(name)
                session.flush()
                return topic

        # A name that differs from an existing topic only by punctuation (e.g. a
        # `secondary_topics` entry missing a comma) slugifies to the same slug.
        # Reuse that topic rather than insert a row whose slug already exists —
        # the UNIQUE(slug) constraint would otherwise abort the run.
        existing = session.scalar(select(Topic).where(Topic.slug == slugify(name)))
        if existing:
            return existing

        canonical = self.canonical_topics()
        folder_exists = (self._config.vault_path / "Topics" / name).is_dir()
        if canonical is not None:
            if name in canonical and not folder_exists:
                file_uuid = self._materialise_topic(name, canonical[name])
            elif name not in canonical and not folder_exists:
                logger.warning("ignoring non-canonical topic reference: %r", name)
                return None
            elif name not in canonical and folder_exists:
                logger.warning("off-taxonomy topic folder in use: %r (add it to the taxonomy)", name)

        topic = Topic(
            name=name,
            slug=slugify(name),
            uuid=file_uuid or str(uuid4()),
            active=name not in SENTINEL_TOPICS,
        )
        session.add(topic)
        session.flush()
        return topic
