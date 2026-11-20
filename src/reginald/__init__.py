"""reginald — headless orchestrator for the Agentic GC vault."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Read from the installed distribution's metadata, which is built from
    # pyproject.toml. Hardcoding it here meant `reginald --version` reported
    # whatever was last edited by hand, independently of the released version.
    __version__ = version("reginald")
except PackageNotFoundError:  # running from a source tree, not installed
    __version__ = "0.0.0+unknown"
