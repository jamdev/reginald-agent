"""The triage task's local MCP server. Its parent run holds the vault lock."""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from contextlib import ExitStack
from pathlib import Path

from ..core.git import VaultGit
from ..publisher.callouts import ACTIONABLE_TYPES, parse_callouts, walk
from ..publisher.diff import BRIEF_RE, substantive_content


def _request_only(text: str) -> bool:
    return not substantive_content(text) and any(
        callout.type in ACTIONABLE_TYPES and not callout.children
        for callout, _ in walk(parse_callouts(text))
    )


def _parent_fd(stack: ExitStack, root: int, path: Path, *, create: bool = False) -> int:
    """Resolve each parent under the open vault directory without following symlinks."""
    parent = root
    for name in path.parent.parts:
        if create:
            try:
                os.mkdir(name, dir_fd=parent)
            except FileExistsError:
                pass
        parent = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        stack.callback(os.close, parent)
    return parent


def _rename_exclusive(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    # os.rename replaces a racing destination. Use the single-operation,
    # no-replace variant provided by our supported Ubuntu and macOS hosts.
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename, flags = libc.renameatx_np, 0x4  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename, flags = libc.renameat2, 0x1  # RENAME_NOREPLACE
    else:
        raise OSError("Exclusive inbox moves require Linux or macOS")
    if rename(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), flags) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), dst)


def move_inbox_file(vault: Path, source: str, destination: str) -> str:
    src, dst = Path(source), Path(destination)
    for path in (src, dst):
        if "\0" in str(path) or path.is_absolute() or any(part.startswith(".") for part in path.parts):
            raise ValueError("Use vault-relative paths without hidden or parent components")
    if len(src.parts) < 2 or src.parts[0] != "Inbox":
        raise ValueError("Source must be a file under Inbox/")
    filing_folder = dst.parent.parts
    if not (BRIEF_RE.fullmatch(dst.as_posix()) or filing_folder == ("Templates",)
            or (len(filing_folder) == 2 and filing_folder[0] == "Sources")):
        raise ValueError("File into Topics/<topic>/Briefs/, Sources/<topic>/ or Templates/")
    if src.suffix.lower() != dst.suffix.lower():
        raise ValueError("Keep the source file's extension when filing")

    with ExitStack() as stack:
        root = os.open(vault, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, root)
        src_fd = _parent_fd(stack, root, src)
        if not stat.S_ISREG(os.stat(src.name, dir_fd=src_fd, follow_symlinks=False).st_mode):
            raise ValueError("Source must be a regular file")
        if src.suffix.lower() == ".md":
            fd = os.open(src.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_fd)
            with os.fdopen(fd, encoding="utf-8") as note:
                held = _request_only(note.read())
            # Triage may already have edited the note. Also check the version
            # pulled at run start, before those edits or the next queue scan.
            if not held:
                original = VaultGit(vault).show_file_if_present("HEAD", src.as_posix())
                held = original is not None and _request_only(original)
            if held:
                raise ValueError("Leave this request in Inbox/ until its callout has a nested reply")
        dst_fd = _parent_fd(stack, root, dst, create=True)
        _rename_exclusive(src_fd, src.name, dst_fd, dst.name)
    return f"Moved {src} to {dst}"


def main(vault: Path) -> None:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("inbox")

    @server.tool()
    def move_file(source: str, destination: str) -> str:
        """File one Inbox file at a new vault-relative path. Use this before editing
        the filed note or updating indexes. Destinations are Topics/<topic>/Briefs/,
        Sources/<topic>/ and Templates/. Leave a callout-only note with an unanswered
        todo or question untouched for action-edits. On collision leave the source
        in place. Never copy over the destination as a fallback.
        """
        return move_inbox_file(vault, source, destination)

    server.run()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
