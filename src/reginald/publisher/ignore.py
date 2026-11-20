"""gitignore-style matching for `.publish-ignore`.

A focused subset of the .gitignore format, matched against POSIX-relative
paths (last matching pattern wins):

- `# ...` comment lines and blank lines are skipped.
- `!pattern` negates (re-includes) a previously-ignored path.
- a trailing `/` makes a pattern match a directory's contents only.
- a leading `/`, or any internal `/`, anchors the pattern to the root; a
  pattern with no internal `/` matches at any depth (by name).
- `*` matches within a path segment, `**` matches across segments, `?` matches
  one non-`/` character.

A folder name like `Archive` therefore excludes the folder and everything
under it. Not supported (falls back to literal): character classes `[...]`,
escaped metacharacters. If you need those, the fix is to swap in `pathspec`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

IGNORE_FILE = ".publish-ignore"


@dataclass(frozen=True)
class _Rule:
    regex: re.Pattern[str]
    negate: bool


def _translate(pattern: str) -> str:
    """Glob (gitignore semantics) → a regex body over a full relative path."""
    dir_only = pattern.endswith("/")
    body = pattern[:-1] if dir_only else pattern
    anchored = body.startswith("/") or "/" in body
    body = body.lstrip("/")

    out: list[str] = []
    i = 0
    while i < len(body):
        if body[i : i + 2] == "**":
            out.append(".*")
            i += 2
            if i < len(body) and body[i] == "/":  # collapse **/ into one .*
                i += 1
        elif body[i] == "*":
            out.append("[^/]*")
            i += 1
        elif body[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(body[i]))
            i += 1

    core = "".join(out)
    prefix = "^" if anchored else r"(?:^|.*/)"
    # dir-only: only the directory's contents (files under it). Otherwise the
    # path itself OR anything under it (so a folder name catches its contents).
    suffix = "/.*$" if dir_only else r"(?:/.*)?$"
    return prefix + core + suffix


class PublishIgnore:
    """Compiled `.publish-ignore`. `match(rel_posix_path)` → is it excluded."""

    def __init__(self, rules: list[_Rule]):
        self._rules = rules

    def match(self, rel_path: str) -> bool:
        ignored = False
        for rule in self._rules:
            if rule.regex.match(rel_path):
                ignored = not rule.negate
        return ignored


def compile_ignore(lines: Iterable[str]) -> PublishIgnore:
    rules: list[_Rule] = []
    for raw in lines:
        line = raw.rstrip("\n").rstrip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        if not line:
            continue
        rules.append(_Rule(re.compile(_translate(line)), negate))
    return PublishIgnore(rules)


def load_publish_ignore(vault_path: Path) -> PublishIgnore:
    """Read `<vault>/.publish-ignore`, or an empty matcher when it is absent
    (nothing excluded — every note is eligible to publish)."""
    path = vault_path / IGNORE_FILE
    lines = path.read_text().splitlines() if path.exists() else []
    return compile_ignore(lines)
