"""DoctorService — self-check: config, DB, keys, git, ob, runners."""

from __future__ import annotations

import os
import shutil
import subprocess

from sqlalchemy import text

from .. import __version__
from ..core.config import Config
from ..core.git import VaultGit
from ..core.observability import check_logfire
from ..core.release import release_detail
from ..db.session import make_engine
from ..domain.models import DoctorCheck, DoctorReport
from ..runners.registry import build_runners


class DoctorService:
    def __init__(self, config: Config, escalations=None, identify_changes=None):
        self._config = config
        self._escalations = escalations  # EscalationService | None
        self._identify_changes = identify_changes

    def check(self) -> DoctorReport:
        checks: list[DoctorCheck] = []
        config = self._config

        # Informational, never a failure: an operator reading a red doctor needs
        # to know which release produced it, and a checkout ahead of its tag is
        # normal on a dev box.
        checks.append(
            DoctorCheck(name="release", ok=True, detail=release_detail(__version__))
        )

        checks.append(DoctorCheck(name="config", ok=True, detail=f"env={config.env}"))

        vault_ok = config.vault_path.is_dir()
        checks.append(
            DoctorCheck(name="vault path", ok=vault_ok, detail=str(config.vault_path))
        )

        git = VaultGit(
            config.vault_path,
            author_name=config.git.author_name,
            author_email=config.git.author_email,
        )
        git_ok = vault_ok and git.is_repo()
        detail = ""
        if git_ok:
            detail = f"HEAD {git.head()[:10]}" + (" (dirty)" if git.is_dirty() else " (clean)")
        checks.append(DoctorCheck(name="vault git", ok=git_ok, detail=detail))

        try:
            with make_engine(config.db_path).connect() as conn:
                conn.execute(text("SELECT 1"))
            checks.append(DoctorCheck(name="database", ok=True, detail=str(config.db_path)))
        except Exception as exc:  # noqa: BLE001
            checks.append(DoctorCheck(name="database", ok=False, detail=str(exc)))

        for name, runner in sorted(build_runners(config).items()):
            tasks = {key: task for key, task in config.tasks.items() if task.runner == name}
            if tasks:
                checks.extend(runner.doctor_checks(tasks))
            else:
                checks.append(DoctorCheck(
                    name=f"runner {name}", ok=True, warning=True,
                    detail=f"Unused. No tasks select this runner in {config.env}.",
                ))
        if config.email.provider == "postmark":
            has_token = "POSTMARK_SERVER_TOKEN" in os.environ
            checks.append(
                DoctorCheck(
                    name="email service",
                    ok=has_token,
                    detail="Postmark (token present)" if has_token else "Postmark (token missing)",
                )
            )

        if config.logfire.enabled:
            checks.append(check_logfire(config.logfire.environment))

        if config.site.enabled:
            ob = shutil.which(config.site.ob_binary)
            if ob:
                try:
                    ver = subprocess.run(
                        [config.site.ob_binary, "--version"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,  # a non-zero probe still counts as "present"
                    ).stdout.strip()
                except Exception:  # noqa: BLE001
                    ver = "found"
                checks.append(DoctorCheck(name="obsidian-headless", ok=True, detail=ver or ob))
            else:
                checks.append(
                    DoctorCheck(
                        name="obsidian-headless",
                        ok=False,
                        detail=f"'{config.site.ob_binary}' not on PATH (npm install -g obsidian-headless)",
                    )
                )
        node = shutil.which("node")
        checks.append(
            DoctorCheck(name="node", ok=node is not None, detail=node or "not on PATH")
        )

        # Informational, never failures: the collaboration surface at a glance.
        if self._escalations is not None:
            try:
                pending = len(self._escalations.list(status="pending"))
                missing = [r for r in self._escalations.list() if r.missing_since]
                detail = f"{pending} pending"
                if missing:
                    detail += f", {len(missing)} row(s) whose file vanished"
                checks.append(DoctorCheck(name="escalations", ok=True, detail=detail))
            except Exception as exc:  # noqa: BLE001 — doctor must not crash on a broken table
                checks.append(DoctorCheck(name="escalations", ok=False, detail=str(exc)))
        if self._identify_changes is not None:
            try:
                items = self._identify_changes.items(state="pending")
                handle = self._config.identity.handle
                for_us = [i for i in items if i.addressee in (None, handle)]
                detail = f"{len(for_us)} item(s) pending for @{handle}"
                others = len(items) - len(for_us)
                if others:
                    detail += f", {others} awaiting other contributors"
                checks.append(DoctorCheck(name="callout queue", ok=True, detail=detail))
            except Exception as exc:  # noqa: BLE001
                checks.append(DoctorCheck(name="callout queue", ok=False, detail=str(exc)))
        if git_ok:
            salvages = self._recent_salvages(git)
            if salvages:
                checks.append(
                    DoctorCheck(
                        name="salvage commits", ok=True,
                        detail=f"{salvages} in the last 7 days — someone edited on the host? "
                               "(contribute via your own clone instead)",
                    )
                )

        return DoctorReport(ok=all(c.ok for c in checks), checks=checks)

    @staticmethod
    def _recent_salvages(git: VaultGit) -> int:
        try:
            out = git._git(  # noqa: SLF001 — doctor is allowed plumbing access
                "log", "--since=7.days", "--grep=^reginald: salvage",
                "--format=%h", check=False,
            ).stdout.strip()
            return len(out.splitlines()) if out else 0
        except Exception:  # noqa: BLE001
            return 0
