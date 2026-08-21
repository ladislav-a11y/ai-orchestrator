"""`orchestrator.py doctor` - environment/health checks.

Every check is best-effort and never raises; a failed check is reported,
not thrown. Directories are created if missing (safe, local, reversible).
Nothing here ever touches Git history or spends API budget unless --live
is explicitly requested.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from orchestrator.agents.claude_code import ClaudeCodeAgent
from orchestrator.config import Config, load_config


@dataclass
class Check:
    name: str
    ok: bool
    message: str


def _check_python() -> Check:
    version = sys.version.split()[0]
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 10):
        return Check("Python", False, f"Nalezen Python {version}, ale je potřeba 3.10+.")
    return Check("Python", True, f"Python {version} ({sys.executable})")


def _check_git() -> Check:
    git_path = shutil.which("git")
    if not git_path:
        return Check("Git", False, "Git nenalezen v PATH. Nainstaluj Git for Windows.")
    try:
        proc = subprocess.run([git_path, "--version"], capture_output=True, text=True, timeout=10)
    except Exception as e:  # pragma: no cover - defensive
        return Check("Git", False, f"'git --version' selhalo: {e}")
    if proc.returncode != 0:
        return Check("Git", False, f"'git --version' vrátil chybu: {proc.stderr.strip()}")
    return Check("Git", True, f"{proc.stdout.strip()} ({git_path})")


def _check_claude_cli(config: Config) -> tuple[Check, Optional[ClaudeCodeAgent]]:
    try:
        agent = ClaudeCodeAgent(config.claude_code)
    except ValueError as e:
        return Check("Claude Code CLI", False, str(e)), None
    ok, message = agent.is_available()
    return Check("Claude Code CLI", ok, message), (agent if ok else None)


def _check_claude_live(agent: ClaudeCodeAgent, project_path: Path) -> Check:
    from orchestrator.agents.base import AgentRunRequest

    result = agent.run(
        AgentRunRequest(project_path=project_path, prompt="Reply with exactly the single word: OK")
    )
    if not result.success:
        hint = ""
        if result.error and "not logged in" in result.error.lower():
            hint = (
                " -> Spusť CLI ručně jednou interaktivně a přihlaš se (napovězeno "
                "v README.md, sekce 'Přihlášení Claude Code CLI')."
            )
        return Check("Claude Code CLI (živý test)", False, f"{result.error}{hint}")
    return Check(
        "Claude Code CLI (živý test)",
        True,
        f"Odpověděl: {result.output_text.strip()!r} (cena ~${result.cost_usd or 0:.4f})",
    )


def _check_dirs(config: Config) -> Check:
    dirs = [config.inbox_dir, config.outbox_dir, config.logs_dir, config.data_dir]
    created = []
    for d in dirs:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d))
    msg = ", ".join(str(d) for d in dirs)
    if created:
        msg += f" (vytvořeno: {', '.join(created)})"
    return Check("Pracovní adresáře", True, msg)


def _check_config(config: Config) -> Check:
    problems = []
    for name, entry in config.projects.items():
        if not Path(entry.path).exists():
            problems.append(f"projekt '{name}' odkazuje na neexistující cestu: {entry.path}")
    if problems:
        return Check("Konfigurace", False, "; ".join(problems))
    return Check(
        "Konfigurace",
        True,
        f"{config.source_path} ({len(config.projects)} projekt(ů), agent='{config.default_agent}')",
    )


@dataclass
class DoctorReport:
    checks: list[Check]

    @property
    def all_ok(self) -> bool:
        return all(c.ok for c in self.checks)


def run_doctor(live: bool = False, live_project: Optional[str] = None) -> DoctorReport:
    checks: list[Check] = [_check_python(), _check_git()]

    try:
        config = load_config()
    except Exception as e:
        checks.append(Check("Konfigurace", False, str(e)))
        return DoctorReport(checks)

    checks.append(_check_dirs(config))
    checks.append(_check_config(config))

    claude_check, agent = _check_claude_cli(config)
    checks.append(claude_check)

    if live and agent is not None:
        project_path = Path(live_project) if live_project else Path.cwd()
        checks.append(_check_claude_live(agent, project_path))

    return DoctorReport(checks)
