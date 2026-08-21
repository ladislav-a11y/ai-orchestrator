"""Agent implementation that drives the locally installed Claude Code CLI.

Invocation contract (do not weaken this without updating AGENTS.md too):
  - always non-interactive: `claude -p "<prompt>" --output-format json ...`
  - NEVER passes --dangerously-skip-permissions / --allow-dangerously-skip-permissions
  - NEVER sets --permission-mode bypassPermissions (config.py also rejects this,
    this is a second, independent guard in case config validation is ever bypassed)
  - runs with cwd = the target project's directory, so Claude's own
    project-level .claude/settings.json permission rules apply as normal
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from orchestrator.agents.base import Agent, AgentRunRequest, AgentRunResult
from orchestrator.config import ClaudeCodeAgentConfig

FORBIDDEN_FLAGS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
)
FORBIDDEN_PERMISSION_MODE = "bypassPermissions"


def _version_key(folder_name: str) -> tuple:
    parts = re.split(r"[.\-]", folder_name)
    key = []
    for p in parts:
        key.append(int(p)) if p.isdigit() else key.append(p)
    return tuple(key)


def find_claude_cli(explicit_path: str = "") -> tuple[Optional[str], str]:
    """Return (path_or_None, human_readable_note)."""
    if explicit_path:
        p = Path(explicit_path)
        if p.exists():
            return str(p), f"použita ručně nastavená cesta v config.yaml: {p}"
        return None, f"config.yaml udává claude_code.cli_path='{explicit_path}', ale ten soubor neexistuje"

    on_path = shutil.which("claude")
    if on_path:
        return on_path, f"nalezeno v PATH: {on_path}"

    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    search_roots = []
    if appdata:
        search_roots.append(Path(appdata) / "Claude" / "claude-code")
    if localappdata:
        search_roots.append(
            Path(localappdata) / "Packages"
        )  # will glob for Claude_*/LocalCache/Roaming/Claude/claude-code

    for root in search_roots:
        if not root.exists():
            continue
        if root.name == "claude-code":
            for version_dir in root.iterdir():
                exe = version_dir / "claude.exe"
                if exe.exists():
                    candidates.append(exe)
        else:  # Packages root - glob for the sandboxed copy
            for exe in root.glob("Claude_*/LocalCache/Roaming/Claude/claude-code/*/claude.exe"):
                candidates.append(exe)

    if not candidates:
        return None, (
            "claude CLI nenalezen ani v PATH, ani ve standardních instalačních "
            "adresářích Claude Desktop. Nainstaluj Claude Code, nebo nastav "
            "claude_code.cli_path v config.yaml ručně."
        )

    best = max(candidates, key=lambda exe: _version_key(exe.parent.name))
    note = (
        f"nalezeno v instalaci Claude Desktop (verze {best.parent.name}): {best}. "
        "Pozor: tato cesta obsahuje číslo verze a po aktualizaci Claude Desktop "
        "se může změnit - pokud doctor přestane CLI nacházet, spusť ho znovu "
        "nebo nastav claude_code.cli_path ručně."
    )
    return str(best), note


class ClaudeCodeAgent(Agent):
    name = "claude-code"

    def __init__(self, config: ClaudeCodeAgentConfig):
        if config.permission_mode == FORBIDDEN_PERMISSION_MODE:
            raise ValueError(
                "ClaudeCodeAgent odmítá permission_mode='bypassPermissions' - "
                "to je ekvivalent --dangerously-skip-permissions a je trvale zakázané."
            )
        self.config = config
        self._cli_path, self._detect_note = find_claude_cli(config.cli_path)

    def is_available(self) -> tuple[bool, str]:
        if not self._cli_path:
            return False, self._detect_note
        try:
            proc = subprocess.run(
                [self._cli_path, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception as e:  # pragma: no cover - defensive
            return False, f"nepodařilo se spustit '{self._cli_path} --version': {e}"
        if proc.returncode != 0:
            return False, f"'{self._cli_path} --version' selhalo (kod {proc.returncode}): {proc.stderr.strip()}"
        return True, f"{proc.stdout.strip()} ({self._detect_note})"

    def _build_command(self, request: AgentRunRequest) -> list[str]:
        assert self._cli_path
        cmd = [self._cli_path, "-p", request.prompt, "--output-format", "json"]

        if request.session_id:
            cmd += ["--resume", request.session_id]

        cmd += ["--permission-mode", self.config.permission_mode]

        if self.config.model:
            cmd += ["--model", self.config.model]
        if self.config.allowed_tools:
            cmd += ["--allowedTools", *self.config.allowed_tools]
        if self.config.disallowed_tools:
            cmd += ["--disallowedTools", *self.config.disallowed_tools]
        if self.config.max_budget_usd:
            cmd += ["--max-budget-usd", str(self.config.max_budget_usd)]

        for forbidden in FORBIDDEN_FLAGS:
            assert forbidden not in cmd, "safety invariant violated: forbidden flag in command"

        return cmd

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        available, note = self.is_available()
        if not available:
            return AgentRunResult(success=False, output_text="", error=note)

        prompt = request.prompt
        if request.context:
            prompt = f"{request.prompt}\n\n---\n{request.context}"
        effective_request = AgentRunRequest(
            project_path=request.project_path,
            prompt=prompt,
            session_id=request.session_id,
        )
        cmd = self._build_command(effective_request)

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(request.project_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return AgentRunResult(
                success=False,
                output_text="",
                error=f"Claude Code neodpověděl do {self.config.timeout_seconds}s (timeout).",
            )
        except FileNotFoundError as e:
            return AgentRunResult(success=False, output_text="", error=f"Nelze spustit CLI: {e}")

        raw: Optional[dict] = None
        try:
            raw = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            raw = None

        if raw is not None:
            is_error = bool(raw.get("is_error", proc.returncode != 0))
            result_text = raw.get("result", "")
            denials = raw.get("permission_denials") or []
            if denials:
                result_text += f"\n\n[orchestrator] Claude odmítl {len(denials)} akci(í) kvůli oprávněním."
            return AgentRunResult(
                success=(not is_error),
                output_text=result_text,
                raw_response=raw,
                session_id=raw.get("session_id"),
                cost_usd=raw.get("total_cost_usd"),
                error=None if not is_error else (result_text or "Claude Code vrátil chybu."),
            )

        # Could not parse JSON - fall back to raw stdout/stderr.
        success = proc.returncode == 0
        return AgentRunResult(
            success=success,
            output_text=proc.stdout.strip(),
            error=None if success else (proc.stderr.strip() or f"exit code {proc.returncode}"),
        )
