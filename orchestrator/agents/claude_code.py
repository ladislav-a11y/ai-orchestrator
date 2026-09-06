"""Agent implementation that drives the locally installed Claude Code CLI.

Invocation contract (do not weaken this without updating AGENTS.md too):
  - always non-interactive: `claude -p "<prompt>" --output-format json ...`
  - NEVER passes --dangerously-skip-permissions / --allow-dangerously-skip-permissions
  - NEVER sets --permission-mode bypassPermissions (config.py also rejects this,
    this is a second, independent guard in case config validation is ever bypassed)
  - runs with cwd = the target project's directory, so Claude's own
    project-level .claude/settings.json permission rules apply as normal
  - every prompt gets NO_COMMIT_INSTRUCTION appended: the agent must never
    run `git commit` itself (claude_settings.py denies it at the permission
    level too) - only the orchestrator's own Git layer commits, and only
    after tests are verified (see runner.py/_maybe_commit)
  - every prompt also gets TEST_EXECUTION_INSTRUCTION appended: the
    orchestrator always runs test_command itself after the agent finishes,
    so the agent should not retry a test-invocation command after it's
    denied once - claude_settings.py's PreToolUse hook
    (orchestrator/hooks/test_command_guard.py) enforces the same rule at
    the permission level, this is the matching prompt-level nudge so the
    agent does not even try
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from orchestrator.agents.base import Agent, AgentRunRequest, AgentRunResult
from orchestrator.config import ClaudeCodeAgentConfig
from orchestrator.hooks.test_command_guard import read_saved_attempts

FORBIDDEN_FLAGS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
)
FORBIDDEN_PERMISSION_MODE = "bypassPermissions"

# Committing is the orchestrator's job (runner.py/_maybe_commit,
# autonomous.py/_commit_if_ready), done through its own Git layer only after
# tests have been verified - never the agent's. This is enforced a second,
# stronger way too (claude_settings.py denies `Bash(git commit:*)` outright),
# but the prompt still says it explicitly so the agent doesn't waste a turn
# attempting it and getting denied. `git status`/`git diff` stay fine to run.
NO_COMMIT_INSTRUCTION = (
    "Důležité pravidlo: NIKDY nespouštěj `git commit` (ani `git commit --amend`) - commit "
    "po skončení tvého běhu vytváří výhradně orchestrátor, až ověří testy. `git status` a "
    "`git diff` používat smíš a můžeš k ověření stavu, jen sám nic necommituj."
)

# Testy vždy spouští a vyhodnocuje orchestrátor sám (viz runner.py/
# run_test_command a autonomous.py) - agentovo vlastní spuštění testů nikdy
# nic nerozhoduje, ani kdyby prošlo. Prompt to říká výslovně navíc k hook
# breakeru (orchestrator/hooks/test_command_guard.py), který stejně po
# první zamítnuté dávce blokuje každý další ekvivalentní pokus - cílem je,
# aby agent po prvním zamítnutí vůbec nezkoušel jinou variantu příkazu a
# nemarnil tím tahy/tokeny (viz ten modul pro celé zdůvodnění).
TEST_EXECUTION_INSTRUCTION = (
    "Testy po dokončení úkolu vždy spouští a vyhodnocuje výhradně orchestrátor, nikdy sám "
    "agent - i kdyby ti spuštění prošlo. Pokud ti spuštění testovacího příkazu (pytest, "
    "`python -m pytest`, `python -m unittest`, přes `cmd` apod.) jednou zamítne systém "
    "oprávnění, NEZKOUŠEJ to znovu jinou variantou příkazu - další pokusy se stejně "
    "automaticky blokují a jen plýtvají časem. Pokračuj rovnou v editaci kódu podle zadání a "
    "na úplný závěr jen konstatuj, že ověření testů necháváš na orchestrátorovi."
)


# Substrings that, seen anywhere in the CLI's error or result text,
# indicate a quota/rate/session limit rather than an ordinary failure.
# Matched case-insensitively. Same rationale/approach as antigravity.py
# and codex.py.
QUOTA_LIMIT_MARKERS = (
    "resource_exhausted",
    "quota has been exceeded",
    "quota exceeded",
    "out of quota",
    "rate limit",
    "rate-limited",
    "rate_limit",
    "too many requests",
    "429",
    "session limit",
    "usage limit",
    "weekly limit",
    "credit balance",
    "insufficient credit",
    "insufficient_quota",
    "overloaded",
)

RETRY_AFTER_KEYS = ("retry_after_seconds", "retry_after", "retryAfterSeconds", "retryAfter")

_RETRY_AFTER_TEXT_RE = re.compile(
    r"retry(?:ing)?\s+(?:again\s+)?(?:in|after)\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|s|m)\b",
    re.IGNORECASE,
)

_RESET_AT_TEXT_RE = re.compile(
    r"resets?\s+([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
    re.IGNORECASE,
)

_RESET_TODAY_TEXT_RE = re.compile(
    r"resets?\s+(?:today\s+|at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
    re.IGNORECASE,
)


def _extract_retry_after_seconds(raw: dict, text: str) -> Optional[float]:
    for key in RETRY_AFTER_KEYS:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    match = _RETRY_AFTER_TEXT_RE.search(text or "")
    if match:
        value = float(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("m"):
            value *= 60
        return value

    match = _RESET_AT_TEXT_RE.search(text or "")
    if match:
        month, day, hour, minute, ampm = match.groups()
        now = datetime.now().astimezone()
        minute = minute or "00"
        reset = datetime.strptime(
            f"{month} {day} {now.year} {hour}:{minute} {ampm}",
            "%b %d %Y %I:%M %p",
        ).replace(tzinfo=now.tzinfo)
        if reset <= now:
            reset = reset.replace(year=now.year + 1)
        return max(0.0, (reset - now).total_seconds())

    match = _RESET_TODAY_TEXT_RE.search(text or "")
    if match:
        hour, minute, ampm = match.groups()
        now = datetime.now().astimezone()
        reset_time = datetime.strptime(
            f"{hour}:{minute or '00'} {ampm}", "%I:%M %p"
        ).time()
        reset = datetime.combine(now.date(), reset_time, tzinfo=now.tzinfo)
        if reset <= now:
            reset += timedelta(days=1)
        return max(0.0, (reset - now).total_seconds())

    return None


def _detect_quota_limit(raw: dict, error_text: str) -> tuple[bool, Optional[float]]:
    haystack = " ".join(
        str(part) for part in (raw.get("error"), raw.get("result"), error_text) if part
    ).lower()
    if not any(marker in haystack for marker in QUOTA_LIMIT_MARKERS):
        return False, None
    return True, _extract_retry_after_seconds(raw, error_text)


def _reported_model(raw: dict, usage: dict) -> Optional[str]:
    """Extract the model Claude actually reports for this CLI call.

    Claude Code's JSON receipt has used both direct model fields and a
    ``modelUsage`` map. The latter is the important case when PM deliberately
    omits ``--model`` and lets Claude choose by task type. Never substitute a
    configured catalog value here: missing provider evidence must remain
    visibly unknown.
    """
    for source in (raw, usage):
        for key in ("model", "model_id", "modelId"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    model_usage = raw.get("modelUsage")
    if isinstance(model_usage, dict):
        names = [str(name).strip() for name in model_usage if str(name).strip()]
        if names:
            return ", ".join(names)
    return None


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

    def _effective_model(self, request: AgentRunRequest) -> tuple[str, bool]:
        """Return (model_to_pass, was_explicitly_requested).

        A non-empty ``request.requested_model`` overrides ``config.model``
        for this call only - config.yaml is never mutated. An empty/blank
        override is treated as "no override" instead of sending a blank
        --model value to the CLI.
        """
        requested = (request.requested_model or "").strip()
        if requested:
            return requested, True
        return self.config.model, False

    def _build_command(self, request: AgentRunRequest) -> list[str]:
        assert self._cli_path
        cmd = [self._cli_path, "-p", request.prompt, "--output-format", "json"]

        if request.session_id:
            cmd += ["--resume", request.session_id]

        cmd += ["--permission-mode", self.config.permission_mode]

        effective_model, _ = self._effective_model(request)
        if effective_model:
            cmd += ["--model", effective_model]
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
            return AgentRunResult(
                success=False, output_text="", error=note,
                selection_reason=request.selection_reason,
            )

        prompt = request.prompt
        if request.context:
            prompt = f"{request.prompt}\n\n---\n{request.context}"
        prompt = f"{prompt}\n\n---\n{NO_COMMIT_INSTRUCTION}\n\n---\n{TEST_EXECUTION_INSTRUCTION}"
        effective_request = AgentRunRequest(
            project_path=request.project_path,
            prompt=prompt,
            session_id=request.session_id,
            requested_model=request.requested_model,
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
        except subprocess.TimeoutExpired as e:
            timeout_output = e.stdout or e.output or b""
            timeout_stderr = e.stderr or b""
            if isinstance(timeout_output, bytes):
                timeout_output = timeout_output.decode("utf-8", errors="replace")
            if isinstance(timeout_stderr, bytes):
                timeout_stderr = timeout_stderr.decode("utf-8", errors="replace")
            timeout_text = "\n".join(
                part.strip() for part in (timeout_output, timeout_stderr) if part and part.strip()
            )
            limited, retry_after_seconds = _detect_quota_limit({}, timeout_text)
            error = (
                f"Claude Code hlásí vyčerpání kvóty/limitu (LIMITED): {timeout_text}"
                if limited
                else f"Claude Code neodpověděl do {self.config.timeout_seconds}s (timeout)."
            )
            return AgentRunResult(
                success=False,
                output_text=timeout_output.strip(),
                error=error,
                limited=limited,
                timed_out=True,
                retry_after_seconds=retry_after_seconds,
                selection_reason=request.selection_reason,
            )
        except FileNotFoundError as e:
            return AgentRunResult(
                success=False, output_text="", error=f"Nelze spustit CLI: {e}",
                selection_reason=request.selection_reason,
            )

        raw: Optional[dict] = None
        try:
            raw = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            raw = None

        if raw is not None:
            is_error = bool(raw.get("is_error", proc.returncode != 0))
            result_text = raw.get("result", "")
            denials = raw.get("permission_denials") or []
            usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
            reported_model = _reported_model(raw, usage)
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if not isinstance(input_tokens, int) or isinstance(input_tokens, bool):
                input_tokens = None
            if not isinstance(output_tokens, int) or isinstance(output_tokens, bool):
                output_tokens = None
            output_details = usage.get("output_tokens_details")
            if not isinstance(output_details, dict):
                output_details = {}
            thinking_tokens = output_details.get("thinking_tokens")
            if not isinstance(thinking_tokens, int) or isinstance(thinking_tokens, bool):
                thinking_tokens = None
            total_tokens = usage.get("total_tokens")
            if not isinstance(total_tokens, int) or isinstance(total_tokens, bool):
                total_tokens = None
            if (
                total_tokens is None
                and input_tokens is not None
                and output_tokens is not None
            ):
                total_tokens = input_tokens + output_tokens
            # Never append anything to result_text/output_text here:
            # autonomous.py's DoD contract requires the agent's *own* last
            # message to be exactly one JSON object, and callers parse
            # `output_text` for it. Text appended after a valid JSON payload
            # (this note used to be concatenated directly onto it) breaks a
            # naive full-string json.loads and was the root cause of every
            # iteration in run 11b4aaae08b4 being misreported as
            # protocol_error despite an otherwise valid agent response - see
            # autonomous._extract_json (now robust to trailing/leading text
            # too, as defense in depth) and AgentRunResult.permission_denials
            # (the structured, out-of-band place for this count).
            response_session_id = raw.get("session_id") or effective_request.session_id
            error_message = None if not is_error else (result_text or "Claude Code vrátil chybu.")
            limited = False
            retry_after_seconds = None
            if is_error and error_message:
                limited, retry_after_seconds = _detect_quota_limit(raw, error_message)
                if limited:
                    error_message = f"Claude Code hlásí vyčerpání kvóty/limitu (LIMITED): {error_message}"

            return AgentRunResult(
                success=(not is_error),
                output_text=result_text,
                raw_response=raw,
                session_id=raw.get("session_id"),
                cost_usd=raw.get("total_cost_usd"),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                thinking_tokens=thinking_tokens,
                total_tokens=total_tokens,
                error=error_message,
                permission_denials=len(denials),
                permission_denial_details=denials,
                breaker_saved_attempts=read_saved_attempts(request.project_path, response_session_id),
                limited=limited,
                retry_after_seconds=retry_after_seconds,
                model=reported_model,
                model_source=("reported" if reported_model else None),
                selection_reason=request.selection_reason,
            )

        # Could not parse JSON - fall back to raw stdout/stderr.
        success = proc.returncode == 0
        error_message = None if success else (
            proc.stderr.strip()
            or proc.stdout.strip()
            or f"exit code {proc.returncode}"
        )
        limited = False
        retry_after_seconds = None
        if not success and error_message:
            limited, retry_after_seconds = _detect_quota_limit({}, error_message)
            if limited:
                error_message = f"Claude Code hlásí vyčerpání kvóty/limitu (LIMITED): {error_message}"

        return AgentRunResult(
            success=success,
            output_text=proc.stdout.strip(),
            error=error_message,
            limited=limited,
            retry_after_seconds=retry_after_seconds,
            selection_reason=request.selection_reason,
        )
