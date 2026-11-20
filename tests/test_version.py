"""__version__ must track pyproject.toml.

Regression guard: it was hardcoded in reginald/__init__.py, so release.sh could
bump pyproject.toml and tag a release while `reginald --version` went on
reporting the previous number.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import reginald


def test_version_matches_pyproject():
    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    assert reginald.__version__ == pyproject["project"]["version"]


def test_version_is_not_the_uninstalled_fallback():
    assert reginald.__version__ != "0.0.0+unknown"
