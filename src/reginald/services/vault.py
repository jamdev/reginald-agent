"""VaultService — deterministic vault-wide maintenance (no LLM).

UUID/publish-flag backfill, plus `git()`: an arbitrary git command run against
the vault under the run lock. That lock is the point — a raw `git` invoked
directly on the host checkout could `reset --hard` under a working agent, but
one routed through here queues behind any run instead. reginald never depends
on uncommitted status as state (all state is commit-based), so external git use
between runs is safe; `git()` just makes it convenient and lock-safe.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from ..core.config import Config
from ..core.git import VaultGit
from ..core.locking import run_lock
from ..core.observability import logger
from ..publisher.frontmatter import (
    collapse_duplicate_frontmatter_keys,
    parse_frontmatter,
    upsert_frontmatter_key,
)
from ..publisher.ignore import load_publish_ignore

SKIP_DIRS = {".obsidian", ".git", ".trash"}


class VaultSyncError(RuntimeError):
    """A vault git operation refused before running (e.g. not a repo)."""


class GitCommandResult(BaseModel):
    returncode: int
    stdout: str = ""
    stderr: str = ""


class BackfillResult(BaseModel):
    scanned: int = 0
    updated: int = 0
    already_had: int = 0
    dry_run: bool = False
    updated_files: list[str] = Field(default_factory=list)


class PublishFlagResult(BaseModel):
    scanned: int = 0
    set_true: int = 0     # newly published (was missing the flag)
    set_false: int = 0    # forced hidden (matched .publish-ignore)
    unchanged: int = 0
    dry_run: bool = False
    changed_files: list[str] = Field(default_factory=list)


class NormalizeResult(BaseModel):
    scanned: int = 0
    fixed: int = 0
    dry_run: bool = False
    # relative path -> the top-level keys that were duplicated in that file
    fixed_files: dict[str, list[str]] = Field(default_factory=dict)


def _has_valid_uuid(text: str) -> bool:
    value = parse_frontmatter(text).get("uuid")
    if not value:
        return False
    try:
        UUID(str(value))
        return True
    except ValueError:
        return False


class VaultService:
    def __init__(self, config: Config):
        self._config = config
        self._git = VaultGit(
            config.vault_path,
            author_name=config.git.author_name,
            author_email=config.git.author_email,
        )

    # -- git ---------------------------------------------------------------

    def git(self, args: list[str]) -> GitCommandResult:
        """Run an arbitrary git command against the vault, under the run lock.

        The lock is what makes this safe where a raw external `git` is not: it
        queues behind any in-progress task rather than mutating the working
        tree under a running agent. reginald's git identity is injected (so
        `git commit` works on a host with no ~/.gitconfig); pass a trailing
        `-c user.name=… -c user.email=…` to commit under your own name.

        git's own exit code and output are returned verbatim — this is a
        passthrough, not a wrapper, so `git status`, `log`, `pull`, `push`,
        `reset`, etc. all behave exactly as they would on the command line.
        """
        git = self._require_repo()
        with run_lock(self._config.lock_path, blocking=True):
            result = git.passthrough(args)
        logger.info("vault git %s -> exit %d", " ".join(args), result.returncode)
        return GitCommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def _require_repo(self) -> VaultGit:
        if not self._git.is_repo():
            raise VaultSyncError(f"{self._config.vault_path} is not a git repository")
        return self._git

    def normalize_frontmatter(self, dry_run: bool = False) -> NormalizeResult:
        """Collapse duplicate top-level frontmatter keys across the vault.

        Duplicate mapping keys are invalid YAML; every parser silently keeps the
        last occurrence, so an agent that emits a key twice (seen with `uuid:`)
        leaves a stale earlier copy that a later divergent edit would silently
        win. This rewrites each affected file to keep only the last occurrence —
        making the file say what is already read, so values never change.

        Runs before `backfill_uuids`/`set_publish_flags` in a task's post-steps
        so those writes operate on clean, single-keyed frontmatter. Idempotent;
        no-op on well-formed files, so it raises no content events.
        """
        result = NormalizeResult(dry_run=dry_run)
        vault = self._config.vault_path
        for path in sorted(vault.rglob("*.md")):
            relative = path.relative_to(vault)
            if any(part in SKIP_DIRS for part in relative.parts):
                continue
            result.scanned += 1
            text = path.read_text()
            new_text, collapsed = collapse_duplicate_frontmatter_keys(text)
            if not collapsed:
                continue
            result.fixed += 1
            result.fixed_files[str(relative)] = collapsed
            logger.warning(
                "duplicate frontmatter key(s) %s in %s",
                ", ".join(collapsed),
                relative,
            )
            if not dry_run:
                path.write_text(new_text)
        logger.info(
            "frontmatter normalize%s: %d scanned, %d fixed",
            " (dry-run)" if dry_run else "",
            result.scanned,
            result.fixed,
        )
        return result

    def backfill_uuids(self, dry_run: bool = False) -> BackfillResult:
        """Add a frontmatter `uuid:` to every .md file that lacks one.
        Idempotent; never touches an existing valid uuid."""
        result = BackfillResult(dry_run=dry_run)
        vault = self._config.vault_path
        for path in sorted(vault.rglob("*.md")):
            relative = path.relative_to(vault)
            if any(part in SKIP_DIRS for part in relative.parts):
                continue
            result.scanned += 1
            text = path.read_text()
            if _has_valid_uuid(text):
                result.already_had += 1
                continue
            result.updated += 1
            result.updated_files.append(str(relative))
            if not dry_run:
                path.write_text(upsert_frontmatter_key(text, "uuid", str(uuid4())))
        logger.info(
            "uuid backfill%s: %d scanned, %d updated, %d already had",
            " (dry-run)" if dry_run else "",
            result.scanned,
            result.updated,
            result.already_had,
        )
        return result

    def set_publish_flags(self, dry_run: bool = False) -> PublishFlagResult:
        """Ensure every note carries a `publish:` flag so obsidian-headless
        publishes it (it only publishes files with `publish: true`).

        - A note matching `.publish-ignore` (default: Archive) is forced to
          `publish: false`, overwriting any prior value — so moving a note into
          an ignored folder hides it on the next run.
        - Any other note MISSING the flag gets `publish: true`. A note that
          already has a `publish` value is left alone, so a human who set
          `publish: false` on a live note keeps it hidden.

        Frontmatter-only, so it raises no content events; idempotent after the
        first pass.
        """
        result = PublishFlagResult(dry_run=dry_run)
        vault = self._config.vault_path
        ignore = load_publish_ignore(vault)
        for path in sorted(vault.rglob("*.md")):
            relative = path.relative_to(vault)
            if any(part in SKIP_DIRS for part in relative.parts):
                continue
            result.scanned += 1
            text = path.read_text()
            current = parse_frontmatter(text).get("publish")
            if ignore.match(relative.as_posix()):
                if current is False:
                    result.unchanged += 1
                    continue
                result.set_false += 1
                result.changed_files.append(str(relative))
                if not dry_run:
                    path.write_text(upsert_frontmatter_key(text, "publish", "false"))
            elif current is None:
                result.set_true += 1
                result.changed_files.append(str(relative))
                if not dry_run:
                    path.write_text(upsert_frontmatter_key(text, "publish", "true"))
            else:
                result.unchanged += 1
        logger.info(
            "publish flags%s: %d scanned, %d published, %d hidden, %d unchanged",
            " (dry-run)" if dry_run else "",
            result.scanned, result.set_true, result.set_false, result.unchanged,
        )
        return result
