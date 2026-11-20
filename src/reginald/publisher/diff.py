"""Change detection: one commit's diff → RawEvents.

Pure logic over DiffEntry lists + file readers; IdentifyChangesService supplies the
git plumbing (per-commit, with real author attribution) and persists the
results as content_events rows.

The substantive-change rule lives here: a modified note raises an `updated`
event only if its body, with callout blocks stripped, actually changed —
dialogue is not content, frontmatter housekeeping is not content, and a
rename that moves unchanged content is not content.

"Escalation" has exactly one meaning: a note raised in Escalations/. A brief's
`flag:` frontmatter is a marker for humans reading the vault and raises no
event of its own — it used to mint `escalation` events too, which meant the
link checker's `flag: source-unavailable` housekeeping mailed subscribers an
ESCALATION for a broken URL.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ..core.git import DiffEntry
from .callouts import strip_callouts
from .frontmatter import parse_frontmatter

BRIEF_RE = re.compile(r"^Topics/(?P<topic>[^/]+)/Briefs/(?P<name>[^/]+\.md)$")
MATERIALS_RE = re.compile(r"^Topics/(?P<topic>[^/]+)/Materials/(?P<name>[^/]+\.md)$")
# Change detection may replay commits from before the log directory moved. Both locations
# hold run reports, including any callout examples quoted inside those reports.
RUN_LOG_PREFIXES = ("Setup/Logs/", "Setup/Tasks/Logs/")
# An escalation is a note in Escalations/ — that folder is the *only* thing
# that makes something an escalation. `[^/]+` deliberately excludes
# Escalations/Resolved/, so resolving one (a file move) raises nothing: the
# raise is the news, the resolution is bookkeeping.
ESCALATION_RE = re.compile(r"^Escalations/(?P<name>[^/]+\.md)$")
ESCALATION_INDEX = "Escalations/Index.md"
# Routing bucket for an escalation that names no topic, or one outside the
# taxonomy. Defined here (the producer) and re-exported by services.topics
# alongside __operational__; services already depend on publisher, not the
# other way round.
UNROUTED_TOPIC = "__unrouted__"


@dataclass
class RawEvent:
    topic: str
    kind: str  # brief_created | brief_updated | escalation | digest | report
    file_path: str
    commit_hash: str
    note_uuid: str | None = None  # the note's frontmatter uuid — rename-stable identity
    title: str | None = None
    jurisdiction: str | None = None
    effective_date: str | None = None
    summary: str | None = None  # digest one-liner (changelog note / commit subject)
    meta: dict = field(default_factory=dict)
    secondary_topics: list[str] = field(default_factory=list)

    def meta_json(self) -> str:
        return json.dumps(self.meta, default=str)


def substantive_content(text: str) -> str:
    """A note's content for change comparison: body only (frontmatter
    dropped), callout blocks stripped."""
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            body = parts[2]
    return strip_callouts(body).strip()


def _new_changelog_note(pre_text: str | None, post_text: str) -> str | None:
    """The `note` of a changelog entry added by this change, if any — the
    vault's own convention for saying what changed, reused as the email
    one-liner."""
    try:
        post_cl = parse_frontmatter(post_text).get("changelog") or []
        pre_cl = (parse_frontmatter(pre_text).get("changelog") or []) if pre_text else []
    except Exception:  # noqa: BLE001 — malformed frontmatter must not drop the event
        return None
    if not isinstance(post_cl, list):
        return None
    fresh = [e for e in post_cl if e not in (pre_cl if isinstance(pre_cl, list) else [])]
    for entry in reversed(fresh):
        if isinstance(entry, dict) and entry.get("note"):
            return str(entry["note"])
    return None


def update_summary(pre_text: str | None, post_text: str, commit_subject: str) -> str:
    note = _new_changelog_note(pre_text, post_text)
    if note:
        return note[:200]
    if commit_subject.strip():
        return commit_subject.strip()[:100]
    return "updated"


def events_from_diff(
    entries: list[DiffEntry],
    commit_hash: str,
    read_file: Callable[[str], str],
    read_pre: Callable[[str], str | None],
    commit_subject: str = "",
    only_topic: str | None = None,
) -> list[RawEvent]:
    """Map one commit's name-status diff onto content events.

    read_file: post-commit content of a repo-relative path
    read_pre:  pre-commit content (None when the file did not exist) —
               for renames it is called with the OLD path
    only_topic: restrict events to one topic (per-topic runs)
    """
    events: list[RawEvent] = []
    for entry in entries:
        if entry.status == "D":
            continue  # deletions raise no subscriber-facing events
        if entry.path.startswith(RUN_LOG_PREFIXES):
            continue  # run logs are bookkeeping, not content

        brief = BRIEF_RE.match(entry.path)
        materials = MATERIALS_RE.match(entry.path)
        escalation = ESCALATION_RE.match(entry.path)

        if escalation:
            # Only a newly *added* file is a raise. Modifications are the
            # lifecycle — status changes, ESC-NNN assignment by
            # escalations-sync, a human appending a resolution note — and a
            # rename is the move into Resolved/. None of those re-notify.
            if entry.status != "A" or entry.path == ESCALATION_INDEX:
                continue
            try:
                fm = parse_frontmatter(read_file(entry.path))
            except Exception:  # noqa: BLE001 — malformed frontmatter must not drop an escalation
                fm = {}
            # No topic means no audience of its own — it goes to the unrouted
            # bucket, which the outbox delivers to all-topics subscribers.
            # Dropping it would lose the raise entirely.
            topic = str(fm.get("topic") or UNROUTED_TOPIC)
            if only_topic and topic != only_topic:
                continue
            events.append(
                RawEvent(
                    topic=topic,
                    kind="escalation",
                    file_path=entry.path,
                    commit_hash=commit_hash,
                    note_uuid=_valid_uuid(fm.get("uuid")),
                    title=fm.get("title") or _title_from_path(entry.path),
                    summary=fm.get("raised_by") or commit_subject.strip()[:100] or None,
                    meta={
                        k: fm[k]
                        for k in ("priority", "raised_by", "raised_date", "status", "number")
                        if k in fm
                    },
                )
            )
        elif brief:
            topic = brief.group("topic")
            try:
                post_text = read_file(entry.path)
                fm = parse_frontmatter(post_text)
            except Exception:  # noqa: BLE001 — malformed frontmatter must not drop the event
                post_text, fm = "", {}

            pre_text = None
            if entry.status in ("M", "R"):
                try:
                    pre_text = read_pre(entry.old_path or entry.path)
                except Exception:  # noqa: BLE001
                    pre_text = None

            if pre_text is None:
                kind = "brief_created"
                summary = fm.get("title") or _title_from_path(entry.path)
            else:
                # The substantive-change rule: callout dialogue, frontmatter
                # housekeeping and pure renames are not content. `flag:` is
                # frontmatter, so it no longer forces an event of its own —
                # raising an Escalations/ note is what notifies.
                if substantive_content(pre_text) == substantive_content(post_text):
                    continue
                kind = "brief_updated"
                summary = update_summary(pre_text, post_text, commit_subject)

            meta = {
                k: fm[k]
                for k in ("key_dates", "status", "secondary_topics", "flag", "type")
                if k in fm
            }
            event = RawEvent(
                topic=topic,
                kind=kind,
                file_path=entry.path,
                commit_hash=commit_hash,
                note_uuid=_valid_uuid(fm.get("uuid")),
                title=fm.get("title") or _title_from_path(entry.path),
                jurisdiction=fm.get("jurisdiction"),
                effective_date=str(fm.get("effective_date")) if fm.get("effective_date") else None,
                summary=summary,
                meta=meta,
                secondary_topics=list(fm.get("secondary_topics") or []),
            )
            if not only_topic or topic == only_topic:
                events.append(event)
            for sec in event.secondary_topics:
                if only_topic and sec != only_topic:
                    continue
                events.append(
                    RawEvent(
                        topic=sec,
                        kind=event.kind,
                        file_path=entry.path,
                        commit_hash=commit_hash,
                        note_uuid=event.note_uuid,
                        title=event.title,
                        jurisdiction=event.jurisdiction,
                        effective_date=event.effective_date,
                        summary=event.summary,
                        meta={**meta, "cross_topic_from": topic},
                    )
                )
        elif materials and "Key dates digest" in entry.path:
            topic = materials.group("topic")
            if only_topic and topic != only_topic:
                continue
            events.append(
                RawEvent(
                    topic=topic,
                    kind="digest",
                    file_path=entry.path,
                    commit_hash=commit_hash,
                    title=_title_from_path(entry.path),
                    summary=_title_from_path(entry.path),
                )
            )
    return events


def _title_from_path(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def _valid_uuid(value) -> str | None:
    from uuid import UUID

    try:
        return str(UUID(str(value))) if value else None
    except ValueError:
        return None
