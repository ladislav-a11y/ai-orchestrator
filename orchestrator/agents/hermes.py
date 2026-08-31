"""Nous-only Hermes Agent provider for the production orchestrator.

Hermes is invoked as a local one-shot CLI, but its terminal tool has its own
working-directory resolver.  The adapter therefore sets both subprocess
``cwd`` and ``TERMINAL_CWD`` and never trusts the CLI's default home
directory.  The provider/model are intentionally constants: Hermes must use
only the authenticated Nous free endpoint, never OpenCode, Gemini, or a
fallback model.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from orchestrator.agents.base import Agent, AgentRunRequest, AgentRunResult
from orchestrator.config import HermesAgentConfig


HERMES_PROVIDER = "nous"
HERMES_FREE_MODEL = "upstage/solar-pro4:free"
_LIMIT_MARKERS = ("429", "quota", "rate limit", "resource_exhausted", "exhausted")


def find_hermes_cli(explicit_path: str = "") -> tuple[Optional[str], str]:
    """Find the installed Hermes CLI without starting it."""
    if explicit_path:
        path = Path(explicit_path).expanduser()
        if path.is_file():
            return str(path), f"použita ručně nastavená cesta v config.yaml: {path}"
        return None, f"config.yaml udává hermes.cli_path='{explicit_path}', ale soubor neexistuje"

    for command in ("hermes", "hermes.exe"):
        found = shutil.which(command)
        if found:
            return found, f"nalezeno v PATH: {found}"

    local_appdata = os.environ.get("LOCALAPPDATA")
    candidates = []
    if local_appdata:
        candidates.append(Path(local_appdata) / "hermes" / "bin" / "hermes.exe")
    candidates.append(Path.home() / "AppData" / "Local" / "hermes" / "bin" / "hermes.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate), f"nalezeno v lokální instalaci Hermes: {candidate}"
    return None, "Hermes CLI nenalezeno v PATH ani v %LOCALAPPDATA%\\hermes\\bin."


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _validate_output_schema(output: str, schema: Optional[dict[str, Any]]) -> Optional[str]:
    """Enforce the small part of JSON Schema needed by autonomous.py.

    Hermes has no native ``--output-schema`` flag.  It receives the schema in
    its prompt, and this adapter fail-closes when the required object/keys
    are still absent, so prose cannot be reported as a successful iteration.
    """
    if schema is None:
        return None
    value = _extract_json_object(output)
    if value is None:
        return "Hermes nevrátil JSON objekt požadovaný orchestratorovým kontraktem"
    if schema.get("type") == "object" and not isinstance(value, dict):
        return "Hermesův výstup není JSON object"
    missing = [key for key in schema.get("required", []) if key not in value]
    if missing:
        return f"Hermesův JSON postrádá povinné klíče: {', '.join(missing)}"
    return None


def _usage_int(usage: dict[str, Any], key: str) -> Optional[int]:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _read_usage(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _retry_after(text: str) -> Optional[float]:
    match = re.search(r"(?:retry|wait|za)\D+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m)", text, re.I)
    if not match:
        return None
    value = float(match.group(1))
    return value * 60 if match.group(2).lower().startswith("m") else value


def _git_status(path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "(git status unavailable)"
    return proc.stdout.strip() or "(clean)"


class HermesAgent(Agent):
    """Production Hermes adapter with a hard Nous-free provider policy."""

    name = "hermes"

    def __init__(self, config: HermesAgentConfig):
        if config.model != HERMES_FREE_MODEL:
            raise ValueError(
                f"Hermes smí používat pouze {HERMES_FREE_MODEL}, ne {config.model!r}."
            )
        self.config = config
        self._cli_path, self._detect_note = find_hermes_cli(config.cli_path)

    def is_available(self) -> tuple[bool, str]:
        if not self._cli_path:
            return False, self._detect_note
        try:
            proc = subprocess.run(
                [self._cli_path, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"Hermes --version selhalo: {exc}"
        if proc.returncode != 0:
            return False, f"Hermes --version selhalo (kód {proc.returncode}): {proc.stderr.strip()}"
        return True, f"{proc.stdout.strip()} ({self._detect_note}); provider=nous model={HERMES_FREE_MODEL}"

    def _prompt(self, request: AgentRunRequest, project_path: Path) -> str:
        prompt = request.prompt
        if request.context:
            prompt = f"{prompt}\n\n---\n{request.context}"
        contract = (
            "\n\n--- ORCHESTRATOR HERMES CONTRACT ---\n"
            f"Pracovní adresář je přesně {project_path}. Prováděj skutečnou práci pomocí terminal nástrojů; "
            "pouhý popis postupu není výsledek. Před finální odpovědí ověř skutečný stav a nikdy nehlaš success, "
            "pokud příkaz nebo postcondition selhal. Výstup musí být pouze JSON požadovaný orchestrátorem."
        )
        if request.output_schema is not None:
            contract += "\nJSON schema:\n" + json.dumps(request.output_schema, ensure_ascii=False, separators=(",", ":"))
        return prompt + contract

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        project_path = Path(request.project_path).resolve()
        if not project_path.is_dir():
            return AgentRunResult(success=False, output_text="", error=f"Hermes pracovní adresář neexistuje: {project_path}")
        available, note = self.is_available()
        if not available:
            return AgentRunResult(success=False, output_text="", error=note)

        before_status = _git_status(project_path)
        response_path = project_path / "response.txt"
        response_existed = response_path.exists()
        with tempfile.TemporaryDirectory(prefix="ai-orchestrator-hermes-") as usage_dir:
            usage_path = Path(usage_dir) / "usage.json"
            env = os.environ.copy()
            # Hermes' terminal backend reads this variable; subprocess cwd alone
            # is insufficient on Windows and previously drifted to C:\\Users\\Admin.
            env["TERMINAL_CWD"] = str(project_path)
            cmd = [
                self._cli_path,
                "--provider", HERMES_PROVIDER,
                "--model", HERMES_FREE_MODEL,
                "--no-restore-cwd",
                "--usage-file", str(usage_path),
                "-z", self._prompt(request, project_path),
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(project_path),
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.config.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                return AgentRunResult(
                    success=False,
                    output_text="",
                    error=f"Hermes neodpověděl do {self.config.timeout_seconds}s (timeout): {exc}",
                    timed_out=True,
                )
            except OSError as exc:
                return AgentRunResult(success=False, output_text="", error=f"Nelze spustit Hermes CLI: {exc}")
            usage = _read_usage(usage_path)

        # The CLI writes response.txt as a convenience copy of stdout.  Do not
        # leave that generated control artifact in the user's project; preserve
        # it if it existed before the run or differs from stdout intentionally.
        if not response_existed and response_path.is_file():
            try:
                if response_path.read_text(encoding="utf-8", errors="replace").strip() == proc.stdout.strip():
                    response_path.unlink()
            except OSError:
                pass

        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        combined_error = stderr or stdout or f"exit code {proc.returncode}"
        provider = usage.get("provider")
        model = usage.get("model")
        if provider != HERMES_PROVIDER or model != HERMES_FREE_MODEL:
            return AgentRunResult(
                success=False,
                output_text=stdout,
                error=f"Hermes porušil Nous-only kontrakt: usage provider={provider!r}, model={model!r}",
                raw_response={"cwd": str(project_path), "usage": usage, "postcondition_before": before_status, "postcondition_after": _git_status(project_path)},
            )

        schema_error = _validate_output_schema(stdout, request.output_schema)
        limited = proc.returncode != 0 and any(marker in combined_error.lower() for marker in _LIMIT_MARKERS)
        error = schema_error or (None if proc.returncode == 0 and stdout else combined_error)
        after_status = _git_status(project_path)
        input_tokens = _usage_int(usage, "input_tokens")
        output_tokens = _usage_int(usage, "output_tokens")
        total_tokens = _usage_int(usage, "total_tokens")
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        usage_event = {
            "provider": self.name,
            "source": "reported",
            "backend_provider": HERMES_PROVIDER,
            "model": HERMES_FREE_MODEL,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cost_usd": usage.get("estimated_cost_usd"),
        }
        return AgentRunResult(
            success=proc.returncode == 0 and bool(stdout) and schema_error is None,
            output_text=stdout,
            raw_response={
                "cwd": str(project_path),
                "provider": HERMES_PROVIDER,
                "model": HERMES_FREE_MODEL,
                "usage": usage,
                "postcondition_before": before_status,
                "postcondition_after": after_status,
                "postcondition_changed": before_status != after_status,
                "stderr": stderr,
            },
            session_id=usage.get("session_id"),
            cost_usd=usage.get("estimated_cost_usd"),
            error=error,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            usage_events=[usage_event],
            limited=limited,
            retry_after_seconds=_retry_after(combined_error) if limited else None,
        )
