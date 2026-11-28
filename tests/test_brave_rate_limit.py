"""Exercise the fetch used by the shipped Brave MCP command, with an offline upstream."""

import os
import subprocess
from pathlib import Path

import yaml

from reginald.core.config import ToolConfig


def test_brave_rate_limits(tmp_path):
    definition = Path(__file__).resolve().parents[1] / "config/tools.d/brave-search.yaml"
    tool = ToolConfig.model_validate(yaml.safe_load(definition.read_text())).for_vault(tmp_path)
    node_options = [arg.removeprefix("--node-options=") for arg in tool.args
                    if arg.startswith("--node-options=")]
    suite = Path(__file__).with_name("brave_rate_limit.mjs")
    command = ["node", "--import", suite.as_uri(), *node_options, "--input-type=module", "-e",
               "await globalThis.exerciseBrave(process.argv[1])"]
    # Separate MCP processes under the same account must honour the first one's cooldown.
    for scenario in ("search", "restart"):
        subprocess.run([*command, scenario], check=True, timeout=15,
                       env={**os.environ, "HOME": str(tmp_path), "BRAVE_API_KEY": "offline"})
