"""What release this install actually is.

Two facts, deliberately kept apart. `__version__` comes from the installed
distribution's metadata; the tag comes from the checkout the code is running
out of. On the host these should agree — `update.sh` checks out a tag and then
syncs — and when they do not, that gap is the thing worth seeing: a checkout
moved without a re-sync reports the old number while running the new code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# .../src/reginald/core/release.py -> the repo root, when running from a
# checkout (uv installs the project itself editable, so this holds on the host
# too). Installed as a plain wheel it points into site-packages, which is not a
# repo — describe() then returns None and callers fall back to the version.
CHECKOUT_ROOT = Path(__file__).resolve().parents[3]


def checkout_tag(root: Path | None = None) -> str | None:
    """`git describe` of the code checkout, or None if it is not a repo.

    Returns the tag exactly when HEAD is on one ("v0.6.0"), and a tag-relative
    description otherwise ("v0.6.0-2-gf266941", plus "-dirty" for uncommitted
    changes), so an unreleased or drifted install is visible as such.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root or CHECKOUT_ROOT), "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def release_detail(version: str, root: Path | None = None) -> str:
    """One line for `doctor`: the installed version and the checkout it came from.

    A dirty tree is reported but is not drift — it is the normal state of a box
    someone is working on. Drift is HEAD sitting somewhere other than the tag
    matching the installed version, which is what a half-finished deploy or a
    hand-edited host looks like.
    """
    described = checkout_tag(root)
    if described is None:
        return f"{version} (no checkout — installed from a package)"

    dirty = described.endswith("-dirty")
    tag = described.removesuffix("-dirty")
    suffix = ", working tree dirty" if dirty else ""

    if tag == f"v{version}":
        return f"{version} (tag {tag}{suffix})"
    if "-g" in tag:  # describe's "<tag>-<n commits>-g<sha>" form
        return f"{version} (checkout {tag} — ahead of its tag{suffix})"
    return f"{version} (checkout {tag} — expected tag v{version}{suffix})"
