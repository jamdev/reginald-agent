"""Callout parser — the protocol's syntax layer."""

from __future__ import annotations

from reginald.publisher.callouts import (
    content_hash,
    parse_callouts,
    strip_callouts,
    walk,
)

THREAD = """\
---
title: Some escalation
uuid: abc
---

# Body

Prose before.

> [!todo] @reginald Verify the CP26/21 deadline
> The Index.md row says 14 Aug — check the FCA page directly.
>
> > [!success] 2026-08-11 — Verified, no change needed
> > Fetched the FCA page; deadline confirmed.

Prose after.
"""


def test_parses_a_threaded_conversation():
    [todo] = parse_callouts(THREAD)
    assert todo.type == "todo"
    assert todo.addressee == "reginald"
    assert todo.title == "Verify the CP26/21 deadline"
    assert "check the FCA page" in todo.body
    [reply] = todo.children
    assert reply.type == "success"
    assert reply.title.startswith("2026-08-11")
    assert "deadline confirmed" in reply.body
    assert reply.children == []


def test_types_match_case_insensitively_and_fold_state_markers():
    text = "> [!TODO]- Big letters\n> body\n"
    [callout] = parse_callouts(text)
    assert callout.type == "todo"
    assert callout.title == "Big letters"


def test_unknown_callout_types_are_inert():
    text = "> [!note] Just a note\n> [!info] info\n"
    assert parse_callouts(text) == []


def test_unknown_types_do_not_break_an_enclosing_thread():
    text = (
        "> [!question] Which regulator?\n"
        "> [!note] an aside inside the question\n"
        "> more question body\n"
    )
    [question] = parse_callouts(text)
    assert "[!note]" in question.body  # the aside is body text, not structure
    assert "more question body" in question.body


def test_fenced_examples_never_parse():
    text = (
        "Documentation:\n"
        "```markdown\n"
        "> [!todo] this is an example, not work\n"
        "```\n"
        "> [!warning] but this one is real\n"
    )
    [callout] = parse_callouts(text)
    assert callout.type == "warning"


def test_sibling_callouts_at_the_same_depth_are_separate():
    text = (
        "> [!todo] First\n"
        "> body one\n"
        "\n"
        "> [!todo] Second\n"
        "> body two\n"
    )
    todos = parse_callouts(text)
    assert [c.title for c in todos] == ["First", "Second"]
    assert all(c.children == [] for c in todos)


def test_adjacent_callouts_without_blank_line_are_siblings_not_nested():
    text = "> [!todo] First\n> [!question] Second at the same depth\n"
    callouts = parse_callouts(text)
    assert [c.type for c in callouts] == ["todo", "question"]


def test_walk_yields_parent_linkage():
    pairs = [(c.type, p.type if p else None) for c, p in walk(parse_callouts(THREAD))]
    assert pairs == [("todo", None), ("success", "todo")]


def test_multi_turn_threads_nest_deeper():
    text = (
        "> [!todo] Do the thing\n"
        "> > [!question] 2026-08-11 — Which thing exactly?\n"
        "> > > [!todo] The CP26/21 thing\n"
    )
    [todo] = parse_callouts(text)
    [question] = todo.children
    [answer] = question.children
    assert answer.title == "The CP26/21 thing"


# -- identity ---------------------------------------------------------------


def test_hash_is_stable_across_reformatting_but_not_rewording():
    [a] = parse_callouts("> [!todo] Fix it\n> the   body")
    [b] = parse_callouts("\n" * 8 + "> [!todo] Fix it\n> the body")
    [c] = parse_callouts("> [!todo] Fix it properly\n> the body")
    assert content_hash("uuid-1", a) == content_hash("uuid-1", b)  # whitespace/line-no irrelevant
    assert content_hash("uuid-1", a) != content_hash("uuid-1", c)  # rewording = new work
    assert content_hash("uuid-1", a) != content_hash("uuid-2", a)  # per-note identity


# -- strip (the substantive-change rule's engine) ---------------------------


def test_strip_removes_threads_but_keeps_prose_and_plain_quotes():
    text = (
        "Intro prose.\n"
        "> An ordinary quotation, not a callout.\n"
        "> [!todo] Do something\n"
        "> todo body\n"
        ">\n"
        "> > [!success] Done\n"
        "\n"
        "Closing prose.\n"
    )
    stripped = strip_callouts(text)
    assert "Intro prose." in stripped
    assert "ordinary quotation" in stripped
    assert "Closing prose." in stripped
    assert "todo body" not in stripped
    assert "[!success]" not in stripped


def test_adding_only_a_callout_compares_as_unchanged():
    before = "# Note\n\nBody paragraph.\n"
    after = before + "\n> [!question] @reginald is this right?\n> please check\n"
    assert strip_callouts(before).strip() == strip_callouts(after).strip()


def test_a_real_edit_still_compares_as_changed():
    before = "# Note\n\nDeadline is 15 August.\n"
    after = "# Note\n\nDeadline is 14 August.\n\n> [!success] corrected\n"
    assert strip_callouts(before) != strip_callouts(after)


def test_strip_keeps_fenced_examples_outside_callouts():
    text = "```\n> [!todo] example in fence\n```\nprose\n"
    stripped = strip_callouts(text)
    assert "example in fence" in stripped
    assert "prose" in stripped
