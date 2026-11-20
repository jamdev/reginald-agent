"""Obsidian callout parsing — the collaboration protocol's syntax layer.

Five callout types carry the protocol ([!todo] [!question] [!success]
[!failure] [!warning]); anything else Obsidian supports ([!note], [!info]…)
is inert here. Replies nest one blockquote level under the callout they
answer, so the human-visible thread structure *is* the machine state.

Pure functions over text: no git, no DB, no I/O.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

KNOWN_TYPES = frozenset({"todo", "question", "success", "failure", "warning"})
# Only these create work items — the rest are reports.
ACTIONABLE_TYPES = frozenset({"todo", "question"})

# After quote markers are stripped: "[!Todo]- @reginald check this"
_CALLOUT_START = re.compile(r"^\[!([A-Za-z]+)\][+-]?\s*(.*)$")
_ADDRESSEE = re.compile(r"^@([A-Za-z0-9_-]+)\b\s*(.*)$")
_QUOTE_PREFIX = re.compile(r"^((?:\s{0,3}>\s?)+)")
_FENCE = re.compile(r"^(```|~~~)")


@dataclass
class Callout:
    type: str  # lowercased, one of KNOWN_TYPES
    title: str  # title line with any @addressee removed
    addressee: str | None  # "@reginald ..." in the title, lowercased
    depth: int  # blockquote nesting depth (1 = top-level callout)
    body_lines: list[str] = field(default_factory=list)  # own lines, children excluded
    children: list[Callout] = field(default_factory=list)

    @property
    def body(self) -> str:
        return "\n".join(self.body_lines).strip()

    def normalized(self) -> str:
        """Whitespace-collapsed identity text. Any edit to title or body is a
        new identity — a changed instruction is new work, by design."""
        text = f"{self.title}\n{self.body}"
        return re.sub(r"[ \t]+", " ", text).strip()


def _quote_depth(line: str) -> tuple[int, str]:
    """(blockquote depth, content with quote markers stripped)."""
    match = _QUOTE_PREFIX.match(line)
    if not match:
        return 0, line
    prefix = match.group(1)
    return prefix.count(">"), line[len(prefix):]


def parse_callouts(text: str) -> list[Callout]:
    """Extract known-type callouts (with nesting) from markdown.

    Fenced code blocks are inert wherever they appear — documentation shows
    protocol examples in fences precisely so they never parse.
    """
    top: list[Callout] = []
    stack: list[Callout] = []
    in_fence = False

    for raw in text.splitlines():
        depth, content = _quote_depth(raw)

        if _FENCE.match(content.strip()):
            in_fence = not in_fence
            if stack and depth >= stack[-1].depth:
                stack[-1].body_lines.append(content)
            continue
        if in_fence:
            if stack and depth >= stack[-1].depth:
                stack[-1].body_lines.append(content)
            continue

        # a line below the current callout's depth closes it (and any deeper)
        while stack and depth < stack[-1].depth:
            stack.pop()
        start = _CALLOUT_START.match(content.strip()) if depth > 0 else None
        if start and start.group(1).lower() in KNOWN_TYPES:
            # a new callout at depth d replaces any open callout at >= d
            while stack and stack[-1].depth >= depth:
                stack.pop()
            title = start.group(2).strip()
            addressee = None
            addr = _ADDRESSEE.match(title)
            if addr:
                addressee = addr.group(1).lower()
                title = addr.group(2).strip()
            callout = Callout(
                type=start.group(1).lower(), title=title,
                addressee=addressee, depth=depth,
            )
            if stack:
                stack[-1].children.append(callout)
            else:
                top.append(callout)
            stack.append(callout)
        elif stack and depth >= stack[-1].depth:
            stack[-1].body_lines.append(content)

    return top


def walk(callouts: list[Callout], parent: Callout | None = None):
    """Depth-first (callout, parent) pairs over a parse tree."""
    for callout in callouts:
        yield callout, parent
        yield from walk(callout.children, callout)


def content_hash(note_id: str, callout: Callout) -> str:
    """Stable identity for the ledger: note identity + type + normalised text.

    note_id is the note's frontmatter uuid where present, else its vault
    path — uuid keeps identity stable across renames.
    """
    payload = "\x00".join([note_id, callout.type, callout.normalized()])
    return hashlib.sha256(payload.encode()).hexdigest()


def strip_callouts(text: str) -> str:
    """Remove every known-type callout block (children included).

    The substantive-change rule diffs this: dialogue is not content, so a
    commit that only adds callout threads must compare as unchanged.
    Ordinary blockquotes that are not protocol callouts survive.
    """
    lines = text.splitlines()
    drop: set[int] = set()
    open_depths: list[int] = []  # depths of callout blocks currently open
    in_fence = False

    for idx, raw in enumerate(lines):
        depth, content = _quote_depth(raw)

        if _FENCE.match(content.strip()):
            in_fence = not in_fence
            if open_depths and depth >= open_depths[0]:
                drop.add(idx)
            continue
        if in_fence:
            if open_depths and depth >= open_depths[0]:
                drop.add(idx)
            continue

        while open_depths and depth < open_depths[-1]:
            open_depths.pop()
        if depth == 0 and not content.strip():
            open_depths.clear()
            continue

        start = _CALLOUT_START.match(content.strip()) if depth > 0 else None
        if start and start.group(1).lower() in KNOWN_TYPES:
            while open_depths and open_depths[-1] >= depth:
                open_depths.pop()
            open_depths.append(depth)
            drop.add(idx)
        elif open_depths and depth >= open_depths[0]:
            drop.add(idx)

    kept = [line for idx, line in enumerate(lines) if idx not in drop]
    return "\n".join(kept)
