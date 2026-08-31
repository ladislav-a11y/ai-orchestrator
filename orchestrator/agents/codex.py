"""Agent implementation that drives the locally installed OpenAI Codex CLI (`codex`).

Invocation contract (do not weaken this without updating AGENTS.md too):
  - Verified against the real `codex exec --help` of Codex CLI 0.149.0. That
    CLI does NOT have an `--ask-for-approval` flag - `codex exec` is already
    the headless/scripting subcommand (there is no terminal to prompt), and
    what a run is allowed to do is governed entirely by `--sandbox`. An
    earlier version of this adapter passed `--ask-for-approval never`, which
    0.149.0 rejects outright as an unknown argument (the run would fail
    before doing anything) - do not reintroduce it.
  - always non-interactive: the prompt is sent on stdin to `codex exec ... -`
    so Windows `.cmd` command-line limits cannot truncate long autonomous
    iteration prompts
    (`--json` makes it emit one JSON object per line on stdout instead of
    human-formatted text; `--ignore-user-config` makes the run's behavior
    depend only on the flags this adapter passes, not on whatever a
    machine's `~/.codex/config.toml` happens to contain; `--approve-for-me`
    is what actually lets a headless run proceed without a human answering
    approval prompts - it does not by itself widen what's allowed, that is
    still `--sandbox`'s job, see below)
  - Codex CLI 0.149.0 has a verified `exec resume` permission regression:
    a fresh `workspace-write` session can run read-only shell commands such
    as `git status`, but the same session resumed with `codex exec resume`
    rejects them as `blocked by policy`. Therefore this adapter deliberately
    ignores incoming Codex `session_id` values and starts each autonomous
    iteration as a fresh session with the full explicit sandbox/approval
    flags. Do not re-enable `resume` until the installed CLI can preserve or
    reapply the same permission context on resumed sessions.
  - NEVER passes `--dangerously-bypass-approvals-and-sandbox` (or its short
    alias `--yolo`) or `--dangerously-bypass-hook-trust` - those flags
    disable the sandbox/approval/hook-trust safety net entirely and are
    Codex's equivalent of Claude's `--dangerously-skip-permissions` /
    Antigravity's `--dangerously-skip-permissions`. Instead this adapter
    always passes an explicit `--sandbox` value from
    CodexAgentConfig.sandbox_mode, restricted to `read-only` or
    `workspace-write` (never `danger-full-access`) - see
    CODEX_ALLOWED_SANDBOX_MODES in config.py. `workspace-write` still denies
    network access and any filesystem write outside the working directory;
    it is the Codex analogue of Antigravity's `--mode accept-edits`.
    `--approve-for-me` only removes the interactive prompt for actions the
    sandbox would already allow - a command the sandbox blocks still fails
    instead of silently running unrestricted.
  - runs with `--cd` (and the subprocess's own `cwd`) set to the target
    project's directory, which is already validated against workspace_root
    by `Config.resolve_project` before an `AgentRunRequest` is built (see
    config.py's `_ensure_within_workspace`), so Codex only ever operates
    inside the one project directory it was invoked for
  - every prompt gets NO_COMMIT_INSTRUCTION appended: the agent must never
    run `git commit` itself - only the orchestrator's own Git layer commits,
    and only after tests are verified (see runner.py/_maybe_commit). Same
    rationale as AntigravityAgent (see antigravity.py): the installed
    `codex` CLI has no repo-local settings file this orchestrator controls,
    so the prompt instruction plus the sandbox/approval guarantee above
    (a shell command is just another sandboxed action from Codex's point of
    view) is the actual guarantee here.
  - every prompt also gets TEST_EXECUTION_INSTRUCTION appended, matching
    ClaudeCodeAgent's contract (see claude_code.py for the full rationale)
  - `codex exec --json` streams JSONL events, not one JSON blob. Each line
    is parsed independently. Current Codex 0.149.x emits events such as
    `thread.started`, `item.completed` with an `agent_message` item, and
    `turn.completed` with usage; older builds used a nested `msg` object.
    Both shapes are accepted. A completed turn (and no error event) counts
    as SUCCESS; an error event, a non-zero exit with no completed turn, or
    output with no recognizable event at all are converted into
    AgentRunResult(success=False, error=...) - never silently treated as ok.
  - the CLI has no dedicated JSON status for quota/rate/session limits, so
    _detect_quota_limit() recognizes common substrings (rate limit, quota,
    429, usage limit, RESOURCE_EXHAUSTED, ...) in the error text, matching
    the same approach as antigravity.py's _detect_quota_limit(), and maps
    them onto AgentRunResult.limited=True instead of an ordinary error.
  - usage.{input_tokens,output_tokens,thinking_tokens,total_tokens}, when a
    `token_count` event is present, are copied onto the matching
    AgentRunResult fields, taking the *last* such event in the stream
    (Codex reports cumulative totals per turn, so the last one reflects the
    whole run).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from datetime import datetime, timedelta
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from orchestrator.agents.base import Agent, AgentRunRequest, AgentRunResult
from orchestrator.config import CODEX_ALLOWED_SANDBOX_MODES, CodexAgentConfig

FORBIDDEN_FLAGS = (
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
    "--dangerously-bypass-hook-trust",
)

# Substrings that, seen anywhere in an "error" event's message text,
# indicate a quota/rate/session limit rather than an ordinary failure.
# Matched case-insensitively. Same rationale/approach as
# antigravity.py's QUOTA_LIMIT_MARKERS.
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
)

# Keys a Codex error event might use to report how long to wait before
# retrying, checked in order. None of these are confirmed to exist in the
# real CLI output - best-effort, matching the DoD's "pokud ji CLI poskytne".
RETRY_AFTER_KEYS = ("retry_after_seconds", "retry_after", "retryAfterSeconds", "retryAfter")

_RETRY_AFTER_TEXT_RE = re.compile(
    r"retry(?:ing)?\s+(?:again\s+)?(?:in|after)\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|s|m)\b",
    re.IGNORECASE,
)

_RETRY_AT_TEXT_RE = re.compile(
    r"try\s+again\s+at\s+([A-Za-z]{3})\s+(\d{1,2})(?:st|nd|rd|th)?,\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)

_RETRY_TODAY_TEXT_RE = re.compile(
    r"try\s+again\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b",
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

    match = _RETRY_AT_TEXT_RE.search(text or "")
    if match:
        month, day, year, hour, minute, ampm = match.groups()
        reset = datetime.strptime(
            f"{month} {day} {year} {hour}:{minute} {ampm}",
            "%b %d %Y %I:%M %p",
        ).astimezone()
        now = datetime.now().astimezone()
        return max(0.0, (reset - now).total_seconds())

    match = _RETRY_TODAY_TEXT_RE.search(text or "")
    if match:
        hour, minute, ampm = match.groups()
        now = datetime.now().astimezone()
        retry_time = datetime.strptime(
            f"{hour}:{minute or '00'} {ampm}", "%I:%M %p"
        ).time()
        retry_at = datetime.combine(now.date(), retry_time, tzinfo=now.tzinfo)
        if retry_at <= now:
            retry_at += timedelta(days=1)
        return max(0.0, (retry_at - now).total_seconds())

    return None


def _detect_quota_limit(raw: dict, error_text: str) -> tuple[bool, Optional[float]]:
    haystack = " ".join(str(part) for part in (raw.get("message"), error_text) if part).lower()
    if not any(marker in haystack for marker in QUOTA_LIMIT_MARKERS):
        return False, None
    return True, _extract_retry_after_seconds(raw, error_text)


# Same contract as ClaudeCodeAgent's / AntigravityAgent's NO_COMMIT_INSTRUCTION.
NO_COMMIT_INSTRUCTION = (
    "Důležité pravidlo: NIKDY nespouštěj `git commit` (ani `git commit --amend`) - commit "
    "po skončení tvého běhu vytváří výhradně orchestrátor, až ověří testy. `git status` a "
    "`git diff` používat smíš a můžeš k ověření stavu, jen sám nic necommituj."
)

# Same contract as ClaudeCodeAgent's / AntigravityAgent's TEST_EXECUTION_INSTRUCTION.
TEST_EXECUTION_INSTRUCTION = (
    "Testy po dokončení úkolu vždy spouští a vyhodnocuje výhradně orchestrátor, nikdy sám "
    "agent - i kdyby ti spuštění prošlo. Pokud ti spuštění testovacího příkazu (pytest, "
    "`python -m pytest`, `python -m unittest`, přes `cmd` apod.) jednou zamítne systém "
    "oprávnění, NEZKOUŠEJ to znovu jinou variantou příkazu - další pokusy se stejně "
    "automaticky blokují a jen plýtvají časem. Pokračuj rovnou v editaci kódu podle zadání a "
    "na úplný závěr jen konstatuj, že ověření testů necháváš na orchestrátorovi."
)


def find_codex_cli(explicit_path: str = "") -> tuple[Optional[str], str]:
    """Return (path_or_None, human_readable_note)."""
    if explicit_path:
        p = Path(explicit_path)
        if p.exists():
            return str(p), f"použita ručně nastavená cesta v config.yaml: {p}"
        return None, f"config.yaml udává codex.cli_path='{explicit_path}', ale ten soubor neexistuje"

    on_path = shutil.which("codex")
    if on_path:
        return on_path, f"nalezeno v PATH: {on_path}"

    return None, (
        "OpenAI Codex CLI 'codex' nenalezeno v PATH. Nainstaluj Codex CLI, "
        "nebo nastav codex.cli_path v config.yaml ručně."
    )


def _iter_events(stdout: str):
    """Parse `codex exec --json` output: one JSON object per line.

    Blank lines and lines that aren't valid JSON are skipped (the CLI may
    interleave non-JSON diagnostic lines even in --json mode). Returns the
    list of successfully parsed event dicts, in order.
    """
    events: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


class CodexAgent(Agent):
    name = "codex"

    def __init__(self, config: CodexAgentConfig):
        # Second, independent guard against the same thing config.py's
        # load_config() already checks - protects callers that construct a
        # Config/CodexAgentConfig by hand instead of via load_config().
        if config.sandbox_mode not in CODEX_ALLOWED_SANDBOX_MODES:
            raise ValueError(
                f"CodexAgent odmítá codex.sandbox_mode='{config.sandbox_mode}' - povolené "
                f"hodnoty jsou {sorted(CODEX_ALLOWED_SANDBOX_MODES)}. Non-interactive běh "
                "nikdy nesmí obcházet sandbox (danger-full-access je zakázané)."
            )
        self.config = config
        self._cli_path, self._detect_note = find_codex_cli(config.cli_path)

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

    def _build_command(
        self,
        request: AgentRunRequest,
        output_schema_path: Optional[Path] = None,
    ) -> list[str]:
        assert self._cli_path
        cmd = [self._cli_path, "exec"]
        # Codex CLI 0.149.0 reproducibly loses the usable shell permission
        # context on `exec resume`: even read-only `git status` is rejected
        # as "blocked by policy". Until resume can preserve/reapply the
        # sandbox/approval context, every autonomous iteration starts a fresh
        # Codex session with explicit permissions.
        cmd += [
            "--json",
            "--cd",
            str(request.project_path),
            "--ignore-user-config",
        ]
        if self.config.sandbox_mode == "read-only":
            cmd += ["--sandbox", "read-only"]
        elif self.config.sandbox_mode == "workspace-write":
            cmd += ["--approve-for-me"]
        else:
            raise ValueError(f"Unsupported Codex sandbox mode: {self.config.sandbox_mode}")
        if self.config.model:
            cmd += ["--model", self.config.model]
        if output_schema_path is not None:
            cmd += ["--output-schema", str(output_schema_path)]
        # `codex exec -` reads the prompt from stdin.  Never put an autonomous
        # prompt on the command line: on Windows the codex.cmd wrapper inherits
        # cmd.exe's short command-line ceiling and fails before Codex starts.
        cmd += ["-"]

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
            output_schema=request.output_schema,
        )

        output_schema_path: Optional[Path] = None
        if effective_request.output_schema is not None:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".json",
                prefix="ai-orchestrator-codex-schema-",
                delete=False,
            ) as schema_file:
                json.dump(effective_request.output_schema, schema_file, ensure_ascii=False)
                output_schema_path = Path(schema_file.name)
        cmd = self._build_command(effective_request, output_schema_path)

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(request.project_path),
                input=prompt,
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
                    f"Codex neodpověděl do {self.config.timeout_seconds}s (timeout)."
                    + (f" stderr: {timeout_stderr}" if timeout_stderr else "")
                ),
                timed_out=True,
            )
        except FileNotFoundError as e:
            return AgentRunResult(success=False, output_text="", error=f"Nelze spustit CLI: {e}")
        finally:
            if output_schema_path is not None:
                output_schema_path.unlink(missing_ok=True)

        events = _iter_events(proc.stdout)
        if not events:
            stderr = proc.stderr.strip()
            return AgentRunResult(
                success=False,
                output_text=proc.stdout.strip(),
                error=(
                    f"Codex CLI nevrátilo platný JSON výstup (exit kod {proc.returncode})"
                    + (f": {stderr}" if stderr else ".")
                ),
            )

        error_event: Optional[dict] = None
        complete_event: Optional[dict] = None
        last_agent_message = ""
        usage = {}
        session_id = effective_request.session_id
        for event in events:
            event_type = str(event.get("type") or "").lower()
            if event_type == "thread.started" and event.get("thread_id"):
                session_id = event["thread_id"]
            elif event_type == "turn.completed":
                complete_event = event
                usage = event.get("usage") or {}
            elif event_type in {"error", "turn.failed"}:
                error_event = event.get("error") if isinstance(event.get("error"), dict) else event
            elif event_type == "item.completed":
                item = event.get("item") if isinstance(event.get("item"), dict) else {}
                item_type = str(item.get("type") or "").lower()
                if item_type == "agent_message":
                    last_agent_message = item.get("text") or last_agent_message
                elif item_type in {"error", "turn_failed"}:
                    error_event = item

            msg = event.get("msg") if isinstance(event.get("msg"), dict) else event
            msg_type = str(msg.get("type") or "").lower()
            if event.get("id"):
                session_id = event.get("id") or session_id
            if msg_type == "error":
                error_event = msg
            elif msg_type == "token_count":
                usage = msg
            elif msg_type == "agent_message":
                last_agent_message = msg.get("message") or last_agent_message
            elif msg_type == "task_complete":
                complete_event = msg
                if msg.get("last_agent_message"):
                    last_agent_message = msg.get("last_agent_message")

        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        total_tokens = usage.get("total_tokens")
        if (
            total_tokens is None
            and isinstance(input_tokens, int) and not isinstance(input_tokens, bool)
            and isinstance(output_tokens, int) and not isinstance(output_tokens, bool)
        ):
            # Exact arithmetic derivation from reported counters, not a
            # tokenizer or model-pricing estimate.
            total_tokens = input_tokens + output_tokens
        token_fields = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "thinking_tokens": usage.get("reasoning_output_tokens", usage.get("thinking_tokens")),
            "total_tokens": total_tokens,
        }
        raw_response = {"events": events}

        if error_event is not None:
            error_message = error_event.get("message") or "Codex CLI vrátilo chybu."
            limited, retry_after_seconds = _detect_quota_limit(error_event, error_message)
            if limited:
                return AgentRunResult(
                    success=False,
                    output_text=last_agent_message,
                    raw_response=raw_response,
                    session_id=session_id,
                    error=f"Codex CLI hlásí vyčerpání kvóty/limitu (LIMITED): {error_message}",
                    limited=True,
                    retry_after_seconds=retry_after_seconds,
                    **token_fields,
                )
            return AgentRunResult(
                success=False,
                output_text=last_agent_message,
                raw_response=raw_response,
                session_id=session_id,
                error=f"Codex CLI vrátilo chybu: {error_message}",
                **token_fields,
            )

        if complete_event is None:
            stderr = proc.stderr.strip()
            return AgentRunResult(
                success=False,
                output_text=last_agent_message,
                raw_response=raw_response,
                session_id=session_id,
                error=(
                    "Codex CLI výstup neobsahuje 'task_complete' ani 'error' událost - "
                    f"neúplný výstup (exit kod {proc.returncode})"
                    + (f": {stderr}" if stderr else ".")
                ),
                **token_fields,
            )

        return AgentRunResult(
            success=True,
            output_text=last_agent_message,
            raw_response=raw_response,
            session_id=session_id,
            error=None,
            **token_fields,
        )


def contract_fingerprint() -> str:
    """Stable fingerprint of the code that decides what actually gets sent to
    the real Codex binary (flags, sandbox mode, --output-schema, stdin usage
    - see `CodexAgent._build_command`'s docstring/module docstring above).

    Used by `doctor.py`'s live-verification receipt (see AGENTS.md rule 14):
    a live run's evidence is only trustworthy for as long as this contract
    hasn't changed underneath it. If `_build_command` is edited, this value
    changes too, and a previously written receipt is detectably stale -
    never silently treated as still-valid evidence of the *new* contract.
    """
    source = inspect.getsource(CodexAgent._build_command)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()
