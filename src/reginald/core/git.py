"""Vault Git operations. One commit per run; diffs drive the publisher."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .observability import span


class GitError(RuntimeError):
    pass


class RebaseConflictError(GitError):
    """A rebase hit a conflict. The rebase has been aborted; the tree is back
    at its pre-rebase state. Never auto-resolved — a human decides."""


# git's well-known empty tree object: the "parent" of a root commit, so the
# first commit in a repo diffs as all-additions like any other.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
RUN_TRAILER = "Reginald-Run"


@dataclass
class DiffEntry:
    status: str  # A | M | D | R
    path: str
    old_path: str | None = None  # renames: where the file came from


@dataclass
class CommitInfo:
    """One commit with its author, subject and position in vault history."""

    sha: str
    author_name: str
    author_email: str
    subject: str
    run_trailer: str = ""


class VaultGit:
    def __init__(
        self,
        vault_path: Path,
        author_name: str = "Reginald",
        author_email: str = "reginald@localhost",
    ):
        self.vault_path = Path(vault_path).expanduser()
        self.author_name = author_name
        self.author_email = author_email

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        # Identity is passed per-invocation rather than left to host config.
        # The service user has no ~/.gitconfig, so every commit would otherwise
        # fail with "Author identity unknown" — and configuring it during
        # install would break again on the next host or vault re-clone.
        # -c sets user.name/user.email, which cover author and committer both.
        result = subprocess.run(
            [
                "git",
                "-c", f"user.name={self.author_name}",
                "-c", f"user.email={self.author_email}",
                "-C", str(self.vault_path),
                *args,
            ],
            capture_output=True,
            text=True,
            check=False,  # handled below, so failures raise GitError not CalledProcessError
        )
        if check and result.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {result.stderr.strip()}")
        return result

    def passthrough(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run an arbitrary git command in the vault for the operator.

        Reginald's identity is injected first (the service user has no
        ~/.gitconfig, so `git commit` would otherwise fail), and the caller can
        override it with their own trailing `-c user.name=…`. Never raises on a
        non-zero exit — the caller propagates git's code. Credential prompts
        fail fast rather than hang: there is no TTY to answer them here.
        """
        return subprocess.run(
            [
                "git",
                "-c", f"user.name={self.author_name}",
                "-c", f"user.email={self.author_email}",
                "-C", str(self.vault_path),
                *args,
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

    def is_repo(self) -> bool:
        try:
            return self._git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
        except (GitError, FileNotFoundError):
            return False

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def rev_parse(self, ref: str) -> str:
        """Resolve a ref (HEAD~1, a tag, a short sha) to a full commit hash."""
        return self._git("rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()

    def is_dirty(self) -> bool:
        return bool(self._git("status", "--porcelain").stdout.strip())

    def remote_head(self, remote: str, branch: str) -> str | None:
        """The remote-tracking ref's commit, or None if it is not there yet.

        Read from the local ref, so it is only as fresh as the last fetch —
        enough to tell whether a push has anything to send.
        """
        try:
            return self.rev_parse(f"{remote}/{branch}")
        except GitError:
            return None

    @span("vault.commit", log_when_disabled=False)
    def commit_all(self, message: str) -> str | None:
        """Stage everything and commit. Returns new HEAD, or None if no changes."""
        self._git("add", "-A")
        if not self._git("diff", "--cached", "--quiet", check=False).returncode:
            return None  # nothing staged
        self._git("commit", "-m", message)
        return self.head()

    @span("vault.push", log_when_disabled=False)
    def push(self, remote: str, branch: str) -> str:
        """Push, returning git's own report of what it did. git writes that to
        stderr even on success ("Everything up-to-date"), so both streams are
        collected — callers that ignore the return value are unaffected."""
        result = self._git("push", remote, branch)
        return (result.stdout + result.stderr).strip()

    @span("vault.reset", log_when_disabled=False)
    def reset_hard(self, ref: str) -> None:
        self._git("reset", "--hard", ref)
        self._git("clean", "-fd")

    def diff_name_status(self, pre: str, post: str) -> list[DiffEntry]:
        out = self._git("diff", "--name-status", "-M", f"{pre}..{post}").stdout
        entries: list[DiffEntry] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0][0]  # R100 -> R
            path = parts[-1]  # for renames, the new path
            old_path = parts[1] if status == "R" and len(parts) == 3 else None
            entries.append(DiffEntry(status=status, path=path, old_path=old_path))
        return entries

    # -- per-commit history --------------------------------------

    def commits_between(self, pre: str, post: str) -> list[CommitInfo]:
        """Commits in pre..post, oldest first, with real author identities.

        \\x1f (unit separator) as the field delimiter: commit subjects can
        contain anything printable, but not control characters.
        """
        trailer = f"%(trailers:key={RUN_TRAILER},valueonly,separator=%x20)"
        out = self._git(
            "log", "--reverse", f"--format=%H%x1f%an%x1f%ae%x1f%s%x1f{trailer}",
            f"{pre}..{post}",
        ).stdout
        commits: list[CommitInfo] = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 5:
                continue
            commits.append(CommitInfo(*parts))
        return commits

    def parent_of(self, sha: str) -> str:
        """First parent, or the empty tree for a root commit — so every
        commit can be diffed the same way."""
        result = self._git("rev-parse", "--verify", "--quiet", f"{sha}^", check=False)
        if result.returncode != 0:
            return EMPTY_TREE
        return result.stdout.strip()

    def first_added_date(self, path: str) -> str | None:
        """ISO date of the commit that first added the file, or None."""
        out = self._git(
            "log", "--diff-filter=A", "--follow", "--format=%ad", "--date=short",
            "--", path, check=False,
        ).stdout.strip()
        return out.splitlines()[-1] if out else None

    # -- multi-author sync -------------------------------------------------

    @span("vault.sync", log_when_disabled=False)
    def sync_with_remote(self, remote: str, branch: str) -> None:
        """Fetch, then rebase local unpushed commits onto the remote branch.

        Replaces `pull --ff-only` for the run pipeline: with contributors
        pushing concurrently, ff-only deadlocks the moment Reginald holds an
        unpushed commit. Rebasing is safe here because Reginald's local
        commits are never shared until pushed. A conflict aborts the rebase
        (tree restored) and raises — never auto-resolved.
        """
        self._git("fetch", remote)
        if self.remote_head(remote, branch) is None:
            return  # nothing to rebase onto (fresh remote)
        result = self._git("rebase", f"{remote}/{branch}", check=False)
        if result.returncode != 0:
            self._git("rebase", "--abort", check=False)
            raise RebaseConflictError(
                f"rebase onto {remote}/{branch} conflicted and was aborted: "
                f"{(result.stdout + result.stderr).strip()[-500:]}"
            )

    def push_with_rebase(self, remote: str, branch: str) -> str:
        """Push; if the remote moved first (contributor race), rebase once and
        retry. A second failure raises."""
        try:
            return self.push(remote, branch)
        except GitError:
            self.sync_with_remote(remote, branch)
            return self.push(remote, branch)

    def show_file(self, ref: str, path: str) -> str:
        return self._git("show", f"{ref}:{path}").stdout

    def show_file_if_present(self, ref: str, path: str) -> str | None:
        if self._git("ls-tree", ref, "--", f":(literal){path}").stdout:
            return self.show_file(ref, path)
        return None
