"""YAML frontmatter parsing and editing — pure functions, no dependencies to inject."""

from __future__ import annotations

import re
from collections import Counter

import yaml

# A top-level frontmatter key: `name:` at column 0 (nested/list lines are
# indented, so they never match — leaving inline dicts and list items alone).
_TOP_KEY = re.compile(r"^([A-Za-z0-9_][\w-]*)\s*:")


def parse_frontmatter(text: str) -> dict:
    """Return the YAML frontmatter of a markdown document, or {}."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def upsert_frontmatter_key(text: str, key: str, value: str) -> str:
    """Set a scalar frontmatter key, preserving the rest of the file verbatim.

    Replaces the key's line if present, appends it to the block if not, and
    creates a frontmatter block if the document has none.
    """
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            lines = parts[1].split("\n")
            pattern = re.compile(rf"^{re.escape(key)}\s*:")
            for i, line in enumerate(lines):
                if pattern.match(line):
                    lines[i] = f"{key}: {value}"
                    break
            else:
                # insert before the trailing empty line that precedes the closing ---
                insert_at = len(lines) - 1 if lines and lines[-1] == "" else len(lines)
                lines.insert(insert_at, f"{key}: {value}")
            return "---" + "\n".join(lines) + "---" + parts[2]
    return f"---\n{key}: {value}\n---\n\n{text}"


def collapse_duplicate_frontmatter_keys(text: str) -> tuple[str, list[str]]:
    """Remove duplicate top-level frontmatter keys, keeping the LAST occurrence.

    Duplicate mapping keys are invalid YAML; every parser (PyYAML, Obsidian)
    silently resolves them by keeping the last, so this makes the file say what
    is already being read — no value change — while removing the latent trap of
    a stale earlier copy that a divergent edit would silently win.

    Operates only on **top-level** keys (column 0); indented list items and
    inline dicts (e.g. `key_dates`, `changelog` entries) are untouched. A key's
    whole span — its line plus any indented continuation — is dropped for
    earlier occurrences. Returns (new_text, sorted list of collapsed keys);
    a no-op returns the text unchanged.
    """
    if not text.startswith("---"):
        return text, []
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text, []

    # Segment the block into (key | None, lines): a top-level `key:` line starts
    # a new segment; blanks, comments and indented lines extend the current one.
    segments: list[tuple[str | None, list[str]]] = []
    for line in parts[1].split("\n"):
        match = _TOP_KEY.match(line)
        if match and not line[:1].isspace():
            segments.append((match.group(1), [line]))
        elif segments:
            segments[-1][1].append(line)
        else:
            segments.append((None, [line]))  # preamble (leading blank line)

    counts = Counter(k for k, _ in segments if k is not None)
    dupes = sorted(k for k, c in counts.items() if c > 1)
    if not dupes:
        return text, []

    last_index = {k: i for i, (k, _) in enumerate(segments) if k is not None}
    kept = [
        seg_lines
        for i, (k, seg_lines) in enumerate(segments)
        if not (k is not None and counts[k] > 1 and last_index[k] != i)
    ]
    new_block = "\n".join(line for seg in kept for line in seg)
    return "---" + new_block + "---" + parts[2], dupes
