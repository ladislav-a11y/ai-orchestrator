"""Minimal, self-contained agent adapter used to evaluate a Hermes-style
local/free provider.

Mirrors the shape of `orchestrator/agents/base.py`'s `Agent` interface
(`is_available()` / `run()` returning a result with `success`/`limited`/
`cost_usd`/`retry_after_seconds`) so a real integration could be added later
by writing one new transport, without changing this adapter's contract. This
module never imports from `orchestrator.*` and is never imported by it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol


@dataclass
class HermesRunRequest:
    project_path: Path
    prompt: str
    context: Optional[str] = None
    session_id: Optional[str] = None


@dataclass
class HermesRunResult:
    success: bool
    output_text: str
    error: Optional[str] = None
    cost_usd: Optional[float] = None
    latency_seconds: Optional[float] = None
    limited: bool = False
    retry_after_seconds: Optional[float] = None
    raw: dict[str, Any] = field(default_factory=dict)


class HermesTransport(Protocol):
    """Pluggable transport a HermesAgent talks to. Swapping this is the only
    thing needed to move from an offline fake to a real local binary."""

    def is_available(self) -> tuple[bool, str]:
        ...

    def invoke(self, prompt: str, cwd: Path) -> dict[str, Any]:
        ...


class FakeLocalTransport:
    """Deterministic, fully offline stand-in for a local/free model process.

    Used because this sandboxed evaluation environment has no verified,
    network- or binary-accessible Hermes/Ollama installation (see
    README.md "Sandbox limitations"). It never touches the network or the
    filesystem outside what the caller explicitly passes in, so it is safe
    to run in any test or demo without further guarding.
    """

    name = "fake-local"

    def __init__(self, canned_response: str = "OK: fake local Hermes response", fail_after: Optional[int] = None):
        self.canned_response = canned_response
        self.fail_after = fail_after
        self.calls = 0

    def is_available(self) -> tuple[bool, str]:
        return True, "fake-local transport is always available (offline stub)"

    def invoke(self, prompt: str, cwd: Path) -> dict[str, Any]:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            return {
                "success": False,
                "output": "",
                "error": "fake-local: simulated quota exhausted",
                "limited": True,
                "retry_after_seconds": 1.0,
                "cost_usd": 0.0,
            }
        return {
            "success": True,
            "output": f"{self.canned_response} (prompt_len={len(prompt)})",
            "error": None,
            "limited": False,
            "retry_after_seconds": None,
            "cost_usd": 0.0,
        }


class OllamaCliTransport:
    """Documented, opt-in transport for a real local/free provider via the
    `ollama` CLI (https://ollama.com). NOT exercised by default anywhere in
    this PoC or its tests - `is_available()` only checks whether the `ollama`
    binary is on PATH (read-only `shutil.which`, never spawns a process), and
    `invoke()` is only ever called if a caller explicitly constructs this
    class and its own `is_available()` returned True. This is the extension
    point a real verification pass (run outside this sandbox, with `ollama`
    actually installed) would use instead of `FakeLocalTransport`.
    """

    name = "ollama-cli"

    def __init__(self, model: str = "llama3.2", timeout_seconds: float = 120.0):
        self.model = model
        self.timeout_seconds = timeout_seconds

    def is_available(self) -> tuple[bool, str]:
        path = shutil.which("ollama")
        if not path:
            return False, "ollama CLI not found on PATH"
        return True, f"ollama CLI found at {path}"

    def invoke(self, prompt: str, cwd: Path) -> dict[str, Any]:
        available, message = self.is_available()
        if not available:
            return {
                "success": False,
                "output": "",
                "error": message,
                "limited": False,
                "retry_after_seconds": None,
                "cost_usd": 0.0,
            }
        try:
            completed = subprocess.run(
                ["ollama", "run", self.model, prompt],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {
                "success": False,
                "output": "",
                "error": f"ollama invocation failed: {exc}",
                "limited": False,
                "retry_after_seconds": None,
                "cost_usd": 0.0,
            }
        return {
            "success": completed.returncode == 0,
            "output": completed.stdout,
            "error": completed.stderr if completed.returncode != 0 else None,
            "limited": False,
            "retry_after_seconds": None,
            "cost_usd": 0.0,
        }


class OpenCodeFreeTransport:
    """Real, keyless, $0 free-tier transport - not a Hermes fork or a guess,
    but the exact same built-in provider the real, locally-installed Hermes
    Agent (Nous Research, `C:\\Users\\Admin\\AppData\\Local\\hermes\\`) ships as
    `plugins/model-providers/opencode-free/__init__.py`: "OpenCode's free
    model tier on the Zen relay (https://opencode.ai/zen/v1). KEYLESS: the
    relay serves free-tier models anonymously ... No OpenCode account
    needed." This class re-implements just enough of that provider's wire
    contract (same base URL, same keyless headers, same default free model
    `laguna-s-2.1-free`) to call it directly over stdlib `urllib` - no new
    dependency, and no touching the real Hermes Agent's own config/account
    at all.

    Verified live from this sandbox (see README.md "Živé ověření
    bezplatného provideru"): a real POST to `/chat/completions` with prompt
    "Reply with exactly one word: pong" returned HTTP 200, content "pong",
    `usage.total_tokens=54`, and `"cost":"0"` (the API's own, not an
    estimate) in ~1.9s. Never called automatically by `e2e_smoke.py` or the
    default test suite (both must stay offline/deterministic) - only via
    `live_free_provider_smoke.py`, run explicitly, and the opt-in
    `AI_ORCHESTRATOR_RUN_LIVE_HERMES_FREE_TEST=1`-gated test.
    """

    name = "opencode-free"
    BASE_URL = "https://opencode.ai/zen/v1"

    def __init__(self, model: str = "laguna-s-2.1-free", timeout_seconds: float = 30.0, max_tokens: int = 64):
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens

    def _headers(self) -> dict[str, str]:
        # Same keyless header shape as the real plugin - an empty
        # Authorization value, never a placeholder Bearer token, is what
        # keeps the relay's anonymous free tier from 401-ing the request.
        return {
            "Content-Type": "application/json",
            "Authorization": "",
            "HTTP-Referer": "https://hermes-agent.nousresearch.com",
            "X-Title": "Hermes Agent",
            "User-Agent": "hermes-agent-poc/1.0 (ai-orchestrator isolated PoC)",
        }

    def is_available(self) -> tuple[bool, str]:
        req = urllib.request.Request(f"{self.BASE_URL}/models", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if resp.status == 200:
                    return True, "opencode.ai/zen free tier reachable (HTTP 200 on /models)"
                return False, f"unexpected HTTP status {resp.status} from /models"
        except (urllib.error.URLError, OSError) as exc:
            return False, f"opencode.ai/zen unreachable: {exc}"

    def invoke(self, prompt: str, cwd: Path) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
        }
        request = urllib.request.Request(
            f"{self.BASE_URL}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            return {
                "success": False,
                "output": "",
                "error": f"HTTP {exc.code}: {detail}",
                "limited": exc.code in (429, 503),
                "retry_after_seconds": None,
                "cost_usd": None,
            }
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            return {
                "success": False,
                "output": "",
                "error": f"opencode-free invocation failed: {exc}",
                "limited": False,
                "retry_after_seconds": None,
                "cost_usd": None,
            }

        choices = body.get("choices") or []
        content = choices[0]["message"]["content"] if choices else ""
        raw_cost = body.get("cost")
        try:
            cost_usd = float(raw_cost) if raw_cost is not None else 0.0
        except (TypeError, ValueError):
            cost_usd = None
        return {
            "success": bool(content),
            "output": content,
            "error": None if content else "empty completion content",
            "limited": False,
            "retry_after_seconds": None,
            "cost_usd": cost_usd,
            "usage": body.get("usage"),
        }


class HermesAgent:
    """Self-contained evaluation adapter. `name` mirrors the production
    `Agent.name` convention purely for readability in reports/logs."""

    name = "hermes-agent-poc"

    def __init__(self, transport: HermesTransport, workspace_root: Path | None = None):
        self.transport = transport
        self.workspace_root = (workspace_root or Path.cwd()).resolve()

    def is_available(self) -> tuple[bool, str]:
        try:
            return self.transport.is_available()
        except Exception as exc:  # never raise, matches base.Agent contract
            return False, f"transport raised during is_available(): {exc}"

    def run(self, request: HermesRunRequest) -> HermesRunResult:
        start = time.monotonic()
        # This is an enforced boundary, not merely a standalone classifier:
        # unsafe input is rejected before any provider/CLI receives it.
        from poc.hermes_agent.security import (
            SecurityBoundaryError,
            assert_prompt_is_safe,
            ensure_within_workspace,
        )

        try:
            project_path = ensure_within_workspace(
                request.project_path, self.workspace_root
            )
            assert_prompt_is_safe(request.prompt)
        except SecurityBoundaryError as exc:
            return HermesRunResult(
                success=False,
                output_text="",
                error=f"security boundary rejected request: {exc}",
                latency_seconds=time.monotonic() - start,
            )
        try:
            raw = self.transport.invoke(request.prompt, project_path)
        except Exception as exc:  # never raise, matches base.Agent contract
            return HermesRunResult(
                success=False,
                output_text="",
                error=f"transport raised: {exc}",
                latency_seconds=time.monotonic() - start,
            )
        latency = time.monotonic() - start
        return HermesRunResult(
            success=bool(raw.get("success")),
            output_text=raw.get("output", ""),
            error=raw.get("error"),
            cost_usd=raw.get("cost_usd"),
            latency_seconds=latency,
            limited=bool(raw.get("limited", False)),
            retry_after_seconds=raw.get("retry_after_seconds"),
            raw=raw,
        )
