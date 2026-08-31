"""`orchestrator.py doctor` - environment/health checks.

Every check is best-effort and never raises; a failed check is reported,
not thrown. Directories are created if missing (safe, local, reversible).
Nothing here ever touches Git history or spends API budget unless --live
is explicitly requested.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from orchestrator.agents.claude_code import ClaudeCodeAgent
from orchestrator.agents.codex import CodexAgent
from orchestrator.claude_settings import ensure_project_claude_settings
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


def _check_codex_cli(config: Config) -> tuple[Check, Optional[CodexAgent]]:
    try:
        agent = CodexAgent(config.codex)
    except ValueError as e:
        return Check("Codex CLI", False, str(e)), None
    ok, message = agent.is_available()
    return Check("Codex CLI", ok, message), (agent if ok else None)


CODEX_LIVE_RECEIPT_FILENAME = "codex_live_contract_verified.json"


def _codex_live_receipt_path(config: Config) -> Path:
    return config.data_dir / CODEX_LIVE_RECEIPT_FILENAME


def _write_codex_live_receipt(config: Config, cli_note: str, result=None) -> None:
    """Persist durable, auditable evidence that a live Codex verification
    actually happened - a console/Slack line saying "kontrakt ověřen" only
    exists for as long as someone remembers reading it. Recorded here as a
    dated JSON file, fingerprinted against the exact contract code that was
    verified (see `codex.contract_fingerprint`), so a later audit can check
    for its presence *and* confirm it is still fresh, not just take the
    claim on faith."""
    from datetime import datetime, timezone

    from orchestrator.agents.codex import contract_fingerprint

    receipt = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "codex_cli_note": cli_note,
        "contract_fingerprint": contract_fingerprint(),
        "usage": {
            "source": "reported",
            "input_tokens": result.input_tokens if result is not None else None,
            "output_tokens": result.output_tokens if result is not None else None,
            "thinking_tokens": result.thinking_tokens if result is not None else None,
            "total_tokens": result.total_tokens if result is not None else None,
            "cost_usd": result.cost_usd if result is not None else None,
        },
    }
    path = _codex_live_receipt_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")


def _check_codex_live_receipt(config: Config) -> Check:
    """Surface the trust state of the last `doctor --live` Codex
    verification without spending any quota itself - runs unconditionally
    (not just under --live) so a human/CI/auditor can see at a glance
    whether a *fresh* live verification exists, see AGENTS.md rule 14 and
    `_write_codex_live_receipt`. A stale receipt (contract code changed
    since it was written) is reported as not ok - it must never be read as
    still-valid evidence of the current contract."""
    from orchestrator.agents.codex import contract_fingerprint

    path = _codex_live_receipt_path(config)
    if not path.exists():
        return Check(
            "Codex CLI (poslední živé ověření)", True,
            "Zatím žádné - spusť `orchestrator.py doctor --live` s nainstalovaným a "
            "přihlášeným Codex CLI (viz README.md 'Ověření reálného Codex CLI kontraktu').",
        )
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return Check("Codex CLI (poslední živé ověření)", False, f"Receipt {path} je nečitelný: {e}")
    if not isinstance(receipt, dict):
        return Check("Codex CLI (poslední živé ověření)", False, f"Receipt {path} má neplatný tvar.")

    verified_at = receipt.get("verified_at", "neznámo kdy")
    cli_note = receipt.get("codex_cli_note")
    if not isinstance(cli_note, str) or "codex" not in cli_note.lower():
        return Check(
            "Codex CLI (poslední živé ověření)", False,
            f"Receipt {path} neidentifikuje reálný Codex CLI (`codex --version`): "
            f"{cli_note!r}. Nelze jej přijmout jako živé ověření; spusť `doctor --live` znovu.",
        )
    if receipt.get("contract_fingerprint") != contract_fingerprint():
        return Check(
            "Codex CLI (poslední živé ověření)", False,
            f"Ověřeno {verified_at}, ale kontrakt (_build_command) se od té doby v kódu změnil - "
            "tohle ověření už neplatí pro aktuální kód, spusť `doctor --live` znovu.",
        )
    return Check(
        "Codex CLI (poslední živé ověření)", True,
        f"Naposledy ověřeno naživo {verified_at}, kontrakt v kódu se od té doby nezměnil.",
    )


def _check_codex_live(
    config: Config,
    project_path: Path,
    *,
    write_receipt: bool = False,
) -> Check:
    """Read-only live contract smoke test for the installed Codex CLI - the
    manual, one-time verification step documented in AGENTS.md rule 14 (see
    also tests/test_codex_agent.py::test_live_smoke_reads_project_state_
    without_changes, which exercises the same contract under pytest but is
    gated behind AI_ORCHESTRATOR_RUN_LIVE_CODEX_TEST=1 so it never runs
    automatically). `doctor --live` is this project's one existing,
    documented way to make a human/CI actually invoke a live provider CLI
    (see `_check_claude_live` above) - this gives Codex the same single,
    discoverable entry point instead of a pytest incantation nobody runs.

    Forces sandbox_mode="read-only" regardless of the configured default so
    this check can never mutate `project_path`, and verifies that directly:
    the directory listing before and after must be identical. On success,
    also writes the durable receipt read by `_check_codex_live_receipt`."""
    from orchestrator.agents.base import AgentRunRequest

    read_only_config = dataclasses.replace(config.codex, sandbox_mode="read-only")
    agent = CodexAgent(read_only_config)
    before = sorted(p.name for p in project_path.iterdir()) if project_path.exists() else []

    result = agent.run(
        AgentRunRequest(
            project_path=project_path,
            prompt=(
                "Do not create, modify, or delete any files. Reply only with the "
                "required final JSON response."
            ),
            output_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
        )
    )

    after = sorted(p.name for p in project_path.iterdir()) if project_path.exists() else []
    if before != after:
        return Check(
            "Codex CLI (živý test)", False,
            "Živý test změnil obsah adresáře i přes --sandbox read-only - kontrakt porušen, "
            "NEPOVAŽOVAT za ověřený.",
        )
    if not result.success:
        return Check("Codex CLI (živý test)", False, result.error or "neúspěch bez chybové zprávy")
    try:
        parsed = json.loads(result.output_text)
    except (json.JSONDecodeError, TypeError):
        return Check(
            "Codex CLI (živý test)", False,
            f"Odpověď neodpovídá požadovanému --output-schema kontraktu (není platný JSON): "
            f"{result.output_text!r}",
        )
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        return Check(
            "Codex CLI (živý test)", False,
            f"Odpověď neodpovídá požadovanému --output-schema kontraktu: {parsed!r}",
        )
    # Only the public `doctor --live` entry point may create durable evidence.
    # Unit tests call this helper with a mocked CodexAgent and must never be
    # able to leave a receipt that an auditor could mistake for a real CLI run.
    if write_receipt:
        _write_codex_live_receipt(config, agent.is_available()[1], result)
    usage_note = (
        f"input={result.input_tokens}, output={result.output_tokens}, "
        f"total={result.total_tokens}, zdroj=reported"
    )
    return Check(
        "Codex CLI (živý test)", True,
        f"--output-schema kontrakt ověřen, žádné změny souborů; {usage_note} "
        f"(reportovaná cena: {result.cost_usd!r} USD)",
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


def _check_claude_settings(config: Config) -> Check:
    """Retrofit the safe allow/deny permissions onto already-existing
    registered projects (new projects get this from `service.submit()` on
    their first task; this check catches ones that predate that, e.g. this
    repo itself)."""
    prepared = []
    for name, entry in config.projects.items():
        project_dir = Path(entry.path)
        if not project_dir.exists():
            continue  # created (and given settings) on first submitted task
        settings_path = project_dir / ".claude" / "settings.local.json"
        already_had_it = settings_path.exists()
        ensure_project_claude_settings(project_dir)
        if not already_had_it:
            prepared.append(name)
    msg = f"{len(config.projects)} registrovaný(ch) projekt(ů) zkontrolováno"
    if prepared:
        msg += f"; nastaveny bezpečné allow/deny permissions pro: {', '.join(prepared)}"
    return Check("Claude Code permissions", True, msg)


def _check_config(config: Config) -> Check:
    notes = []
    for name, entry in config.projects.items():
        if not Path(entry.path).exists():
            notes.append(
                f"projekt '{name}' ({entry.path}) zatím na disku neexistuje - "
                "vytvoří se automaticky při prvním spuštěném úkolu"
            )
    msg = f"{config.source_path} ({len(config.projects)} projekt(ů), agent='{config.default_agent}')"
    msg += f", pracovní prostor: {config.workspace_root_dir}"
    if notes:
        msg += "; " + "; ".join(notes)
    return Check("Konfigurace", True, msg)


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
    checks.append(_check_claude_settings(config))

    claude_check, agent = _check_claude_cli(config)
    checks.append(claude_check)

    if live and agent is not None:
        project_path = Path(live_project) if live_project else Path.cwd()
        checks.append(_check_claude_live(agent, project_path))

    codex_check, codex_agent = _check_codex_cli(config)
    checks.append(codex_check)
    checks.append(_check_codex_live_receipt(config))

    if live and codex_agent is not None:
        project_path = Path(live_project) if live_project else Path.cwd()
        checks.append(_check_codex_live(config, project_path, write_receipt=True))

    return DoctorReport(checks)
