"""Headless Gemini CLI provider for autonomous orchestration.

The adapter uses the locally installed Gemini CLI with an explicit model and
the narrow ``auto_edit`` approval mode. It deliberately never passes
``--yolo`` or any equivalent permission-bypass flag. ``--skip-trust`` only
acknowledges the explicitly selected workspace for headless operation; it does
not grant additional tool permissions. The full prompt is sent on stdin rather
than argv because Windows has a command-line length limit. Gemini CLI's JSON output
is normalized into the common AgentRunResult contract so failover and the
autonomous runner can treat quota, timeout, and malformed output distinctly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from orchestrator.agents.base import Agent, AgentRunRequest, AgentRunResult
from orchestrator.config import GeminiAgentConfig


FORBIDDEN_FLAGS = ("--yolo", "-y")
ALLOWED_APPROVAL_MODES = {"default", "auto_edit", "plan"}
ALLOWED_AUTH_MODES = {"api-key", "oauth-personal"}
QUOTA_LIMIT_MARKERS = (
    "resource_exhausted",
    "quota has been exceeded",
    "quota exceeded",
    "rate limit",
    "rate-limited",
    "too many requests",
    "429",
    "usage limit",
    "limit reached",
)
AUTH_UNAVAILABLE_MARKERS = (
    "unsupported_client",
    "ineligibletiererror",
    "not supported for gemini code assist",
    "error authenticating",
    "authentication failed",
    "unauthenticated",
    "login required",
    "not logged in",
    "permission_denied",
    "project has been denied access",
)


def find_gemini_cli(explicit_path: str = "") -> tuple[Optional[str], str]:
    """Return the installed Gemini CLI path without starting a model run."""
    if explicit_path:
        path = Path(explicit_path).expanduser()
        if path.is_file():
            return str(path), f"použita ručně nastavená cesta v config.yaml: {path}"
        return None, f"config.yaml udává gemini.cli_path='{explicit_path}', ale soubor neexistuje"
    found = shutil.which("gemini")
    if found:
        return found, f"nalezeno v PATH: {found}"
    return None, "Gemini CLI 'gemini' nenalezeno v PATH."


def _quota_error(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in QUOTA_LIMIT_MARKERS)


def _auth_unavailable(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in AUTH_UNAVAILABLE_MARKERS)


def _response_text(raw: dict[str, Any]) -> str:
    for key in ("response", "result", "text", "output"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return ""


def _usage(raw: dict[str, Any]) -> dict[str, Optional[int]]:
    """Extract exact counters when Gemini CLI exposes them; never estimate."""
    candidates = [raw]
    stats = raw.get("stats")
    if isinstance(stats, dict):
        candidates.append(stats)
        models = stats.get("models")
        if isinstance(models, dict):
            candidates.extend(value for value in models.values() if isinstance(value, dict))
    values: dict[str, Optional[int]] = {
        "input_tokens": None,
        "output_tokens": None,
        "thinking_tokens": None,
        "total_tokens": None,
    }
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "candidates_tokens", "completion_tokens"),
        "thinking_tokens": ("thinking_tokens", "thoughts_tokens"),
        "total_tokens": ("total_tokens",),
    }
    for candidate in candidates:
        for target, keys in aliases.items():
            if values[target] is not None:
                continue
            for key in keys:
                value = candidate.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    values[target] = value
                    break
    if values["total_tokens"] is None and all(
        isinstance(values[key], int) for key in ("input_tokens", "output_tokens")
    ):
        values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
    return values


def _reported_model(raw: dict[str, Any], configured_model: str) -> Optional[str]:
    """Return the model reported by Gemini, or the model explicitly passed."""
    candidates = [raw]
    stats = raw.get("stats")
    if isinstance(stats, dict):
        candidates.append(stats)
    for source in candidates:
        for key in ("model", "model_id", "modelId"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return configured_model.strip() or None


class GeminiAgent(Agent):
    """Gemini CLI adapter using an explicit model and non-yolo approval mode."""

    name = "gemini"

    def __init__(self, config: GeminiAgentConfig):
        if config.approval_mode not in ALLOWED_APPROVAL_MODES:
            raise ValueError(
                f"GeminiAgent odmítá approval_mode={config.approval_mode!r}; "
                f"povolené jsou {sorted(ALLOWED_APPROVAL_MODES)} a nikdy yolo."
            )
        if config.auth_mode not in ALLOWED_AUTH_MODES:
            raise ValueError(
                f"GeminiAgent odmítá auth_mode={config.auth_mode!r}; "
                f"povolené jsou {sorted(ALLOWED_AUTH_MODES)}."
            )
        self.config = config
        self._cli_path, self._detect_note = find_gemini_cli(config.cli_path)

    def is_available(self) -> tuple[bool, str]:
        if not self._cli_path:
            return False, self._detect_note
        try:
            proc = subprocess.run(
                [*self._command_prefix(), "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"Gemini --version selhalo: {exc}"
        if proc.returncode != 0:
            return False, f"Gemini --version selhalo (kód {proc.returncode}): {proc.stderr.strip()}"
        return True, f"{proc.stdout.strip()} ({self._detect_note})"

    def _command_prefix(self) -> list[str]:
        """Return a directly executable Gemini command on Windows.

        npm installs ``gemini.CMD`` as a wrapper that launches Node through
        another shell process. On Windows that wrapper can outlive Python's
        ``subprocess`` timeout and leave a headless request hanging. When the
        package entrypoint is available, invoke Node and the bundled script
        directly; tests and non-Windows installations keep the normal CLI
        path unchanged.
        """
        if self._cli_path and os.name == "nt":
            cli_path = Path(self._cli_path)
            if cli_path.suffix.lower() in {".cmd", ".bat"}:
                node_path = shutil.which("node")
                script_path = cli_path.parent / "node_modules" / "@google" / "gemini-cli" / "bundle" / "gemini.js"
                if node_path and script_path.is_file():
                    return [node_path, str(script_path)]
        return [self._cli_path]

    def _build_command(self, request: AgentRunRequest) -> list[str]:
        assert self._cli_path
        command = [
            *self._command_prefix(),
            "-p",
            # A non-empty headless prompt is required by the Windows Gemini
            # CLI wrapper. Gemini appends stdin to this prompt, so the full
            # autonomous request still travels through stdin and cannot hit
            # the Windows command-line length limit. An empty -p value is
            # parsed as interactive mode by the current CLI and can leave a
            # child process waiting forever outside subprocess' timeout.
            "Read the complete task from stdin.",
            "--output-format",
            "json",
            "--approval-mode",
            self.config.approval_mode,
            "--model",
            self.config.model,
            "--skip-trust",
        ]
        for forbidden in FORBIDDEN_FLAGS:
            assert forbidden not in command, "safety invariant violated: forbidden flag in command"
        return command

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        available, note = self.is_available()
        if not available:
            return AgentRunResult(success=False, output_text="", error=note, model=self.config.model or None)

        prompt = request.prompt
        if request.context:
            prompt = f"{prompt}\n\n---\n{request.context}"
        prompt = (
            f"{prompt}\n\n---\n"
            "Pracuj skutečně v přiloženém pracovním adresáři a proveď konkrétní změnu. "
            "Nikdy nespouštěj git commit; ten provede orchestrátor po ověření. "
            "Testy po dokončení spouští orchestrátor. Vrať pouze JSON odpověď požadovanou úlohou."
        )
        effective = AgentRunRequest(
            project_path=request.project_path,
            prompt=prompt,
            session_id=request.session_id,
            output_schema=request.output_schema,
        )
        command = self._build_command(effective)
        # Keep both explicit mechanisms: --skip-trust is supported by current
        # Gemini CLI versions, while the environment variable also makes the
        # intended headless workspace policy visible to versions that use the
        # documented environment switch. Neither grants extra tool access.
        env = os.environ.copy()
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        # Gemini CLI selects API-key auth whenever GEMINI_API_KEY is present,
        # even when settings.json selects OAuth.  Make the selected mode
        # explicit in the child environment, without copying credentials or
        # mutating the user's profile.  The default api-key mode is the free
        # AI Studio path; oauth-personal is available as an explicit option.
        try:
            env.pop("GEMINI_CLI_HOME", None)
            if self.config.auth_mode == "oauth-personal":
                env.pop("GEMINI_API_KEY", None)
                env.pop("GOOGLE_API_KEY", None)
                env["GOOGLE_GENAI_USE_GCA"] = "true"
            else:
                env.pop("GOOGLE_GENAI_USE_GCA", None)
            proc = subprocess.run(
                command,
                cwd=str(request.project_path),
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.timeout_seconds,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            detail = str(exc.stderr or "").strip() if getattr(exc, "stderr", None) else ""
            return AgentRunResult(
                success=False,
                output_text="",
                error=(
                    f"Gemini neodpověděl do {self.config.timeout_seconds}s (timeout)."
                    + (f" stderr: {detail}" if detail else "")
                ),
                timed_out=True,
                model=self.config.model or None,
            )
        except OSError as exc:
            return AgentRunResult(success=False, output_text="", error=f"Nelze spustit Gemini CLI: {exc}", model=self.config.model or None)

        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        try:
            raw = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            error = stderr or stdout or f"exit code {proc.returncode}"
            return AgentRunResult(
                success=False,
                output_text=stdout,
                error=f"Gemini CLI nevrátilo platný JSON (exit kód {proc.returncode}): {error}",
                limited=_quota_error(error),
                unavailable=_auth_unavailable(error),
                model=self.config.model or None,
            )
        if not isinstance(raw, dict):
            return AgentRunResult(success=False, output_text="", error="Gemini CLI vrátilo JSON jiné než object")

        response = _response_text(raw)
        error = raw.get("error") if isinstance(raw.get("error"), str) else stderr
        usage = _usage(raw)
        fields = {**usage, "raw_response": raw, "session_id": raw.get("session_id") or raw.get("sessionId"), "model": _reported_model(raw, self.config.model)}
        if proc.returncode != 0 or error:
            message = error or response or f"Gemini CLI vrátilo exit kód {proc.returncode}"
            limited = _quota_error(message)
            return AgentRunResult(
                success=False,
                output_text=response,
                error=(f"Gemini CLI hlásí vyčerpání kvóty/limitu (LIMITED): {message}" if limited else message),
                limited=limited,
                unavailable=_auth_unavailable(message),
                **fields,
            )
        if not response:
            return AgentRunResult(
                success=False,
                output_text="",
                error="Gemini CLI vrátilo JSON bez textové odpovědi",
                **fields,
            )
        return AgentRunResult(success=True, output_text=response, error=None, **fields)
