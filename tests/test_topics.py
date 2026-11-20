"""Topic UUID identity: frontmatter <-> DB, rename safety, resolution."""

import shutil
from uuid import UUID

from reginald.domain.models import AddSubscriber
from reginald.publisher.frontmatter import parse_frontmatter, upsert_frontmatter_key


def index_path(config, name):
    return config.vault_path / "Topics" / name / "Index.md"


def test_sync_writes_uuid_to_frontmatter(topics, config):
    views = {v.name: v for v in topics.list()}
    fm = parse_frontmatter(index_path(config, "Payments and E-money").read_text())
    assert fm["uuid"] == views["Payments and E-money"].uuid
    UUID(fm["uuid"])  # valid uuid
    assert fm["title"] == "Payments and E-money"  # rest of frontmatter preserved


def test_sync_is_stable(topics, config):
    first = {v.name: v.uuid for v in topics.list()}
    topics.sync()
    second = {v.name: v.uuid for v in topics.list()}
    assert first == second


def test_rename_preserves_identity_and_subscriptions(topics, subscribers, config):
    subscribers.add(
        AddSubscriber(email="a@example.org", topics=["Payments and E-money"])
    )
    old_uuid = next(v.uuid for v in topics.list() if v.name == "Payments and E-money")

    # rename the folder; Index.md (carrying the uuid) moves with it
    old_dir = config.vault_path / "Topics" / "Payments and E-money"
    new_dir = config.vault_path / "Topics" / "Payments, E-money and Stablecoins"
    shutil.move(str(old_dir), str(new_dir))
    topics.sync()

    views = {v.name: v for v in topics.list()}
    assert "Payments, E-money and Stablecoins" in views
    assert views["Payments, E-money and Stablecoins"].uuid == old_uuid
    assert "Payments and E-money" not in views  # renamed, not duplicated

    view = subscribers.get("a@example.org")
    assert view.effective_topics == ["Payments, E-money and Stablecoins"]


def test_frontmatter_uuid_is_authoritative(topics, config, sessions):
    """Operator sets an explicit uuid in Index.md -> DB adopts it."""
    path = index_path(config, "AI and Algorithmic Systems")
    forced = "12345678-1234-5678-1234-567812345678"
    path.write_text(upsert_frontmatter_key(path.read_text(), "uuid", forced))
    topics.sync()
    view = next(v for v in topics.list() if v.name == "AI and Algorithmic Systems")
    assert view.uuid == forced


def test_subscribe_by_uuid(topics, subscribers):
    uuid = next(v.uuid for v in topics.list() if v.name == "Payments and E-money")
    view = subscribers.add(AddSubscriber(email="u@example.org", topics=[uuid]))
    assert view.effective_topics == ["Payments and E-money"]


def test_upsert_frontmatter_key_variants():
    # existing block, new key
    doc = "---\ntitle: X\n---\n\nBody\n"
    out = upsert_frontmatter_key(doc, "uuid", "abc")
    assert parse_frontmatter(out) == {"title": "X", "uuid": "abc"}
    assert out.endswith("Body\n")
    # existing key replaced
    out2 = upsert_frontmatter_key(out, "uuid", "def")
    assert parse_frontmatter(out2)["uuid"] == "def"
    assert out2.count("uuid:") == 1
    # no frontmatter at all
    out3 = upsert_frontmatter_key("# Heading\n", "uuid", "abc")
    assert parse_frontmatter(out3) == {"uuid": "abc"}
    assert "# Heading" in out3


def test_get_or_create_reuses_topic_for_a_punctuation_variant(topics, sessions):
    """A secondary_topics name that differs from a real topic only by
    punctuation (e.g. a missing comma) slugifies to the same slug. get_or_create
    must reuse the existing topic, not insert a row whose slug already exists —
    the UNIQUE(slug) constraint would otherwise abort the whole run."""
    from sqlalchemy import func, select

    from reginald.db.schema import Topic

    with sessions.begin() as session:
        canonical = topics.get_or_create(session, "Payments and E-money")
        variant = topics.get_or_create(session, "Payments and E money")  # no hyphen
        assert variant.id == canonical.id

    with sessions() as session:
        count = session.scalar(
            select(func.count())
            .select_from(Topic)
            .where(Topic.slug == "payments-and-e-money")
        )
        assert count == 1  # no duplicate row created


# -- canonical topics (taxonomy is the source of truth) ----------------------

from reginald.services.topics import TopicService  # noqa: E402


def _write_taxonomy(config, names):
    p = config.vault_path / "Setup" / "Taxonomy" / "Taxonomy - Regulatory topics.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{i}. **{n}** — scope for {n}." for i, n in enumerate(names, 1))
    p.write_text(f"# Regulatory topics\n\n{body}\n")


def test_non_canonical_topic_reference_is_skipped(sessions, config):
    """A topic name not in the taxonomy (a typo / invented one) with no folder
    is skipped, not turned into a phantom row."""
    _write_taxonomy(config, ["Payments and E-money", "Crypto-assets and Digital Assets"])
    svc = TopicService(sessions, config)
    with sessions.begin() as session:
        assert svc.get_or_create(session, "Totally Made Up Topic") is None
        # a real canonical one still resolves
        assert svc.get_or_create(session, "Payments and E-money") is not None


def test_canonical_topic_without_a_folder_is_materialised(sessions, config):
    """A canonical topic referenced before it has a folder gets one created and
    populated (skeleton Index.md), and an active row."""
    _write_taxonomy(config, ["Payments and E-money", "Sanctions and Export Controls"])
    svc = TopicService(sessions, config)
    with sessions.begin() as session:
        t = svc.get_or_create(session, "Sanctions and Export Controls")
        assert t is not None and t.active

    idx = config.vault_path / "Topics" / "Sanctions and Export Controls" / "Index.md"
    assert idx.exists()
    fm = parse_frontmatter(idx.read_text())
    assert fm["topic"] == "Sanctions and Export Controls"
    assert fm["publish"] is True
    assert "scope for Sanctions and Export Controls" in idx.read_text()
    assert t.uuid == fm["uuid"]  # DB row and Index agree


def test_no_taxonomy_file_stays_permissive(sessions, config):
    """Scratch/test vaults without a taxonomy file accept any topic name."""
    svc = TopicService(sessions, config)  # config vault has no taxonomy file
    assert svc.canonical_topics() is None
    with sessions.begin() as session:
        assert svc.get_or_create(session, "Anything Goes Here") is not None
