from uuid import UUID

from reginald.publisher.frontmatter import parse_frontmatter
from reginald.services.vault import VaultService


def test_backfill_adds_uuids_everywhere(config):
    vault = config.vault_path
    (vault / "Home.md").write_text("---\ntitle: Home\n---\n\n# Home\n")
    (vault / "Inbox").mkdir()
    (vault / "Inbox" / "raw note.md").write_text("no frontmatter at all\n")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "skip.md").write_text("should not be touched")

    service = VaultService(config)
    result = service.backfill_uuids()

    # 3 topic Index.md + Home.md + Inbox note = 5 scanned
    assert result.scanned == 5
    assert result.updated == 5

    fm = parse_frontmatter((vault / "Home.md").read_text())
    assert fm["title"] == "Home"
    UUID(fm["uuid"])

    inbox_text = (vault / "Inbox" / "raw note.md").read_text()
    assert "no frontmatter at all" in inbox_text
    UUID(parse_frontmatter(inbox_text)["uuid"])

    assert (vault / ".obsidian" / "skip.md").read_text() == "should not be touched"


def test_backfill_is_idempotent(config):
    service = VaultService(config)
    first = service.backfill_uuids()
    assert first.updated > 0
    second = service.backfill_uuids()
    assert second.updated == 0
    assert second.already_had == first.scanned


def test_backfill_dry_run_writes_nothing(config):
    service = VaultService(config)
    before = {
        p: p.read_text() for p in config.vault_path.rglob("*.md")
    }
    result = service.backfill_uuids(dry_run=True)
    assert result.updated > 0
    assert result.dry_run
    for path, text in before.items():
        assert path.read_text() == text


# -- normalize_frontmatter ---------------------------------------------------

_DUPE = (
    "---\n"
    "title: Roadmap\n"
    "uuid: c1dfd743-c317-4444-b92f-9cc2e5f135a4\n"
    "publish: true\n"
    "uuid: c1dfd743-c317-4444-b92f-9cc2e5f135a4\n"
    "---\n"
    "# Body\n"
)


def test_normalize_collapses_duplicate_keys_across_the_vault(config):
    vault = config.vault_path
    (vault / "dupe.md").write_text(_DUPE)
    (vault / "clean.md").write_text("---\ntitle: Clean\n---\n\nbody\n")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "cfg.md").write_text(_DUPE)  # skipped dir, left alone

    service = VaultService(config)
    result = service.normalize_frontmatter()

    assert result.fixed == 1
    assert result.fixed_files == {"dupe.md": ["uuid"]}
    assert (vault / "dupe.md").read_text().split("---", 2)[1].count("uuid:") == 1
    assert parse_frontmatter((vault / "dupe.md").read_text())["uuid"] == (
        "c1dfd743-c317-4444-b92f-9cc2e5f135a4"
    )
    # the skipped-dir file is untouched — still has both uuid lines
    assert (vault / ".obsidian" / "cfg.md").read_text() == _DUPE


def test_normalize_is_idempotent(config):
    vault = config.vault_path
    (vault / "dupe.md").write_text(_DUPE)
    service = VaultService(config)
    assert service.normalize_frontmatter().fixed == 1
    assert service.normalize_frontmatter().fixed == 0


def test_normalize_dry_run_writes_nothing(config):
    vault = config.vault_path
    (vault / "dupe.md").write_text(_DUPE)
    service = VaultService(config)
    result = service.normalize_frontmatter(dry_run=True)
    assert result.fixed == 1
    assert result.dry_run
    assert (vault / "dupe.md").read_text() == _DUPE  # unchanged on disk
