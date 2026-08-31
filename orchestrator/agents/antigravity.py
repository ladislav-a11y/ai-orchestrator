"""Agent implementation that drives the locally installed Antigravity CLI (`agy`).

Invocation contract (do not weaken this without updating AGENTS.md too):
  - always non-interactive: `agy --print "<prompt>" --output-format json`
  - NEVER passes --dangerously-skip-permissions / --allow-dangerously-skip-permissions
    or any other automatic permission-bypass flag; instead passes
    `--mode accept-edits` (configurable, see AntigravityAgentConfig.mode),
    which auto-approves file edits but still denies-by-default anything else
    (e.g. shell commands) in non-interactive mode - verified against the real
    CLI: a denied command comes back as a normal JSON status=ERROR with an
    "error" message, never a hang and never an implicit approval
  - explicitly adds the target project workspace via `--add-dir <project_path>`
    and runs subprocess with cwd = the target project's directory (already
    validated against workspace_root by Config.resolve_project before
    AgentRunRequest is built - see config.py's `_ensure_within_workspace`).
    Empirical testing of Antigravity CLI on Windows proved that subprocess cwd
    alone is not sufficient (without `--add-dir`, the CLI operates in a scratch
    workspace under ~/.gemini/antigravity-cli/scratch). Passing `--add-dir`
    explicitly mounts the target project. `--sandbox` is intentionally omitted
    because real Antigravity CLI runs on Windows fail with error 'context canceled'.
  - every prompt gets NO_COMMIT_INSTRUCTION appended: the agent must never
    run `git commit` itself - only the orchestrator's own Git layer commits,
    and only after tests are verified (see runner.py/_maybe_commit). Unlike
    ClaudeCodeAgent, there is no second, project-local settings-file layer
    for this (see AGENTS.md rule 11 for why: agy's own permission config
    lives under the user's home directory, not the project, so there is
    nothing safe/repo-local to write into) - the prompt instruction plus the
    verified default-deny of any Bash-equivalent tool call (see the
    --mode/--dangerously-skip-permissions note above, which covers
    `git commit` too - it is just another shell command from agy's point of
    view) is the actual guarantee here
  - every prompt also gets TEST_EXECUTION_INSTRUCTION appended, matching
    ClaudeCodeAgent's contract (see claude_code.py for the full rationale)
  - only a JSON response with status == "SUCCESS" counts as success; any
    other status (ERROR, LIMITED, ...) is converted into a clear
    AgentRunResult(success=False, error=...) - never silently treated as ok
  - the real CLI has no dedicated JSON status for quota/rate/session limits
    (confirmed by inspecting the installed agy.exe: it surfaces these as an
    ordinary status="ERROR" response, with messages built around the
    upstream gRPC code "RESOURCE_EXHAUSTED" and strings like "generation
    quota has been exceeded. Please try again later." / "Too Many
    Requests") - _detect_quota_limit() recognizes those patterns and maps
    them onto AgentRunResult.limited=True instead of an ordinary error, so
    callers can back off/retry instead of treating it as a hard failure
  - usage.{input_tokens,output_tokens,thinking_tokens,total_tokens}, when
    present in the CLI's JSON, are copied onto the matching AgentRunResult
    fields (confirmed present on both SUCCESS and ERROR responses)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from orchestrator.agents.base import Agent, AgentRunRequest, AgentRunResult
from orchestrator.config import ANTIGRAVITY_ALLOWED_MODES, AntigravityAgentConfig

FORBIDDEN_FLAGS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
)

SUCCESS_STATUS = "SUCCESS"

# Substrings that, seen anywhere in the CLI's "error"/"response" text,
# indicate a quota/rate/session limit rather than an ordinary failure - see
# the module docstring for how these were confirmed against the real CLI
# binary. Matched case-insensitively.
QUOTA_LIMIT_MARKERS = (
    "resource_exhausted",
    "quota has been exceeded",
    "quota exceeded",
    "quota reached",
    "out of quota",
    "rate limit",
    "rate-limited",
    "too many requests",
    "session limit",
    "usage limit",
)

# Keys the CLI (or a future version of it) might use to report how long to
# wait before retrying, checked in order. None of these are confirmed to
# exist in the current CLI output - this is best-effort, matching the DoD's
# "pokud ji CLI poskytne" (if the CLI provides it).
RETRY_AFTER_KEYS = ("retry_after_seconds", "retry_after", "retryAfterSeconds", "retryAfter")

# Fallback: parse a "retry in/after N seconds|minutes" phrase out of the
# free-text error message itself.
_RETRY_AFTER_TEXT_RE = re.compile(
    r"retry(?:ing)?\s+(?:again\s+)?(?:in|after)\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|s|m)\b",
    re.IGNORECASE,
)

_RESET_IN_TEXT_RE = re.compile(
    r"resets?\s+in\s+(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?\b",
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

    reset_match = _RESET_IN_TEXT_RE.search(text or "")
    if reset_match:
        hours = int(reset_match.group(1) or 0)
        minutes = int(reset_match.group(2) or 0)
        seconds = int(reset_match.group(3) or 0)
        return float(hours * 3600 + minutes * 60 + seconds)

    return None


def _detect_quota_limit(raw: dict, error_text: str) -> tuple[bool, Optional[float]]:
    haystack = " ".join(
        str(part) for part in (raw.get("error"), raw.get("response"), error_text) if part
    ).lower()
    if not any(marker in haystack for marker in QUOTA_LIMIT_MARKERS):
        return False, None
    return True, _extract_retry_after_seconds(raw, error_text)


# Same contract as ClaudeCodeAgent's NO_COMMIT_INSTRUCTION - committing is
# the orchestrator's job only, done through its own Git layer after tests
# have been verified, never the agent's.
NO_COMMIT_INSTRUCTION = (
    "Důležité pravidlo: NIKDY nespouštěj `git commit` (ani `git commit --amend`) - commit "
    "po skončení tvého běhu vytváří výhradně orchestrátor, až ověří testy. `git status` a "
    "`git diff` používat smíš a můžeš k ověření stavu, jen sám nic necommituj."
)

# Same contract as ClaudeCodeAgent's TEST_EXECUTION_INSTRUCTION - the
# orchestrator always runs test_command itself after the agent finishes.
TEST_EXECUTION_INSTRUCTION = (
    "Testy po dokončení úkolu vždy spouští a vyhodnocuje výhradně orchestrátor, nikdy sám "
    "agent - i kdyby ti spuštění prošlo. Pokud ti spuštění testovacího příkazu (pytest, "
    "`python -m pytest`, `python -m unittest`, přes `cmd` apod.) jednou zamítne systém "
    "oprávnění, NEZKOUŠEJ to znovu jinou variantou příkazu - další pokusy se stejně "
    "automaticky blokují a jen plýtvají časem. Pokračuj rovnou v editaci kódu podle zadání a "
    "na úplný závěr jen konstatuj, že ověření testů necháváš na orchestrátorovi."
)


def find_antigravity_cli(explicit_path: str = "") -> tuple[Optional[str], str]:
    """Return (path_or_None, human_readable_note)."""
    if explicit_path:
        p = Path(explicit_path)
        if p.exists():
            return str(p), f"použita ručně nastavená cesta v config.yaml: {p}"
        return None, f"config.yaml udává antigravity.cli_path='{explicit_path}', ale ten soubor neexistuje"

    on_path = shutil.which("agy")
    if on_path:
        return on_path, f"nalezeno v PATH: {on_path}"

    return None, (
        "Antigravity CLI 'agy' nenalezeno v PATH. Nainstaluj Antigravity CLI, "
        "nebo nastav antigravity.cli_path v config.yaml ručně."
    )


class AntigravityAgent(Agent):
    name = "antigravity"

    def __init__(self, config: AntigravityAgentConfig):
        # Second, independent guard against the same thing config.py's
        # load_config() already checks - protects callers that construct a
        # Config/AntigravityAgentConfig by hand instead of via load_config().
        if (config.mode or "") not in ANTIGRAVITY_ALLOWED_MODES:
            raise ValueError(
                f"AntigravityAgent odmítá antigravity.mode='{config.mode}' - povolené hodnoty jsou "
                f"{sorted(ANTIGRAVITY_ALLOWED_MODES - {''})} (nebo prázdné). Non-interactive běh "
                "nikdy nesmí obcházet kontrolu oprávnění."
            )
        self.config = config
        self._cli_path, self._detect_note = find_antigravity_cli(config.cli_path)

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
        cmd = [
            self._cli_path,
            "--add-dir",
            str(request.project_path),
            "--print",
            request.prompt,
            "--output-format",
            "json",
        ]

        if request.session_id:
            cmd += ["--conversation", request.session_id]

        if self.config.mode:
            cmd += ["--mode", self.config.mode]
        if self.config.model:
            cmd += ["--model", self.config.model]

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
        prompt = f"{prompt}\n\n---\n{NO_COMMIT_INSTRUCTION}\n\n---\n{TEST_EXECUTION_INSTRUCTION}"
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
        except subprocess.TimeoutExpired as e:
            timeout_stderr = (e.stderr or "").strip() if isinstance(e.stderr, str) else ""
            return AgentRunResult(
                success=False,
                output_text="",
                error=(
                    f"Antigravity neodpověděl do {self.config.timeout_seconds}s (timeout)."
                    + (f" stderr: {timeout_stderr}" if timeout_stderr else "")
                ),
                timed_out=True,
            )
        except FileNotFoundError as e:
            return AgentRunResult(success=False, output_text="", error=f"Nelze spustit CLI: {e}")

        raw: Optional[dict] = None
        try:
            raw = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            raw = None

        if raw is None:
            stderr = proc.stderr.strip()
            return AgentRunResult(
                success=False,
                output_text=proc.stdout.strip(),
                error=(
                    f"Antigravity CLI nevrátilo platný JSON (exit kod {proc.returncode})"
                    + (f": {stderr}" if stderr else ".")
                ),
            )

        status = str(raw.get("status") or "").upper()
        response_text = raw.get("response") or ""
        conversation_id = raw.get("conversation_id") or effective_request.session_id

        usage = raw.get("usage") or {}
        token_fields = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "thinking_tokens": usage.get("thinking_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }

        if status != SUCCESS_STATUS:
            error_message = raw.get("error") or response_text or f"Antigravity CLI vrátilo stav '{status or 'UNKNOWN'}'."
            limited, retry_after_seconds = _detect_quota_limit(raw, error_message)
            if limited:
                return AgentRunResult(
                    success=False,
                    output_text=response_text,
                    raw_response=raw,
                    session_id=conversation_id,
                    error=f"Antigravity CLI hlásí vyčerpání kvóty/limitu (LIMITED): {error_message}",
                    limited=True,
                    retry_after_seconds=retry_after_seconds,
                    **token_fields,
                )
            return AgentRunResult(
                success=False,
                output_text=response_text,
                raw_response=raw,
                session_id=conversation_id,
                error=f"Antigravity CLI vrátilo neúspěšný stav '{status or 'UNKNOWN'}': {error_message}",
                **token_fields,
            )

        return AgentRunResult(
            success=True,
            output_text=response_text,
            raw_response=raw,
            session_id=conversation_id,
            error=None,
            **token_fields,
        )
