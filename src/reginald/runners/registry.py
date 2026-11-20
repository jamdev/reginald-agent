"""Installed runner adapters, shared by task dispatch and doctor."""

from ..core.config import Config
from .claude_agent import ClaudeAgentRunner
from .codex_agent import CodexAgentRunner
from .pydantic_agent import PydanticAgentRunner


def build_runners(config: Config):
    return {
        "claude-agent": ClaudeAgentRunner(log_turns=config.logging.agent_turns),
        "pydantic_agent": PydanticAgentRunner(),
        "codex_agent": CodexAgentRunner(log_turns=config.logging.agent_turns),
    }
