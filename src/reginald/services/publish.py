"""PublishService — publishing the vault to a front-end.

Two surfaces:

- `publish()` renders the vault with the *default* engine (`site.engine`) the
  standard way. This is what the run pipeline calls and what `reginald vault publish`
  runs. Today the only engine is `ob` (obsidian-headless).
- `ob(args)` is a passthrough to the `ob` CLI — `reginald vault ob <args>` —
  for the engine's other operations (login, publish-setup, diagnostics). New
  engines add their own passthrough alongside; the dispatch below is the stub.

Publishing is read-only w.r.t. the tracked working tree and is invoked from
inside a run (via IdentifyChangesService, under the run lock), so nothing here takes the
lock — doing so would deadlock the non-reentrant run lock.
"""

from __future__ import annotations

import shutil
import subprocess

from pydantic import BaseModel

from ..core.config import Config
from ..core.observability import logger, span
from ..domain.models import PublishResult


class PublishCommandResult(BaseModel):
    """Verbatim result of an engine-CLI passthrough (`vault ob <args>`)."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class PublishService:
    def __init__(self, config: Config):
        self._config = config

    def available(self) -> bool:
        return shutil.which(self._config.site.ob_binary) is not None

    def publish(self, dry_run: bool = False) -> PublishResult:
        """Publish the vault with the configured default engine."""
        if not self._config.site.enabled:
            return PublishResult(ok=True, dry_run=True, detail="site publishing disabled in config")
        # Dispatch on the default engine. One today; the branch is the seam.
        engine = self._config.site.engine
        with span("site.publish", dry_run=dry_run, engine=engine) as action:
            if engine == "ob":
                result = self._publish_ob(dry_run=dry_run)
            else:
                result = PublishResult(ok=False, detail=f"unknown publishing engine '{engine}'")
            if action is not None and not result.ok:
                action.set_level("error")
            return result

    def _publish_ob(self, dry_run: bool) -> PublishResult:
        site = self._config.site
        if not self.available():
            return PublishResult(ok=False, detail=f"'{site.ob_binary}' not found on PATH")

        cmd = [site.ob_binary, "publish", "--path", str(self._config.vault_path), "--yes"]
        if dry_run:
            cmd.append("--dry-run")
        cmd.extend(site.extra_args)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
        except subprocess.TimeoutExpired:
            return PublishResult(ok=False, dry_run=dry_run, detail="ob publish timed out")
        detail = (result.stdout + result.stderr).strip()[-2000:]
        if result.returncode != 0:
            logger.error("ob publish failed: %s", detail)
            return PublishResult(ok=False, dry_run=dry_run, detail=detail)
        return PublishResult(ok=True, dry_run=dry_run, detail=detail)

    def ob(self, args: list[str]) -> PublishCommandResult:
        """Passthrough to the `ob` CLI, run with the vault as its cwd.

        A raw passthrough: `reginald vault ob login`, `... ob publish-setup
        --site X`, `... ob --version` behave exactly as on the command line.
        Not gated on `site.enabled` — it is an explicit operator command — and
        takes no lock, since ob does not mutate the tracked working tree.
        """
        binary = self._config.site.ob_binary
        try:
            result = subprocess.run(
                [binary, *args],
                cwd=str(self._config.vault_path),
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except FileNotFoundError:
            return PublishCommandResult(
                returncode=127, stderr=f"'{binary}' not found on PATH\n"
            )
        except subprocess.TimeoutExpired:
            return PublishCommandResult(returncode=124, stderr="ob timed out\n")
        logger.info("vault ob %s -> exit %d", " ".join(args), result.returncode)
        return PublishCommandResult(
            returncode=result.returncode, stdout=result.stdout, stderr=result.stderr
        )
