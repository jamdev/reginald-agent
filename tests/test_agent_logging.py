"""The agent runner's turn logging, driven with real SDK message objects.

Skipped when the agents extra is not installed — the rest of reginald works
without it, and so must the suite.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import (  # noqa: E402 — must follow the importorskip above
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from reginald.core.config import TaskConfig  # noqa: E402
from reginald.runners.claude_agent import ClaudeAgentRunner  # noqa: E402
from reginald.runners.claude_agent.auth import effective_credential  # noqa: E402
from reginald.runners.claude_agent.runner import humanize_agent_error  # noqa: E402

TASK = TaskConfig(title="Demo", runner="claude-agent", model="claude-sonnet-5")


def fake_conversation():
    return [
        AssistantMessage(
            content=[ToolUseBlock(id="1", name="Read", input={"file_path": "Topics/P/Briefs/a.md"})],
            model="claude-sonnet-5",
        ),
        AssistantMessage(content=[TextBlock(text="Thinking about it")], model="claude-sonnet-5"),
        AssistantMessage(
            content=[ToolUseBlock(id="2", name="Write", input={"file_path": "Topics/P/Briefs/b.md"})],
            model="claude-sonnet-5",
        ),
        ResultMessage(
            subtype="success", duration_ms=1200, duration_api_ms=900, is_error=False,
            num_turns=3, session_id="s1", total_cost_usd=0.0421, result="done",
        ),
    ]


@pytest.fixture
def patched_query(monkeypatch):
    """Replace the SDK's query() with our scripted conversation."""

    async def fake_query(prompt, options):  # noqa: ARG001
        for message in fake_conversation():
            yield message

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)


def run_and_capture(caplog, level, log_turns=True):
    with caplog.at_level(level, logger="reginald"):
        return ClaudeAgentRunner(log_turns=log_turns).run(
            "research-new-developments", TASK, "prompt", Path("/tmp/vault"), {}
        )


def test_result_is_still_extracted(patched_query, caplog):
    result = run_and_capture(caplog, logging.INFO)
    assert result.ok is True
    assert result.num_turns == 3
    assert result.cost_usd == pytest.approx(0.0421)
    assert result.output == "done"


def test_each_tool_call_is_logged_at_info(patched_query, caplog):
    run_and_capture(caplog, logging.INFO)
    text = caplog.text
    assert "turn 1" in text and "Read" in text and "Topics/P/Briefs/a.md" in text
    assert "turn 3" in text and "Write" in text and "Topics/P/Briefs/b.md" in text


def test_a_turn_with_no_tool_call_is_still_visible(patched_query, caplog):
    """Consecutive lines of this are what a stall looks like from the journal."""
    run_and_capture(caplog, logging.INFO)
    assert "turn 2" in caplog.text
    assert "no tool calls" in caplog.text


def test_completion_line_reports_turns_and_cost(patched_query, caplog):
    run_and_capture(caplog, logging.INFO)
    assert "finished" in caplog.text
    assert "3 turn(s)" in caplog.text
    assert "$0.0421" in caplog.text


def test_agent_prose_is_debug_only(patched_query, caplog):
    run_and_capture(caplog, logging.INFO)
    assert "Thinking about it" not in caplog.text

    caplog.clear()
    run_and_capture(caplog, logging.DEBUG)
    assert "Thinking about it" in caplog.text


def test_agent_turns_false_silences_the_per_turn_lines(patched_query, caplog):
    run_and_capture(caplog, logging.INFO, log_turns=False)
    assert "turn 1" not in caplog.text
    assert "finished" in caplog.text  # the completion line is not optional


def test_an_sdk_exception_becomes_a_failed_result_not_a_crash(monkeypatch, caplog):
    """The production failure: the SDK's query() raised mid-stream (CLI error
    result). That must become RunnerResult(ok=False), not propagate and crash
    the run pipeline — otherwise the run row stays 'running' and the vault is
    never reset. And the real reason (from stderr) must reach the error, not
    the misleading '...error result: success'."""

    async def raising_query(prompt, options):
        # The CLI writes the real cause to stderr before the SDK raises.
        options.stderr("Credit balance is too low to run this request.")
        yield AssistantMessage(
            content=[ToolUseBlock(id="1", name="WebSearch", input={"query": "x"})],
            model="claude-sonnet-5",
        )
        raise Exception("Claude Code returned an error result: success")

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", raising_query)

    with caplog.at_level(logging.ERROR, logger="reginald"):
        result = ClaudeAgentRunner().run(
            "action-edits", TASK, "prompt", Path("/tmp/vault"), {}
        )

    assert result.ok is False                                 # not raised
    assert "no reason" not in result.error                    # stderr supplies the reason
    assert "Credit balance is too low" in result.error        # real cause from stderr
    assert "error result: success" not in result.error        # the misleading text is gone
    assert "errored" in caplog.text


def test_api_error_status_from_the_result_message_reaches_the_error(monkeypatch):
    """An auth failure yields an error ResultMessage (carrying the HTTP status)
    just before the SDK raises. Capture that status so the log names 401/403/429
    rather than only the generic 'auth failure' text."""

    async def query_then_raise(prompt, options):  # noqa: ARG001
        yield ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=5, is_error=True,
            num_turns=1, session_id="s", total_cost_usd=None, result="",
            api_error_status=401,
        )
        raise Exception("Claude Code returned an error result: success")

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", query_then_raise)

    result = ClaudeAgentRunner().run("action-edits", TASK, "prompt", Path("/tmp/vault"), {})
    assert result.ok is False
    assert "401" in result.error
    assert "error result: success" not in result.error


def test_error_result_message_without_a_raise_still_surfaces_the_status(monkeypatch):
    """The clean-exit variant: an is_error ResultMessage with no trailing raise
    returns ok=False — the status must reach the error there too."""

    async def erroring_query(prompt, options):  # noqa: ARG001
        yield ResultMessage(
            subtype="error_during_execution", duration_ms=10, duration_api_ms=5,
            is_error=True, num_turns=1, session_id="s", total_cost_usd=None,
            result="run failed", api_error_status=429,
        )

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", erroring_query)

    result = ClaudeAgentRunner().run("action-edits", TASK, "prompt", Path("/tmp/vault"), {})
    assert result.ok is False
    assert "429" in result.error
    assert "run failed" in result.error


def test_model_totals_include_subagents_and_a_reported_zero_cost(monkeypatch):
    import claude_agent_sdk

    async def query(prompt, options):
        usage = {"inputTokens": 2, "outputTokens": 3, "cacheReadInputTokens": 4,
                 "cacheCreationInputTokens": 5, "costUSD": 0.0}
        yield ResultMessage(
            "success", 10, 5, False, 1, "s", usage={"input_tokens": 2, "output_tokens": 3},
            model_usage={"main": usage, "subagent": usage}, total_cost_usd=None,
        )

    monkeypatch.setattr(claude_agent_sdk, "query", query)
    result = ClaudeAgentRunner().run("demo", TASK, "", Path("."), {})
    assert result.cost_usd == 0.0
    assert result.token_usage == {"input_tokens": 22, "output_tokens": 6,
                                 "cache_read.input_tokens": 8, "cache_creation.input_tokens": 10}


def test_interrupted_claude_stream_keeps_observed_turns_and_error_detail(monkeypatch):
    import claude_agent_sdk

    async def query(prompt, options):
        for _ in range(2):
            yield AssistantMessage(content=[TextBlock("vault text")], model=TASK.model, message_id="m")
        yield AssistantMessage(content=[TextBlock("Account disabled")], model="<synthetic>",
                               error="authentication_failed")
        raise Exception("Claude Code returned an error result: success")

    monkeypatch.setattr(claude_agent_sdk, "query", query)
    result = ClaudeAgentRunner().run("demo", TASK, "", Path("."), {})
    assert not result.ok
    assert result.num_turns == 1
    assert result.cost_usd is None
    assert result.token_usage == {}
    assert "Account disabled" in result.error
    assert "vault text" not in result.error


# -- humanize_agent_error ----------------------------------------------------


def test_detail_less_success_error_is_rephrased_not_relayed():
    msg = humanize_agent_error(
        Exception("Claude Code returned an error result: success"), []
    )
    assert "success" not in msg
    assert "reported no reason" in msg


def test_stderr_tail_is_appended_with_the_real_cause():
    msg = humanize_agent_error(
        Exception("Claude Code returned an error result: success"),
        ["", "Credit balance is too low to run this request.", ""],
    )
    assert "stderr: Credit balance is too low to run this request." in msg


def test_informative_errors_keep_their_own_message():
    # error_max_turns / real exceptions are not the detail-less case — pass
    # their message through rather than replacing it with the generic reason.
    msg = humanize_agent_error(
        Exception("Claude Code returned an error result: error_max_turns"), []
    )
    assert "error_max_turns" in msg
    assert "usage/credit" not in msg


def test_api_error_status_is_included_when_present():
    msg = humanize_agent_error(
        Exception("Claude Code returned an error result: success"), [], api_error_status=403
    )
    assert "403" in msg


def test_api_error_status_absent_adds_nothing():
    msg = humanize_agent_error(Exception("boom"), [], api_error_status=None)
    assert "API error status" not in msg


# -- which credential the CLI will use (env-driven, API key wins) ------------


def test_api_key_wins_when_both_are_set():
    env = {"ANTHROPIC_API_KEY": "sk-ant-x", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-y"}
    assert effective_credential(env) == "ANTHROPIC_API_KEY"


def test_token_used_when_only_it_is_set():
    assert effective_credential({"CLAUDE_CODE_OAUTH_TOKEN": "oauth-y"}) == "CLAUDE_CODE_OAUTH_TOKEN"


def test_api_key_used_when_only_it_is_set():
    assert effective_credential({"ANTHROPIC_API_KEY": "sk-ant-x"}) == "ANTHROPIC_API_KEY"


def test_none_when_neither_is_set():
    assert effective_credential({}) is None


def test_empty_string_counts_as_absent():
    # An empty ANTHROPIC_API_KEY still wins its slot and auths as empty — treat
    # it as absent so the token, if set, is used instead.
    assert effective_credential({"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": "t"}) == \
        "CLAUDE_CODE_OAUTH_TOKEN"
