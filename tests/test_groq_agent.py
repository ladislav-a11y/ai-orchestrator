import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import orchestrator.agents.groq as groq_module
from orchestrator.agents.base import AgentRunRequest
from orchestrator.agents.groq import GroqAgent, _execute_tool
from orchestrator.config import DEFAULT_PROVIDER_ORDER, GROQ_FREE_MODEL, GroqAgentConfig, load_config


class FakeMessage:
    def __init__(self, content="", tool_calls=None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self, exclude_none=True):
        data = {"role": self.role, "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in self.tool_calls
            ]
        return data


class FakeResponse:
    def __init__(self, message, *, model=GROQ_FREE_MODEL, prompt=10, completion=4, reasoning=1):
        self.model = model
        self.choices = [SimpleNamespace(message=message)]
        self.usage = SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
        )

    def model_dump(self, exclude_none=True):
        return {"model": self.model, "choices": [{"message": self.choices[0].message.model_dump()}]}


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **kwargs):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def tool_call(name, arguments, call_id="call-1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def make_agent(monkeypatch, responses, **overrides):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    values = {"model": GROQ_FREE_MODEL, "free_only": True}
    values.update(overrides)
    agent = GroqAgent(GroqAgentConfig(**values))
    client = FakeClient(responses)
    agent._client = client
    monkeypatch.setattr(groq_module, "Groq", object)
    return agent, client


def test_default_provider_order_prefers_groq_then_antigravity_claude_codex():
    assert DEFAULT_PROVIDER_ORDER == ["groq", "antigravity", "claude-code", "codex"]


def test_config_defaults_to_strict_free_groq(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("{}\n", encoding="utf-8")
    config = load_config(path, create_if_missing=False)

    assert config.groq.model == GROQ_FREE_MODEL
    assert config.groq.free_only is True
    assert config.groq.max_budget_usd == 0.0
    assert config.provider_order == ["groq", "antigravity", "claude-code", "codex"]


def test_free_only_rejects_non_free_configured_model():
    with pytest.raises(ValueError, match="free_only"):
        GroqAgent(GroqAgentConfig(model="paid-or-unqualified-model", free_only=True))


def test_missing_api_key_is_unavailable(monkeypatch):
    monkeypatch.setattr(groq_module, "Groq", object)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    agent = GroqAgent(GroqAgentConfig())

    ok, message = agent.is_available()

    assert ok is False
    assert "GROQ_API_KEY" in message


def test_requested_model_override_is_rejected_in_free_only_mode(monkeypatch, tmp_path):
    agent, _ = make_agent(monkeypatch, [])

    result = agent.run(
        AgentRunRequest(tmp_path, "do work", requested_model="another-model")
    )

    assert result.success is False
    assert "free-only" in result.error


def test_tool_loop_edits_project_and_returns_structured_final_response(monkeypatch, tmp_path):
    first = FakeResponse(
        FakeMessage(
            tool_calls=[tool_call("write_file", {"path": "hello.txt", "content": "hello\r\nworld\r\n"})]
        ),
        prompt=10,
        completion=5,
        reasoning=2,
    )
    second = FakeResponse(FakeMessage(content="implementation complete"), prompt=20, completion=6, reasoning=1)
    final = FakeResponse(
        FakeMessage(content='{"items":[{"index":0,"done":true}],"notes":"done"}'),
        prompt=30,
        completion=7,
        reasoning=1,
    )
    agent, client = make_agent(monkeypatch, [first, second, final])
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "done": {"type": "boolean"},
                    },
                    "required": ["index", "done"],
                    "additionalProperties": False,
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["items", "notes"],
        "additionalProperties": False,
    }

    result = agent.run(AgentRunRequest(tmp_path, "create hello.txt", output_schema=schema))

    assert result.success is True
    assert result.output_text == '{"items":[{"index":0,"done":true}],"notes":"done"}'
    assert (tmp_path / "hello.txt").read_bytes() == b"hello\nworld\n"
    assert result.model == GROQ_FREE_MODEL
    assert result.model_source == "reported"
    assert result.input_tokens == 60
    assert result.output_tokens == 18
    assert result.thinking_tokens == 4
    assert result.total_tokens == 78
    assert result.session_id
    assert all(call["service_tier"] == "on_demand" for call in client.calls)
    assert all(call["reasoning_effort"] == "low" for call in client.calls)
    assert "tools" in client.calls[0]
    assert "tools" in client.calls[1]
    assert "tools" not in client.calls[2]
    assert client.calls[2]["response_format"]["json_schema"]["strict"] is True
    assert client.calls[2]["response_format"]["json_schema"]["schema"] == schema




def test_structured_finalization_prompt_includes_array_max_items_constraint(
    monkeypatch, tmp_path
):
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "done": {"type": "boolean"},
                    },
                    "required": ["index", "done"],
                    "additionalProperties": False,
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["items", "notes"],
        "additionalProperties": False,
    }
    implementation = FakeResponse(FakeMessage(content="implementation complete"))
    final = FakeResponse(
        FakeMessage(content='{"items":[{"index":0,"done":true}],"notes":"done"}')
    )
    agent, client = make_agent(monkeypatch, [implementation, final])

    result = agent.run(
        AgentRunRequest(tmp_path, "update README", output_schema=schema)
    )

    assert result.success is True
    assert len(client.calls) == 2
    final_messages = client.calls[1]["messages"]
    final_prompt = next(
        message["content"]
        for message in reversed(final_messages)
        if message.get("role") == "user"
    )
    assert "Follow the JSON schema exactly." in final_prompt
    assert "Array `items` must contain at most 1 item(s)." in final_prompt
    assert client.calls[1]["response_format"]["json_schema"]["schema"] == schema


def test_schema_shaped_unknown_tool_call_falls_back_to_structured_finalization(
    monkeypatch, tmp_path
):
    class FakeToolUseError(Exception):
        status_code = 400

        def __init__(self):
            super().__init__("tool use failed")
            self.body = {
                "error": {
                    "code": "tool_use_failed",
                    "failed_generation": json.dumps(
                        {
                            "name": "commentary",
                            "arguments": {
                                "items": [{"index": 0, "done": True}],
                                "notes": "README updated",
                            },
                        }
                    ),
                }
            }

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "done": {"type": "boolean"},
                    },
                    "required": ["index", "done"],
                    "additionalProperties": False,
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["items", "notes"],
        "additionalProperties": False,
    }
    first = FakeResponse(
        FakeMessage(
            tool_calls=[
                tool_call(
                    "write_file",
                    {"path": "README.md", "content": "updated\n"},
                )
            ]
        )
    )
    final = FakeResponse(
        FakeMessage(content='{"items":[{"index":0,"done":true}],"notes":"verified"}')
    )
    agent, client = make_agent(monkeypatch, [first, FakeToolUseError(), final])

    result = agent.run(
        AgentRunRequest(tmp_path, "update README", output_schema=schema)
    )

    assert result.success is True
    assert result.output_text == '{"items":[{"index":0,"done":true}],"notes":"verified"}'
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "updated\n"
    assert len(client.calls) == 3
    assert "tools" in client.calls[0]
    assert "tools" in client.calls[1]
    assert "tools" not in client.calls[2]
    assert client.calls[2]["response_format"]["json_schema"]["schema"] == schema


def test_session_context_is_reused_with_same_session_id(monkeypatch, tmp_path):
    agent, client = make_agent(
        monkeypatch,
        [FakeResponse(FakeMessage(content="first")), FakeResponse(FakeMessage(content="second"))],
    )
    first = agent.run(AgentRunRequest(tmp_path, "first task"))
    second = agent.run(AgentRunRequest(tmp_path, "follow-up", session_id=first.session_id))

    assert first.success and second.success
    second_messages = client.calls[1]["messages"]
    assert any(m.get("content") == "first task" for m in second_messages)
    assert any(m.get("content") == "first" for m in second_messages)
    assert second.session_id == first.session_id



def test_read_file_default_window_is_bounded_for_groq_context(tmp_path):
    path = tmp_path / "large.txt"
    path.write_text("".join(f"line-{i:04d} " + ("x" * 80) + "\n" for i in range(1, 401)), encoding="utf-8")

    result = json.loads(
        _execute_tool(tmp_path, "read_file", json.dumps({"path": "large.txt"}))
    )

    assert result["ok"] is True
    assert result["result"]["truncated"] is True
    assert result["result"]["total_lines"] == 400
    assert len(json.dumps(result, ensure_ascii=False)) <= groq_module._MAX_TOOL_RESULT_JSON


def test_large_search_result_is_compacted_as_valid_json(tmp_path):
    for i in range(80):
        (tmp_path / f"file-{i:03d}.txt").write_text(
            "needle " + ("payload " * 100) + "\n",
            encoding="utf-8",
        )

    raw = _execute_tool(
        tmp_path,
        "search_text",
        json.dumps({"query": "needle", "max_results": 80}),
    )
    result = json.loads(raw)

    assert result["ok"] is True
    assert result["result"]["truncated"] is True
    assert len(raw) <= groq_module._MAX_TOOL_RESULT_JSON


def test_read_file_accepts_line_start_and_line_end_aliases(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    result = json.loads(
        _execute_tool(
            tmp_path,
            "read_file",
            json.dumps({"path": "sample.txt", "line_start": 2, "line_end": 3}),
        )
    )

    assert result["ok"] is True
    assert result["result"]["text"] == "2: two\n3: three"
    assert result["result"]["total_lines"] == 4


def test_read_file_rejects_conflicting_line_window_aliases(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = json.loads(
        _execute_tool(
            tmp_path,
            "read_file",
            json.dumps(
                {
                    "path": "sample.txt",
                    "start_line": 1,
                    "max_lines": 2,
                    "line_start": 1,
                    "line_end": 2,
                }
            ),
        )
    )

    assert result["ok"] is False
    assert "either start_line/max_lines or line_start/line_end" in result["error"]


def test_path_escape_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    result = json.loads(
        _execute_tool(tmp_path, "read_file", json.dumps({"path": "../outside.txt"}))
    )

    assert result["ok"] is False
    assert "escapes project root" in result["error"]


def test_exact_replace_refuses_non_unique_old_block(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("same\nsame\n", encoding="utf-8")

    result = json.loads(
        _execute_tool(
            tmp_path,
            "replace_text",
            json.dumps({"path": "sample.txt", "old": "same", "new": "changed"}),
        )
    )

    assert result["ok"] is False
    assert "found 2" in result["error"]
    assert path.read_text(encoding="utf-8") == "same\nsame\n"



def test_malformed_tool_arguments_are_retried_with_corrective_prompt(monkeypatch, tmp_path):
    class FakeMalformedToolError(Exception):
        status_code = 400

        def __init__(self):
            super().__init__("invalid tool arguments")
            self.body = {
                "error": {
                    "code": "tool_use_failed",
                    "message": "Failed to parse tool call arguments as JSON",
                    "failed_generation": '{"name":"write_file","arguments":{broken}}',
                }
            }

    path = tmp_path / "README.md"
    path.write_text("before\n", encoding="utf-8")
    retry = FakeResponse(
        FakeMessage(
            tool_calls=[
                tool_call(
                    "replace_text",
                    {"path": "README.md", "old": "before\n", "new": "before\n\nafter\n"},
                )
            ]
        )
    )
    done = FakeResponse(FakeMessage(content="implementation complete"))
    agent, client = make_agent(monkeypatch, [FakeMalformedToolError(), retry, done])

    result = agent.run(AgentRunRequest(tmp_path, "append after"))

    assert result.success is True
    assert path.read_text(encoding="utf-8") == "before\n\nafter\n"
    assert len(client.calls) == 3
    retry_messages = client.calls[1]["messages"]
    correction = next(
        message["content"]
        for message in reversed(retry_messages)
        if message.get("role") == "user"
    )
    assert "rejected as invalid JSON" in correction
    assert "use replace_text" in correction
    assert "do not copy read_file line numbers" in correction


def test_rate_limit_maps_to_limited_and_retry_after(monkeypatch, tmp_path):
    class FakeRateLimit(Exception):
        status_code = 429

        def __init__(self):
            super().__init__("rate limit exceeded")
            self.response = SimpleNamespace(headers={"retry-after": "12.5"})

    monkeypatch.setattr(groq_module, "RateLimitError", FakeRateLimit)
    agent, _ = make_agent(monkeypatch, [FakeRateLimit()])

    result = agent.run(AgentRunRequest(tmp_path, "do work"))

    assert result.success is False
    assert result.limited is True
    assert result.unavailable is False
    assert result.retry_after_seconds == 12.5
    assert "LIMITED" in result.error


def test_authentication_error_is_unavailable_not_limited(monkeypatch, tmp_path):
    class FakeAuthError(Exception):
        pass

    monkeypatch.setattr(groq_module, "AuthenticationError", FakeAuthError)
    agent, _ = make_agent(monkeypatch, [FakeAuthError("bad key")])

    result = agent.run(AgentRunRequest(tmp_path, "do work"))

    assert result.success is False
    assert result.unavailable is True
    assert result.limited is False


def test_max_tool_rounds_fails_closed(monkeypatch, tmp_path):
    responses = [
        FakeResponse(FakeMessage(tool_calls=[tool_call("git_status", {}, call_id=f"call-{i}")]))
        for i in range(2)
    ]
    agent, _ = make_agent(monkeypatch, responses, max_tool_rounds=2)

    result = agent.run(AgentRunRequest(tmp_path, "keep inspecting"))

    assert result.success is False
    assert "max_tool_rounds=2" in result.error


def test_git_internals_are_forbidden(tmp_path):
    (tmp_path / ".git").mkdir()
    result = json.loads(
        _execute_tool(
            tmp_path,
            "write_file",
            json.dumps({"path": ".git/config", "content": "forbidden"}),
        )
    )

    assert result["ok"] is False
    assert ".git internals" in result["error"]
    assert not (tmp_path / ".git" / "config").exists()
