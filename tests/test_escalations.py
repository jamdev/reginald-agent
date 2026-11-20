"""EscalationService: the repair matrix, adoption, numbering, migration."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from reginald.core.config import Config
from reginald.db.session import init_db, make_session_factory
from reginald.publisher.frontmatter import parse_frontmatter
from reginald.services.escalations import (
    ESCALATIONS_DIR,
    MIGRATED_MARKER,
    RESOLVED_DIR,
    EscalationError,
    EscalationService,
)


def git(vault: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(vault), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def write(vault: Path, rel: str, body: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def escalation(title: str, status: str = "pending", number: int | None = 1,
               uuid: str = "11111111-1111-4111-8111-111111111111") -> str:
    lines = ["---", f"title: {title}", "type: escalation"]
    if number is not None:
        lines.append(f"number: {number}")
    lines += [f"status: {status}", "raised_date: 2026-08-01",
              "status_changed_date: 2026-08-01", f"uuid: {uuid}", "---", "", "# Body", ""]
    return "\n".join(lines)


@pytest.fixture
def vault(tmp_path) -> Path:
    v = tmp_path / "vault"
    (v / ESCALATIONS_DIR).mkdir(parents=True)
    git(v, "init", "-q", "-b", "main")
    git(v, "config", "user.email", "test@example.org")
    git(v, "config", "user.name", "Test")
    return v


@pytest.fixture
def service(vault, tmp_path) -> EscalationService:
    config = Config(env="dev", vault_path=vault, db_path=tmp_path / "test.db")
    init_db(config.db_path)
    sessions: sessionmaker[Session] = make_session_factory(config.db_path)
    return EscalationService(sessions, config)


# -- the repair matrix -------------------------------------------------------


def test_resolved_status_in_root_moves_the_file(service, vault):
    write(vault, f"{ESCALATIONS_DIR}/2026-08-01 - Done thing.md",
          escalation("Done thing", status="actioned-by-agent"))

    report = service.sync()

    assert not (vault / ESCALATIONS_DIR / "2026-08-01 - Done thing.md").exists()
    assert (vault / RESOLVED_DIR / "2026-08-01 - Done thing.md").exists()
    assert any("moved to Resolved/" in r for r in report.repaired)
    [row] = service.list()
    assert row.file_path == f"{RESOLVED_DIR}/2026-08-01 - Done thing.md"


def test_pending_file_moved_to_resolved_by_hand_becomes_actioned_by_human(service, vault):
    """Moving IS acting — the human's intent wins, the status follows."""
    path = write(vault, f"{RESOLVED_DIR}/2026-08-01 - Human dealt with it.md",
                 escalation("Human dealt with it", status="pending"))

    service.sync()

    fm = parse_frontmatter(path.read_text())
    assert fm["status"] == "actioned-by-human"
    [row] = service.list()
    assert row.status == "actioned-by-human"
    assert row.status_changed_date != "2026-08-01"  # stamped today


def test_status_transition_without_a_date_gets_stamped(service, vault):
    path = write(vault, f"{ESCALATIONS_DIR}/2026-08-01 - Thing.md", escalation("Thing"))
    service.sync()

    # human flips the status but forgets the date
    path.write_text(path.read_text().replace("status: pending", "status: ignored"))
    service.sync()

    moved = vault / RESOLVED_DIR / "2026-08-01 - Thing.md"
    fm = parse_frontmatter(moved.read_text())
    assert fm["status"] == "ignored"
    assert str(fm["status_changed_date"]) != "2026-08-01"


def test_a_human_set_date_is_never_overwritten(service, vault):
    path = write(vault, f"{ESCALATIONS_DIR}/2026-08-01 - Thing.md", escalation("Thing"))
    service.sync()

    text = path.read_text().replace("status: pending", "status: ignored")
    text = text.replace("status_changed_date: 2026-08-01", "status_changed_date: 2026-08-05")
    path.write_text(text)
    service.sync()

    moved = vault / RESOLVED_DIR / "2026-08-01 - Thing.md"
    assert str(parse_frontmatter(moved.read_text())["status_changed_date"]) == "2026-08-05"


def test_contributor_raised_escalation_is_adopted_and_numbered(service, vault):
    """Contributors omit number:; sync assigns the next free one."""
    write(vault, f"{ESCALATIONS_DIR}/2026-08-01 - First.md",
          escalation("First", number=7, uuid="11111111-1111-4111-8111-111111111111"))
    contributed = (
        "---\ntitle: Contributed\ntype: escalation\nstatus: pending\n---\n\n# Body\n"
    )
    path = write(vault, f"{ESCALATIONS_DIR}/2026-08-09 - Contributed.md", contributed)
    git(vault, "add", "-A")
    git(vault, "commit", "-qm", "seed")

    report = service.sync()

    assert report.adopted == 2
    fm = parse_frontmatter(path.read_text())
    assert fm["number"] == 8            # max existing + 1
    assert fm["uuid"]                   # fresh uuid backfilled
    assert str(fm["raised_date"])       # backfilled from git
    rows = {r.number: r for r in service.list()}
    assert set(rows) == {7, 8}
    assert rows[8].title == "Contributed"


def test_missing_file_is_flagged_not_deleted(service, vault):
    path = write(vault, f"{ESCALATIONS_DIR}/2026-08-01 - Vanishing.md", escalation("Vanishing"))
    service.sync()
    path.unlink()

    report = service.sync()

    assert report.missing == 1
    [row] = service.list()
    assert row.missing_since is not None
    assert row.status == "pending"      # status untouched


def test_unknown_status_is_reported_not_repaired(service, vault):
    write(vault, f"{ESCALATIONS_DIR}/2026-08-01 - Odd.md",
          escalation("Odd", status="wontfix"))
    report = service.sync()
    assert any("wontfix" in a for a in report.anomalies)
    # not moved anywhere
    assert (vault / ESCALATIONS_DIR / "2026-08-01 - Odd.md").exists()


def test_sync_is_idempotent(service, vault):
    write(vault, f"{ESCALATIONS_DIR}/2026-08-01 - Steady.md",
          escalation("Steady", status="actioned-by-agent"))
    git(vault, "add", "-A")
    git(vault, "commit", "-qm", "seed")
    first = service.sync()
    second = service.sync()
    assert first.changed
    assert not second.changed


# -- set-status --------------------------------------------------------------


def test_set_status_writes_vault_first_then_moves(service, vault):
    write(vault, f"{ESCALATIONS_DIR}/2026-08-01 - Fix me.md", escalation("Fix me"))
    service.sync()

    row = service.set_status("ESC-1", "actioned-by-human")

    assert row.status == "actioned-by-human"
    moved = vault / RESOLVED_DIR / "2026-08-01 - Fix me.md"
    assert moved.exists()
    assert parse_frontmatter(moved.read_text())["status"] == "actioned-by-human"


def test_set_status_rejects_invalid_values(service, vault):
    write(vault, f"{ESCALATIONS_DIR}/2026-08-01 - X.md", escalation("X"))
    service.sync()
    with pytest.raises(EscalationError, match="invalid status"):
        service.set_status("1", "done")


# -- legacy migration --------------------------------------------------------

LEGACY = """---
title: Escalations — Human attention required
type: escalations
uuid: 60787bf2-50a2-41a5-8ecd-93bf51f3bbc6
---

# Escalations

Items flagged for human review.

---

## Open

### ESC-037 — ICO enforcement notice against the Met

**Raised:** 2026-08-08
**Agent:** eu-uk-regdev-paralegal
**Task:** Draft daily brief
**Priority:** Low (no compliance deadline)

**Issue:** A long description of the issue.

**Action recommended:** Operator to confirm scope.

---

### ESC-036 — Possible vault gap: FCA CP26/21 untracked

**Raised:** 2026-08-05
**Task:** Draft daily brief
**Priority:** Medium (deadline within 30 days)

**Issue:** Another issue.

**Update (2026-08-06, Update trackers run):** Partially resolved.

---

## Resolved

### ESC-035 — Primary source could not be retrieved

**Raised:** 2026-08-05
**Task:** Draft daily brief
**Priority:** Low-Medium

**Issue:** Could not fetch.

**Resolution (2026-08-07, Source verification run):** Fetched and confirmed.
"""


def test_migration_splits_open_and_resolved(service, vault):
    write(vault, "Setup/Escalations.md", LEGACY)
    git(vault, "add", "-A")
    git(vault, "commit", "-qm", "legacy")

    report = service.migrate()

    assert report.entries == 3
    assert report.open_entries == 2
    assert report.resolved_entries == 1

    open_files = sorted(p.name for p in (vault / ESCALATIONS_DIR).glob("*.md"))
    assert open_files == [
        "2026-08-05 - Possible vault gap FCA CP2621 untracked.md",
        "2026-08-08 - ICO enforcement notice against the Met.md",
    ]
    [resolved] = (vault / RESOLVED_DIR).glob("*.md")
    fm = parse_frontmatter(resolved.read_text())
    assert fm["number"] == 35
    assert fm["status"] == "actioned-by-agent"
    assert str(fm["status_changed_date"]) == "2026-08-07"  # from the Resolution line
    assert fm["raised_by"] == "Draft daily brief"
    assert fm["priority"] == "low-medium"
    assert "Fetched and confirmed" in resolved.read_text()  # body preserved

    # numbering continues above the legacy maximum
    rows = service.list()
    assert {r.number for r in rows} == {35, 36, 37}

    # tombstone in place, uuid retained
    legacy_text = (vault / "Setup/Escalations.md").read_text()
    assert MIGRATED_MARKER in legacy_text
    assert "60787bf2" in legacy_text


DUPLICATE_SLUG = """---
title: Escalations
type: escalations
uuid: 60787bf2-50a2-41a5-8ecd-93bf51f3bbc6
---

# Escalations

## Open

### ESC-006 — Commission sector-specific ESRS statutory deadline (30 June 2026) — first

**Raised:** 2026-06-22
**Task:** Research new developments

**Issue:** First of two same-day entries whose titles truncate to one slug.

### ESC-007 — Commission sector-specific ESRS statutory deadline (30 June 2026) — second

**Raised:** 2026-06-22
**Task:** Research new developments

**Issue:** Second entry — must not overwrite the first.
"""


def test_same_day_colliding_slugs_do_not_overwrite(service, vault):
    """Real case from DevVault: two escalations, same date, same 60-char slug.
    Both must survive — losing one is silent data loss."""
    write(vault, "Setup/Escalations.md", DUPLICATE_SLUG)
    git(vault, "add", "-A")
    git(vault, "commit", "-qm", "legacy")

    report = service.migrate()

    assert report.entries == 2
    files = sorted(p.name for p in (vault / ESCALATIONS_DIR).glob("*.md"))
    assert len(files) == 2                       # not 1 — no overwrite
    numbers = {r.number for r in service.list()}
    assert numbers == {6, 7}                     # both mirrored


def test_duplicate_legacy_numbers_are_reassigned_not_crashed(service, vault):
    """The real DevVault log had two entries both headed ESC-006. Migration
    must keep both and give the second a fresh number, not hit the unique
    constraint — and a re-sync must stay stable."""
    write(vault, "Setup/Escalations.md", DUPLICATE_SLUG.replace(
        "### ESC-007 —", "### ESC-006 —"))  # both now ESC-006
    git(vault, "add", "-A")
    git(vault, "commit", "-qm", "legacy")

    service.migrate()                   # must not raise
    service.sync()                      # second pass must also not raise

    rows = service.list()
    assert len(rows) == 2
    numbers = sorted(r.number for r in rows)
    assert numbers[0] == 6
    assert numbers[1] != 6              # the collision was reassigned
    assert len(set(numbers)) == 2


def test_number_already_used_in_db_is_reassigned_not_crashed(service, vault):
    """A new escalation file whose `number:` is already held by a DIFFERENT
    escalation in the DB (numbering diverged across clones/runs) must be
    reassigned, not crash on the UNIQUE(number) constraint."""
    write(vault, f"{ESCALATIONS_DIR}/2026-08-01 - First.md",
          escalation("First", number=38, uuid="11111111-1111-4111-8111-111111111111"))
    service.sync()  # DB row now holds number 38

    second = write(vault, f"{ESCALATIONS_DIR}/2026-08-13 - Second.md",
                   escalation("Second", number=38,
                              uuid="22222222-2222-4222-8222-222222222222"))
    service.sync()  # must not raise on the second, colliding 38

    numbers = sorted(r.number for r in service.list())
    assert len(numbers) == 2
    assert numbers[0] == 38                                  # the original keeps 38
    assert len(set(numbers)) == 2                            # the second is distinct
    assert parse_frontmatter(second.read_text())["number"] != 38  # file rewritten


def test_migration_is_idempotent(service, vault):
    write(vault, "Setup/Escalations.md", LEGACY)
    git(vault, "add", "-A")
    git(vault, "commit", "-qm", "legacy")
    first = service.migrate()
    second = service.migrate()
    assert first.entries == 3
    assert second.already_migrated is True
    assert len(list((vault / ESCALATIONS_DIR).glob("*.md"))) == 2  # no duplicates


def test_migration_dry_run_writes_nothing(service, vault):
    write(vault, "Setup/Escalations.md", LEGACY)
    report = service.migrate(dry_run=True)
    assert report.dry_run and report.entries == 3
    assert len(report.files) == 3
    assert not (vault / ESCALATIONS_DIR).glob("*.md") or \
        list((vault / ESCALATIONS_DIR).glob("*.md")) == []
    assert MIGRATED_MARKER not in (vault / "Setup/Escalations.md").read_text()


def test_migrate_on_an_already_migrated_vault_seeds_the_db(service, vault, tmp_path):
    """The fresh-host path: files exist (pulled from git), DB is empty."""
    write(vault, "Setup/Escalations.md", LEGACY)
    git(vault, "add", "-A")
    git(vault, "commit", "-qm", "legacy")
    service.migrate()

    # a second service with a brand-new DB, same vault
    config = Config(env="dev", vault_path=vault, db_path=tmp_path / "fresh.db")
    init_db(config.db_path)
    fresh = EscalationService(make_session_factory(config.db_path), config)

    report = fresh.migrate()

    assert report.already_migrated is True
    assert {r.number for r in fresh.list()} == {35, 36, 37}
