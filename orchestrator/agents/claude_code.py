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

# Jazykový kontrakt vstupu a výstupu tohoto providera: viz langclaude-code.json.

import json
import os
import re
from datetime import datetime, timedelta
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from orchestrator.agents.base import (
    Agent,
    AgentRunRequest,
    AgentRunResult,
    model_from_paths,
    with_provider_status,
)
from orchestrator.agents.slack_provider_notifications import notify_provider_run
from orchestrator.agents.usage_ledger import record_provider_run
from orchestrator.config import ClaudeCodeAgentConfig
from orchestrator.hooks.test_command_guard import read_saved_attempts

FORBIDDEN_FLAGS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
)
FORBIDDEN_PERMISSION_MODE = "bypassPermissions"
BROKER_ONLY_API_KEY = "ANTHROPIC_API_KEY"


def _cli_environment() -> dict[str, str]:
    """Return CLI environment without the broker-owned Models API key.

    Claude Code work and identity probes must use the user's CLI login.  The
    API key belongs exclusively to provider-broker's read-only model catalog
    refresh and must never alter the authentication of a provider run.
    """
    environment = os.environ.copy()
    environment.pop(BROKER_ONLY_API_KEY, None)
    return environment


def _identity_contract() -> dict[str, Any]:
    contract = json.loads(
        Path(__file__).with_name("langclaude-code.json").read_text(encoding="utf-8")
    )
    probe = contract["identity_probe"]
    if not isinstance(probe, dict):
        raise ValueError("langclaude-code.json neobsahuje identity_probe objekt")
    prompt = probe.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("langclaude-code.json neobsahuje identity_probe.prompt")
    permission_mode = probe.get("permission_mode")
    if not isinstance(permission_mode, str) or not permission_mode.strip():
        raise ValueError("langclaude-code.json neobsahuje identity_probe.permission_mode")
    discovery = probe.get("model_discovery")
    paths = discovery.get("response_paths") if isinstance(discovery, dict) else None
    if not isinstance(paths, list) or not all(isinstance(path, str) and path.strip() for path in paths):
        raise ValueError("langclaude-code.json neobsahuje model_discovery.response_paths")
    return probe


def _task_receipt_contract() -> dict[str, Any]:
    contract = json.loads(
        Path(__file__).with_name("langclaude-code.json").read_text(encoding="utf-8")
    )
    receipt = contract.get("task_execution_receipt")
    return receipt if isinstance(receipt, dict) else {}

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


def _read_temp_output(handle) -> str:
    """Read a temporary CLI output handle without inheriting a pipe."""
    handle.flush()
    handle.seek(0)
    value = handle.read()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _run_claude_command(command: list[str], *, cwd: str, timeout: float, env: dict) -> subprocess.CompletedProcess:
    """Run Claude without a Windows pipe that a child process can keep open.

    Claude Desktop occasionally leaves a helper process attached to stdout or
    stderr.  ``subprocess.run(capture_output=True)`` then waits for EOF even
    after the provider's wall-clock timeout.  Temporary files preserve the
    same captured result while allowing the timeout to return deterministically.
    """
    with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(mode="w+b") as stderr_file:
        options = {
            "stdin": subprocess.DEVNULL,
            "stdout": stdout_file,
            "stderr": stderr_file,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                **options,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or _read_temp_output(stdout_file)
            stderr = exc.stderr or _read_temp_output(stderr_file)
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from exc

        stdout = completed.stdout if completed.stdout is not None else _read_temp_output(stdout_file)
        stderr = completed.stderr if completed.stderr is not None else _read_temp_output(stderr_file)
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            stdout=stdout,
            stderr=stderr,
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
    discovery = _identity_contract()["model_discovery"]
    response_paths = discovery["response_paths"]
    direct_paths = [path for path in response_paths if path.rsplit(".", 1)[-1] not in {"modelUsage", "model_usage"}]
    direct_model = model_from_paths([raw, usage], direct_paths)
    if direct_model:
        return direct_model

    receipt_contract = _task_receipt_contract()
    response_format = receipt_contract.get("response_format", {}) if isinstance(receipt_contract, dict) else {}
    required_fields = response_format.get("required_fields", []) if isinstance(response_format, dict) else []
    receipt_model_field = "model" if "model" in required_fields else None
    if receipt_model_field and isinstance(raw.get("result"), str):
        try:
            receipt = json.loads(raw["result"])
        except (json.JSONDecodeError, TypeError, ValueError):
            receipt = None
        if isinstance(receipt, dict):
            receipt_model = receipt.get(receipt_model_field)
            if isinstance(receipt_model, str) and receipt_model.strip():
                return receipt_model.strip()

    # Claude Code may report internal model activity together with the model
    # that produced the task response.  Returning all modelUsage keys as one
    # comma-separated value is not a model identity and corrupts both usage
    # buckets and Slack.  The aggregate usage counters identify the primary
    # response model when one modelUsage entry matches them exactly.
    model_usage: dict[str, Any] = {}
    for source in (raw, usage):
        candidate = source.get("modelUsage") if isinstance(source, dict) else None
        if isinstance(candidate, dict):
            model_usage.update(candidate)
    if not model_usage:
        return None
    if len(model_usage) == 1:
        only_model = next(iter(model_usage))
        return only_model.strip() if isinstance(only_model, str) and only_model.strip() else None

    aggregate_input = usage.get("input_tokens") if isinstance(usage, dict) else None
    aggregate_output = usage.get("output_tokens") if isinstance(usage, dict) else None
    matches: list[str] = []
    for model_id, details in model_usage.items():
        if not isinstance(model_id, str) or not model_id.strip() or not isinstance(details, dict):
            continue
        model_input = details.get("inputTokens", details.get("input_tokens"))
        model_output = details.get("outputTokens", details.get("output_tokens"))
        if (
            isinstance(aggregate_input, int)
            and not isinstance(aggregate_input, bool)
            and isinstance(aggregate_output, int)
            and not isinstance(aggregate_output, bool)
            and model_input == aggregate_input
            and model_output == aggregate_output
        ):
            matches.append(model_id.strip())
    return matches[0] if len(matches) == 1 else None


def _exact_models_from_provider_response(raw: Any) -> list[dict[str, Any]]:
    """Extract only exact model identifiers confirmed by Claude's response."""
    if not isinstance(raw, dict):
        return []
    usage = raw.get("modelUsage") if isinstance(raw.get("modelUsage"), dict) else {}
    models: list[dict[str, Any]] = []
    for model_id, details in usage.items():
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        entry: dict[str, Any] = {"id": model_id.strip(), "evidence": "modelUsage"}
        if isinstance(details, dict):
            for source_key, target_key in (
                ("canonicalModel", "canonical_model"),
                ("provider", "provider"),
                ("contextWindow", "context_window"),
                ("maxOutputTokens", "max_output_tokens"),
            ):
                value = details.get(source_key)
                if value is not None:
                    entry[target_key] = value
        models.append(entry)

    direct_model = raw.get("model")
    if isinstance(direct_model, str) and direct_model.strip() and not any(
        item["id"] == direct_model.strip() for item in models
    ):
        models.append({"id": direct_model.strip(), "evidence": "provider_response.model"})
    return models


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
        self._last_probe_response: Optional[dict[str, Any]] = None

    def is_available(self) -> tuple[bool, str]:
        if not self._cli_path:
            return False, self._detect_note
        try:
            proc = subprocess.run(
                [self._cli_path, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                env=_cli_environment(),
            )
        except Exception as e:  # pragma: no cover - defensive
            return False, f"nepodařilo se spustit '{self._cli_path} --version': {e}"
        if proc.returncode != 0:
            return False, f"'{self._cli_path} --version' selhalo (kod {proc.returncode}): {proc.stderr.strip()}"
        return True, f"{proc.stdout.strip()} ({self._detect_note})"

    def probe_identity(self) -> dict[str, Any]:
        self._last_probe_response = None
        available, note = self.is_available()
        if not available:
            return {
                "available": False,
                "response": note,
                "model": None,
                "model_source": None,
                "probe_kind": "version_only",
                "full_response": {"available": False, "response": note},
            }

        assert self._cli_path
        project_path = Path.cwd()
        effective_model = self.config.model.strip()
        identity_probe = _identity_contract()
        command = [
            self._cli_path,
            "-p",
            identity_probe["prompt"],
            "--output-format",
            "json",
            "--permission-mode",
            identity_probe["permission_mode"],
        ]
        if effective_model:
            command += ["--model", effective_model]
        if self.config.max_budget_usd:
            command += ["--max-budget-usd", str(self.config.max_budget_usd)]
        try:
            proc = subprocess.run(
                command,
                cwd=str(project_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(60, max(15, self.config.timeout_seconds)),
                env=_cli_environment(),
            )
        except Exception as exc:
            return {
                "available": False,
                "response": f"Identifikační probe Claude Code selhal: {exc}",
                "model": None,
                "model_source": None,
                "probe_kind": "identity_request",
                "full_response": {"exception_type": type(exc).__name__, "exception": str(exc)},
            }

        try:
            raw = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            raw = None
        if not isinstance(raw, dict):
            return {
                "available": False,
                "response": proc.stdout.strip() or proc.stderr.strip(),
                "model": None,
                "model_source": None,
                "probe_kind": "identity_request",
                "full_response": {"stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode},
            }
        self._last_probe_response = raw
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        model = _reported_model(raw, usage)
        if not model and not bool(raw.get("is_error")):
            identity_paths = _identity_contract()["model_discovery"].get("identity_response_paths", [])
            model = model_from_paths([raw], identity_paths)
        return {
            "available": proc.returncode == 0 and not bool(raw.get("is_error")),
            "response": raw.get("result") or raw.get("error") or note,
            "model": model or (effective_model or None),
            "model_source": "reported" if model else ("configured" if effective_model else None),
            "probe_kind": "identity_request",
            "usage": usage,
            "full_response": raw,
        }

    def list_models(self) -> dict[str, Any]:
        """Return the provider's read-only picker hints without inventing a catalog.

        Claude Code has no documented account-catalog command.  Its printable
        ``/model`` command does, however, expose the model choices currently
        known by the CLI session.  The picker is therefore useful broker
        evidence, but it is deliberately kept ``UNKNOWN`` until Claude returns
        exact account model IDs through provider metadata.
        """
        if not self._cli_path:
            return {
                "state": "UNKNOWN",
                "source": "claude /model picker",
                "models": [],
                "picker_choices": [],
                "reason": "Claude CLI nebylo nalezeno; modely se nesmí domýšlet.",
                "full_response": {"catalog_command": None, "known_aliases": []},
            }

        contract = _identity_contract().get("model_catalog", {})
        picker = contract.get("picker", {}) if isinstance(contract, dict) else {}
        picker_prompt = picker.get("prompt", "/model") if isinstance(picker, dict) else "/model"
        picker_permission_mode = (
            picker.get("permission_mode", "plan") if isinstance(picker, dict) else "plan"
        )
        picker_command = [
            self._cli_path,
            "-p",
            picker_prompt,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--permission-mode",
            picker_permission_mode,
            "--tools",
            "",
        ]
        try:
            proc = subprocess.run(
                picker_command,
                cwd=str(Path.cwd()),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(90, max(30, self.config.timeout_seconds)),
                env=_cli_environment(),
            )
        except Exception as exc:
            picker_error = {
                "exception_type": type(exc).__name__,
                "exception": str(exc),
                "command": picker_command[1:],
            }
        else:
            picker_error = None

        raw = None
        picker_result = ""
        if picker_error is None:
            try:
                raw = json.loads(proc.stdout)
            except (json.JSONDecodeError, ValueError):
                raw = None
            if isinstance(raw, dict):
                picker_result = str(raw.get("result") or raw.get("error") or "")

        exact_models = _exact_models_from_provider_response(self._last_probe_response)

        available_match = re.search(
            r"Available:\s*(?P<items>.*?),\s*or\s+a\s+full\s+model\s+ID\.?\s*$",
            picker_result,
            re.IGNORECASE | re.DOTALL,
        )
        if available_match:
            aliases = [
                item.strip()
                for item in available_match.group("items").split(",")
                if item.strip()
            ]
            aliases = list(dict.fromkeys(aliases))
            current_match = re.search(
                r"Current model:\s*(?P<current>.+?)(?:\n|$)",
                picker_result,
                re.IGNORECASE,
            )
            return {
                "state": "PARTIAL" if exact_models else "UNKNOWN",
                "source": "claude -p /model",
                "models": exact_models,
                "picker_choices": aliases,
                "reason": (
                    "Claude providerová odpověď potvrdila přesná použitá modelová ID; "
                    "picker doplnil volby relace, nikoli úplný účetní katalog."
                    if exact_models
                    else "Claude picker vrátil volby relace, ne úplný účetní katalog přesných modelových ID."
                ),
                "full_response": {
                    "catalog_command": None,
                    "picker_command": picker_command[1:],
                    "known_aliases": aliases,
                    "exact_models_from_probe": exact_models,
                    "current_model_label": current_match.group("current").strip() if current_match else None,
                    "picker_result": picker_result,
                    "raw": raw,
                },
            }

        # A picker failure must not erase the last known evidence.  Fall back
        # to the CLI help, which is still useful for its explicitly documented
        # aliases but is not an account catalog either.
        try:
            help_proc = subprocess.run(
                [self._cli_path, "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=_cli_environment(),
            )
        except Exception as exc:
            return {
                "state": "UNKNOWN",
                "source": "claude /model picker",
                "models": [],
                "picker_choices": [],
                "reason": f"Claude model picker i --help selhaly: {exc}",
                "full_response": {"picker_error": picker_error, "help_error": str(exc)},
            }
        help_text = help_proc.stdout or help_proc.stderr
        model_help = re.search(
            r"--model\s+<model>.*?(?=\n\s*-[a-z]|\n\s*--|\Z)",
            help_text,
            re.IGNORECASE | re.DOTALL,
        )
        model_section = model_help.group(0) if model_help else ""
        aliases = re.findall(r"'([a-z][a-z0-9]*(?:\[1m\])?)'", model_section, re.IGNORECASE)
        aliases = list(dict.fromkeys(aliases))
        return {
            "state": "PARTIAL" if exact_models else "UNKNOWN",
            "source": "claude --help",
            "models": exact_models,
            "picker_choices": aliases,
            "reason": (
                "Claude providerová odpověď potvrdila přesná použitá modelová ID; "
                "picker nebyl dostupný."
                if exact_models
                else "Claude model picker neposkytl seznam; help pouze potvrzuje aliasy, modely se nesmí domýšlet."
            ),
            "full_response": {
                "catalog_command": None,
                "picker_command": picker_command[1:],
                "picker_error": picker_error,
                "picker_result": picker_result,
                "exact_models_from_probe": exact_models,
                "known_aliases": aliases,
                "model_option_help": model_section,
                "returncode": help_proc.returncode,
            },
        }

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

    @notify_provider_run("claude-code")
    @record_provider_run("claude-code")
    @with_provider_status("claude-code")
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
            proc = _run_claude_command(
                cmd,
                cwd=str(request.project_path),
                timeout=self.config.timeout_seconds,
                env=_cli_environment(),
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
