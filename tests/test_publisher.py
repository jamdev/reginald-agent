from reginald.core.git import DiffEntry
from reginald.publisher.diff import UNROUTED_TOPIC, events_from_diff
from reginald.publisher.frontmatter import (
    collapse_duplicate_frontmatter_keys,
    parse_frontmatter,
)
from reginald.publisher.links import note_url

BRIEF = """---
title: FCA finalises cryptoasset regulatory framework
type: brief
uuid: 9a1b2c3d-0000-4000-8000-000000000001
jurisdiction: UK
topic: Crypto-assets and Digital Assets
secondary_topics: [Payments and E-money]
effective_date: 2026-06-30
---
# Body
"""

FLAGGED = """---
title: OFSI penalty
jurisdiction: UK
flag: enforcement names firm — 2026-06-17
---
"""


def read_file_factory(contents: dict):
    return lambda path: contents[path]


def no_pre(path):  # file did not exist before the commit
    return None


def test_frontmatter_parses():
    fm = parse_frontmatter(BRIEF)
    assert fm["jurisdiction"] == "UK"
    assert fm["secondary_topics"] == ["Payments and E-money"]


def test_frontmatter_handles_non_frontmatter():
    assert parse_frontmatter("# Just a heading") == {}
    assert parse_frontmatter("---\n[broken\n---\n") == {}


# -- collapse_duplicate_frontmatter_keys -------------------------------------

DUPE_UUID = """---
title: A brief
uuid: 11111111-1111-4111-8111-111111111111
jurisdiction: UK
uuid: 11111111-1111-4111-8111-111111111111
---
# Body stays
"""


def test_collapse_removes_a_duplicate_key_keeping_the_last():
    new, collapsed = collapse_duplicate_frontmatter_keys(DUPE_UUID)
    assert collapsed == ["uuid"]
    # exactly one uuid line remains in the frontmatter block
    block = new.split("---", 2)[1]
    assert block.count("uuid:") == 1
    # value is unchanged and still parses
    assert parse_frontmatter(new)["uuid"] == "11111111-1111-4111-8111-111111111111"


def test_collapse_keeps_the_last_value_when_duplicates_differ():
    # PyYAML silently keeps the last; the collapse makes the file say so.
    text = "---\nuuid: aaaa\nuuid: zzzz\n---\nbody\n"
    assert parse_frontmatter(text)["uuid"] == "zzzz"  # documents the parser rule
    new, collapsed = collapse_duplicate_frontmatter_keys(text)
    assert collapsed == ["uuid"]
    assert parse_frontmatter(new)["uuid"] == "zzzz"
    assert "aaaa" not in new


def test_collapse_is_a_no_op_on_clean_frontmatter():
    new, collapsed = collapse_duplicate_frontmatter_keys(BRIEF)
    assert collapsed == []
    assert new == BRIEF


def test_collapse_is_idempotent():
    once, _ = collapse_duplicate_frontmatter_keys(DUPE_UUID)
    twice, collapsed = collapse_duplicate_frontmatter_keys(once)
    assert collapsed == []
    assert twice == once


def test_collapse_leaves_body_and_other_keys_untouched():
    new, _ = collapse_duplicate_frontmatter_keys(DUPE_UUID)
    assert new.endswith("# Body stays\n")
    fm = parse_frontmatter(new)
    assert fm["title"] == "A brief"
    assert fm["jurisdiction"] == "UK"


def test_collapse_preserves_nested_blocks_and_list_items():
    # Indented keys under a mapping/list share the parent's name at column 0,
    # so they must not be mistaken for duplicate top-level keys.
    text = (
        "---\n"
        "title: T\n"
        "key_dates:\n"
        "  - label: Consultation closes\n"
        "    date: 2026-09-01\n"
        "  - label: Rules in force\n"
        "    date: 2026-12-01\n"
        "changelog:\n"
        "  - date: 2026-08-01\n"
        "    note: Initial filing\n"
        "---\n"
        "body\n"
    )
    new, collapsed = collapse_duplicate_frontmatter_keys(text)
    assert collapsed == []
    assert new == text
    # two list entries each with label/date survive
    assert new.count("label:") == 2
    assert new.count("- date:") == 1


def test_collapse_ignores_files_without_frontmatter():
    assert collapse_duplicate_frontmatter_keys("# heading only\n") == (
        "# heading only\n",
        [],
    )
    assert collapse_duplicate_frontmatter_keys("---\nunterminated\n") == (
        "---\nunterminated\n",
        [],
    )


def test_collapse_reports_multiple_duplicated_keys_sorted():
    text = "---\na: 1\nb: 2\na: 3\nb: 4\nc: 5\n---\nbody\n"
    new, collapsed = collapse_duplicate_frontmatter_keys(text)
    assert collapsed == ["a", "b"]
    fm = parse_frontmatter(new)
    assert fm == {"a": 3, "b": 4, "c": 5}


def test_new_brief_creates_event_with_secondary_topic():
    path = "Topics/Crypto-assets and Digital Assets/Briefs/2026-06-30 - FCA crypto - UK.md"
    events = events_from_diff(
        [DiffEntry("A", path)], "abc123", read_file_factory({path: BRIEF}), no_pre
    )
    assert len(events) == 2  # primary + secondary topic
    primary, secondary = events
    assert primary.kind == "brief_created"
    assert primary.topic == "Crypto-assets and Digital Assets"
    assert primary.jurisdiction == "UK"
    assert primary.effective_date == "2026-06-30"
    assert primary.note_uuid == "9a1b2c3d-0000-4000-8000-000000000001"
    assert secondary.topic == "Payments and E-money"
    assert secondary.note_uuid == primary.note_uuid
    assert secondary.meta["cross_topic_from"] == "Crypto-assets and Digital Assets"


def test_modified_brief_is_update():
    path = "Topics/Payments and E-money/Briefs/2026-05-18 - BoE RTGS - UK.md"
    events = events_from_diff(
        [DiffEntry("M", path)],
        "abc",
        read_file_factory({path: "---\ntitle: T\n---\nnew body\n"}),
        lambda p: "---\ntitle: T\n---\nold body\n",
    )
    assert events[0].kind == "brief_updated"


# -- the substantive-change rule --------------------------------------------

PRE = "---\ntitle: T\nuuid: 9a1b2c3d-0000-4000-8000-000000000001\n---\nDeadline is 15 August.\n"


def _update_events(post: str, subject: str = "", status: str = "M", old_path: str | None = None):
    path = "Topics/Payments and E-money/Briefs/2026-05-18 - BoE RTGS - UK.md"
    entry = DiffEntry(status, path, old_path=old_path)
    return events_from_diff(
        [entry], "abc", read_file_factory({path: post}),
        lambda p: PRE, commit_subject=subject,
    )


def test_callout_only_changes_raise_nothing():
    """Dialogue is not content — replies on published briefs must not mail."""
    post = PRE + "\n> [!success] 2026-08-11 — checked\n> all fine\n"
    assert _update_events(post) == []


def test_frontmatter_only_changes_raise_nothing():
    post = PRE.replace("title: T", "title: T\nlast_verified: 2026-08-11")
    assert _update_events(post) == []


def test_a_real_edit_raises_updated_with_the_commit_subject():
    post = PRE.replace("15 August", "14 August")
    [event] = _update_events(post, subject="fix: correct the deadline")
    assert event.kind == "brief_updated"
    assert event.summary == "fix: correct the deadline"


def test_changelog_note_beats_the_commit_subject():
    post = (
        "---\ntitle: T\nuuid: 9a1b2c3d-0000-4000-8000-000000000001\n"
        "changelog:\n"
        '  - { date: 2026-08-11, agent: chris, note: "Deadline corrected to 14 Aug per FCA page" }\n'
        "---\nDeadline is 14 August.\n"
    )
    [event] = _update_events(post, subject="misc edits")
    assert event.summary == "Deadline corrected to 14 Aug per FCA page"


def test_pure_rename_raises_nothing():
    """A contributor reorganising folders must not spam 'new brief' emails."""
    assert _update_events(PRE, status="R", old_path="Topics/Payments and E-money/Briefs/old.md") == []


def test_rename_with_a_real_edit_is_an_update_not_a_creation():
    post = PRE.replace("15 August", "14 August")
    [event] = _update_events(
        post, subject="move and fix", status="R",
        old_path="Topics/Payments and E-money/Briefs/old.md",
    )
    assert event.kind == "brief_updated"


def test_a_newly_set_flag_raises_nothing_on_its_own():
    """`flag:` is frontmatter for humans reading the vault. It used to mint an
    `escalation` event, which is how the link checker's
    `flag: source-unavailable` mailed subscribers an ESCALATION for a dead URL.
    Raising an Escalations/ note is what notifies now."""
    for value in ("enforcement names firm — 2026-08-11", "source-unavailable — 2026-08-17"):
        post = PRE.replace("title: T", f"title: T\nflag: {value}")
        assert _update_events(post) == [], value


def test_a_flag_alongside_a_real_edit_is_an_ordinary_update():
    post = PRE.replace("15 August", "14 August").replace(
        "title: T", "title: T\nflag: enforcement names firm — 2026-08-11"
    )
    [event] = _update_events(post)
    assert event.kind == "brief_updated"


def test_created_brief_summary_is_its_title():
    path = "Topics/Crypto-assets and Digital Assets/Briefs/2026-06-30 - FCA crypto - UK.md"
    events = events_from_diff(
        [DiffEntry("A", path)], "abc", read_file_factory({path: BRIEF}), no_pre
    )
    assert events[0].summary == "FCA finalises cryptoasset regulatory framework"


def test_a_created_brief_with_a_flag_is_still_just_created():
    path = "Topics/Payments and E-money/Briefs/2026-06-17 - OFSI - UK.md"
    events = events_from_diff(
        [DiffEntry("A", path)], "abc", read_file_factory({path: FLAGGED}), no_pre
    )
    assert events[0].kind == "brief_created"


# -- Escalations/ is the only source of escalation events ---------------------

ESCALATION = """---
title: "Vault gap: BoE FMI-supervision fee consultation not filed"
type: escalation
status: pending
raised_date: 2026-08-17
raised_by: research-new-developments
priority: low
topic: Payments and E-money
uuid: 9a1b2c3d-0000-4000-8000-00000000ff01
---
# Body
"""


def test_a_note_raised_in_escalations_is_an_escalation():
    path = "Escalations/2026-08-17 - Vault gap BoE fee consultation.md"
    [event] = events_from_diff(
        [DiffEntry("A", path)], "abc", read_file_factory({path: ESCALATION}), no_pre
    )
    assert event.kind == "escalation"
    assert event.topic == "Payments and E-money"      # routed by frontmatter topic
    assert event.title.startswith("Vault gap")
    assert event.note_uuid == "9a1b2c3d-0000-4000-8000-00000000ff01"


def test_resolving_an_escalation_raises_nothing():
    """Resolution moves the file to Escalations/Resolved/ — bookkeeping, not
    news. The raise already emailed."""
    old = "Escalations/2026-08-17 - Vault gap BoE fee consultation.md"
    new = "Escalations/Resolved/2026-08-17 - Vault gap BoE fee consultation.md"
    assert events_from_diff(
        [DiffEntry("R", new, old_path=old)], "abc",
        read_file_factory({new: ESCALATION}), lambda p: ESCALATION,
    ) == []


def test_editing_an_escalation_raises_nothing():
    """Status changes and ESC-NNN assignment by escalations-sync must not
    re-notify."""
    path = "Escalations/2026-08-17 - Vault gap BoE fee consultation.md"
    post = ESCALATION.replace("status: pending", "status: actioned-by-human\nnumber: 21")
    assert events_from_diff(
        [DiffEntry("M", path)], "abc", read_file_factory({path: post}), lambda p: ESCALATION
    ) == []


def test_the_escalations_index_is_not_an_escalation():
    path = "Escalations/Index.md"
    body = "---\ntitle: Escalations\ntopic: Payments and E-money\n---\n# Index\n"
    assert events_from_diff(
        [DiffEntry("A", path)], "abc", read_file_factory({path: body}), no_pre
    ) == []


def test_an_escalation_without_a_topic_goes_to_the_unrouted_bucket():
    """Delivery is by topic, so a raise with no `topic:` has no audience of its
    own. Dropping it would lose the raise, so it parks in __unrouted__ and the
    outbox hands it to the all-topics subscribers."""
    path = "Escalations/2026-08-17 - Untopiced.md"
    body = ESCALATION.replace("topic: Payments and E-money\n", "")
    [event] = events_from_diff(
        [DiffEntry("A", path)], "abc", read_file_factory({path: body}), no_pre
    )
    assert event.kind == "escalation"
    assert event.topic == UNROUTED_TOPIC


def test_deletions_and_non_briefs_ignored():
    events = events_from_diff(
        [
            DiffEntry("D", "Topics/Payments and E-money/Briefs/old.md"),
            DiffEntry("M", "Home.md"),
            DiffEntry("M", "Topics/Payments and E-money/Index.md"),
        ],
        "abc",
        read_file_factory({}),
        no_pre,
    )
    assert events == []


def test_only_topic_filter():
    path = "Topics/Crypto-assets and Digital Assets/Briefs/2026-06-30 - x - UK.md"
    events = events_from_diff(
        [DiffEntry("A", path)],
        "abc",
        read_file_factory({path: BRIEF}),
        no_pre,
        only_topic="Payments and E-money",
    )
    # primary topic filtered out; secondary matches the filter
    assert [e.topic for e in events] == ["Payments and E-money"]


# -- run logs raise nothing -------------------------------------------------


def _events_for(paths):
    entries = [DiffEntry(status="A", path=p) for p in paths]
    return events_from_diff(entries, "abc123", read_file=lambda p: "", read_pre=lambda p: None)


def test_per_run_log_raises_no_event():
    """One log per run would otherwise add an event to every single run."""
    assert _events_for(["Setup/Tasks/Logs/2026-08-08 - research-new-developments.md"]) == []


def test_nested_log_raises_no_event():
    assert _events_for(["Setup/Logs/research-new-developments/2026-08-08.md"]) == []


def test_the_legacy_single_log_also_raises_nothing():
    assert _events_for(["Setup/Log.md", "Setup/Tasks/Index.md", "Briefs/Index.md"]) == []


def test_a_log_alongside_a_brief_does_not_suppress_the_brief():
    """The skip must be per-path, not per-diff."""
    events = _events_for([
        "Setup/Logs/2026-08-08 - research-new-developments.md",
        "Topics/Payments and E-money/Briefs/2026-08-08 - A development - UK.md",
    ])
    assert [e.kind for e in events] == ["brief_created"]
    assert events[0].topic == "Payments and E-money"


# -- site links --------------------------------------------------------------


def test_note_url_drops_the_suffix_and_encodes_spaces():
    url = note_url(
        "https://regdev.agenticgc.uk",
        "Topics/Payments and E-money/Briefs/2026-08-08 - A development - UK.md",
    )
    assert url == (
        "https://regdev.agenticgc.uk/Topics/Payments%20and%20E-money/Briefs/"
        "2026-08-08%20-%20A%20development%20-%20UK"
    )


def test_note_url_tolerates_a_trailing_slash_on_the_base():
    assert note_url("https://regdev.agenticgc.uk/", "Home.md") == (
        "https://regdev.agenticgc.uk/Home"
    )


def test_note_url_is_none_when_the_site_has_no_base_url():
    """Callers fall back to an unlinked title rather than emitting a bad href."""
    assert note_url("", "Topics/Payments and E-money/Index.md") is None
