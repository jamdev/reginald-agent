"""The .publish-ignore matcher (gitignore subset) and set_publish_flags."""

from __future__ import annotations

import pytest

from reginald.core.config import Config
from reginald.publisher.frontmatter import parse_frontmatter
from reginald.publisher.ignore import compile_ignore, load_publish_ignore
from reginald.services.vault import VaultService


# -- the matcher -------------------------------------------------------------


def test_folder_name_excludes_the_folder_and_its_contents():
    ig = compile_ignore(["Archive"])
    assert ig.match("Archive") is True
    assert ig.match("Archive/2026/brief.md") is True
    assert ig.match("Topics/A/Briefs/b.md") is False


def test_component_boundaries_are_respected():
    ig = compile_ignore(["Archive"])
    assert ig.match("MyArchive/x.md") is False          # substring, not a component
    assert ig.match("Setup/Archive notes.md") is False  # a file, not the folder


def test_comments_and_blank_lines_are_skipped():
    ig = compile_ignore(["# a comment", "", "   ", "Archive"])
    assert ig.match("Archive/x.md") is True
    assert ig.match("Topics/x.md") is False


def test_trailing_slash_matches_directory_contents_only():
    ig = compile_ignore(["Drafts/"])
    assert ig.match("Drafts/x.md") is True
    assert ig.match("Drafts") is False  # a file named Drafts is not the dir


def test_anchored_pattern_only_matches_at_root():
    ig = compile_ignore(["/Setup"])
    assert ig.match("Setup/x.md") is True
    assert ig.match("Topics/Setup/x.md") is False


def test_glob_star_matches_within_a_segment():
    ig = compile_ignore(["*.tmp"])
    assert ig.match("a/b/c.tmp") is True
    assert ig.match("a/b/c.md") is False


def test_negation_reincludes_a_later_match():
    ig = compile_ignore(["Archive", "!Archive/keep.md"])
    assert ig.match("Archive/x.md") is True
    assert ig.match("Archive/keep.md") is False         # last match wins


def test_absent_file_ignores_nothing(tmp_path):
    ig = load_publish_ignore(tmp_path)  # no .publish-ignore present
    assert ig.match("Archive/x.md") is False


# -- set_publish_flags -------------------------------------------------------


def write(vault, rel, fm_lines):
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "---\ntitle: T\n" + "".join(f"{ln}\n" for ln in fm_lines) + "---\n\nbody\n"
    path.write_text(body)
    return path


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    (v / ".publish-ignore").write_text("Archive\n")
    return v


@pytest.fixture
def service(vault, tmp_path):
    return VaultService(Config(env="dev", vault_path=vault, db_path=tmp_path / "t.db"))


def _publish(path):
    return parse_frontmatter(path.read_text()).get("publish")


def test_missing_flag_on_a_normal_note_becomes_true(service, vault):
    p = write(vault, "Topics/A/Briefs/new.md", [])
    result = service.set_publish_flags()
    assert _publish(p) is True
    assert result.set_true == 1


def test_existing_flag_is_left_alone(service, vault):
    ptrue = write(vault, "Topics/A/Briefs/keep-true.md", ["publish: true"])
    pfalse = write(vault, "Topics/A/Briefs/hidden-by-human.md", ["publish: false"])
    service.set_publish_flags()
    assert _publish(ptrue) is True
    assert _publish(pfalse) is False        # respects a human's deliberate hide


def test_archive_note_is_forced_false_even_if_it_was_true(service, vault):
    p = write(vault, "Archive/old.md", ["publish: true"])
    result = service.set_publish_flags()
    assert _publish(p) is False              # moving into Archive hides it
    assert result.set_false == 1


def test_archive_note_missing_flag_becomes_false(service, vault):
    p = write(vault, "Archive/old.md", [])
    service.set_publish_flags()
    assert _publish(p) is False


def test_idempotent_second_pass_changes_nothing(service, vault):
    write(vault, "Topics/A/Briefs/new.md", [])
    write(vault, "Archive/old.md", ["publish: true"])
    first = service.set_publish_flags()
    second = service.set_publish_flags()
    assert first.set_true + first.set_false == 2
    assert second.set_true == 0 and second.set_false == 0


def test_dry_run_writes_nothing(service, vault):
    p = write(vault, "Topics/A/Briefs/new.md", [])
    result = service.set_publish_flags(dry_run=True)
    assert _publish(p) is None               # unchanged on disk
    assert result.set_true == 1              # but reported
