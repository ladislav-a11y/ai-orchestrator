import io
import json
import urllib.error
from pathlib import Path

from poc.hermes_agent.adapter import (
    FakeLocalTransport,
    HermesAgent,
    HermesRunRequest,
    OllamaCliTransport,
    OpenCodeFreeTransport,
)


def test_fake_local_transport_succeeds():
    agent = HermesAgent(transport=FakeLocalTransport())
    available, _ = agent.is_available()
    assert available is True

    result = agent.run(HermesRunRequest(project_path=Path("."), prompt="hello"))
    assert result.success is True
    assert result.limited is False
    assert result.cost_usd == 0.0
    assert result.latency_seconds is not None and result.latency_seconds >= 0
    assert "prompt_len=5" in result.output_text


def test_fake_local_transport_becomes_limited_after_n_calls():
    agent = HermesAgent(transport=FakeLocalTransport(fail_after=1))
    request = HermesRunRequest(project_path=Path("."), prompt="hi")

    first = agent.run(request)
    assert first.success is True
    assert first.limited is False

    second = agent.run(request)
    assert second.success is False
    assert second.limited is True
    assert second.retry_after_seconds == 1.0


def test_agent_never_raises_when_transport_raises():
    class ExplodingTransport:
        def is_available(self):
            raise RuntimeError("boom")

        def invoke(self, prompt, cwd):
            raise RuntimeError("boom")

    agent = HermesAgent(transport=ExplodingTransport())
    available, message = agent.is_available()
    assert available is False
    assert "boom" in message

    result = agent.run(HermesRunRequest(project_path=Path("."), prompt="x"))
    assert result.success is False
    assert result.error is not None and "boom" in result.error


def test_agent_rejects_destructive_prompt_before_transport(tmp_path):
    class RecordingTransport:
        called = False

        def invoke(self, prompt, cwd):
            self.called = True
            return {"success": True, "output": "unsafe"}

    transport = RecordingTransport()
    agent = HermesAgent(transport=transport, workspace_root=tmp_path)
    result = agent.run(
        HermesRunRequest(project_path=tmp_path, prompt="Please run git reset --hard")
    )

    assert result.success is False
    assert result.error is not None and "security boundary" in result.error
    assert transport.called is False


def test_agent_rejects_project_outside_workspace_before_transport(tmp_path):
    class RecordingTransport:
        called = False

        def invoke(self, prompt, cwd):
            self.called = True
            return {"success": True, "output": "unsafe"}

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    transport = RecordingTransport()
    agent = HermesAgent(transport=transport, workspace_root=workspace)
    result = agent.run(HermesRunRequest(project_path=outside, prompt="hello"))

    assert result.success is False
    assert result.error is not None and "outside workspace" in result.error
    assert transport.called is False


def test_ollama_transport_reports_unavailable_without_binary(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    transport = OllamaCliTransport()
    available, message = transport.is_available()
    assert available is False
    assert "not found" in message

    raw = transport.invoke("hi", Path("."))
    assert raw["success"] is False
    assert raw["error"] is not None


class _FakeHttpResponse:
    """Minimal stand-in for the object `urllib.request.urlopen` returns,
    used as a context manager - just enough surface for OpenCodeFreeTransport
    (`.status`, `.read()`)."""

    def __init__(self, status: int, body_bytes: bytes):
        self.status = status
        self._body = body_bytes

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_opencode_free_transport_is_available_when_reachable(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _FakeHttpResponse(200, b'{"object":"list","data":[]}'),
    )
    transport = OpenCodeFreeTransport()
    available, message = transport.is_available()
    assert available is True
    assert "reachable" in message


def test_opencode_free_transport_is_available_false_when_unreachable(monkeypatch):
    def raise_unreachable(request, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", raise_unreachable)
    transport = OpenCodeFreeTransport()
    available, message = transport.is_available()
    assert available is False
    assert "unreachable" in message


def test_opencode_free_transport_invoke_success_matches_real_observed_shape(monkeypatch):
    # Same JSON shape captured from a real, live, $0 call made while
    # evaluating this provider (see README.md "Zivé ověření bezplatného
    # provideru") - mocked here so the default test suite stays fully
    # offline/deterministic, never dependent on real network access.
    real_shaped_body = json.dumps(
        {
            "id": "gen-1787949271-IAKVXLXgJXL7edLTPNPU",
            "object": "chat.completion",
            "model": "laguna-s-2.1-free",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "pong"},
                }
            ],
            "usage": {"prompt_tokens": 51, "completion_tokens": 3, "total_tokens": 54},
            "cost": "0",
        }
    ).encode("utf-8")

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _FakeHttpResponse(200, real_shaped_body),
    )
    transport = OpenCodeFreeTransport()
    raw = transport.invoke("Reply with exactly one word: pong", Path("."))

    assert raw["success"] is True
    assert raw["output"] == "pong"
    assert raw["cost_usd"] == 0.0
    assert raw["usage"]["total_tokens"] == 54


def test_opencode_free_transport_invoke_reports_limited_on_429(monkeypatch):
    def raise_rate_limited(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests", hdrs=None, fp=io.BytesIO(b"rate limited")
        )

    monkeypatch.setattr("urllib.request.urlopen", raise_rate_limited)
    transport = OpenCodeFreeTransport()
    raw = transport.invoke("hi", Path("."))

    assert raw["success"] is False
    assert raw["limited"] is True


def test_opencode_free_transport_invoke_handles_network_error(monkeypatch):
    def raise_network_error(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", raise_network_error)
    transport = OpenCodeFreeTransport()
    raw = transport.invoke("hi", Path("."))

    assert raw["success"] is False
    assert raw["limited"] is False
    assert raw["cost_usd"] is None


def test_hermes_agent_run_via_opencode_free_transport_mocked(monkeypatch):
    real_shaped_body = json.dumps(
        {
            "choices": [{"message": {"content": "pong"}}],
            "usage": {"total_tokens": 54},
            "cost": "0",
        }
    ).encode("utf-8")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _FakeHttpResponse(200, real_shaped_body),
    )

    agent = HermesAgent(transport=OpenCodeFreeTransport())
    result = agent.run(HermesRunRequest(project_path=Path("."), prompt="Reply with exactly one word: pong"))

    assert result.success is True
    assert result.output_text == "pong"
    assert result.cost_usd == 0.0
    assert result.limited is False
