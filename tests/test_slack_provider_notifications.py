from __future__ import annotations

from datetime import datetime
from pathlib import Path

from orchestrator.agents.slack_provider_notifications import (
    format_messages,
    notify_provider_result,
)


class Result:
    success = True
    limited = False
    token_budget_exceeded = False
    unavailable = False


def test_message_uses_provider_result_and_keeps_full_llm_name():
    result = Result()
    result.model = "openai/gpt-oss-120b"
    result.input_tokens = 11
    result.output_tokens = None
    result.thinking_tokens = 2
    result.total_tokens = 13
    result.cost_usd = None
    message = format_messages(
        "groq",
        result,
        [{
            "model": result.model,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "thinking_tokens": result.thinking_tokens,
            "total_tokens": result.total_tokens,
            "cost_usd": result.cost_usd,
        }],
        now=datetime.fromisoformat("2026-09-09T10:20:30+02:00"),
    )[0]
    assert "[2026-09-09T10:20:30+02:00]" in message
    assert "LLM: `openai/gpt-oss-120b`" in message
    assert "input 11" in message
    assert "output 0" in message
    assert "thinking 2" in message
    assert "celkem 13" in message
    assert "cena: 0 USD" in message


def test_slack_json_ok_is_required_even_when_request_returns(tmp_path, monkeypatch):
    token_path = tmp_path / "slack_bot_token.txt"
    token_path.write_text("xoxb-test", encoding="utf-8")
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": false, "error": "channel_not_found"}'

    called = {}

    def fake_urlopen(request, timeout):
        called["request"] = request
        called["timeout"] = timeout
        return Response()

    monkeypatch.setattr(
        "orchestrator.agents.slack_provider_notifications.urlopen", fake_urlopen
    )
    assert not notify_provider_result(
        "codex", Result(), token_path=token_path
    )
    assert called["request"].full_url.endswith("chat.postMessage")
    assert called["timeout"] == 3.0
