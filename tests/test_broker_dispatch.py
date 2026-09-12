import logging
import json
from pathlib import Path

from orchestrator.agents.base import Agent, AgentRunRequest, AgentRunResult, with_provider_status
from orchestrator.broker_dispatch import BrokerBackedAgent
from orchestrator.provider_broker import ProviderBroker


LANG = {
    "provider": "claude-code",
    "task_execution_receipt": {
        "prompt_wrapper": "Return exactly one JSON object with answer and model.",
        "response_format": {"required_fields": ["answer", "model"]},
    },
}


class FakeProvider(Agent):
    name = "claude-code"

    def __init__(self):
        self.last_request = None

    def is_available(self):
        return True, "ok"

    def run(self, request):
        self.last_request = request
        return AgentRunResult(success=True, output_text='{"answer":"ok","model":"claude-opus-4-6"}')


class FakeBroker:
    def __init__(self, provider):
        self.providers = {"claude-code": provider}
        self.ask_calls = []
        self.report_calls = []

    def ask(self, query):
        self.ask_calls.append(query)
        return {
            "offer": {
                "provider": "claude-code",
                "model": "claude-opus-4-6",
                "state": "AVAILABLE",
                "lang": LANG,
            }
        }

    def report_provider_result(self, provider, result):
        self.report_calls.append((provider, result))


class SequencedBroker:
    def __init__(self, providers):
        self.providers = {provider.name: provider for provider in providers}
        self.ask_calls = []
        self._offers = iter(providers)

    def ask(self, query):
        self.ask_calls.append(query)
        provider = next(self._offers)
        return {
            "offer": {
                "provider": provider.name,
                "model": f"{provider.name}-model",
                "state": "AVAILABLE",
                "lang": LANG,
            }
        }


class FailingProvider(Agent):
    def __init__(self, name, success=False):
        self.name = name
        self.success = success
        self.run_calls = []

    def is_available(self):
        return True, "ok"

    def run(self, request):
        self.run_calls.append(request)
        if self.success:
            return AgentRunResult(
                success=True,
                output_text="ok",
                model=f"{self.name}-actual-model",
            )
        return AgentRunResult(success=False, output_text="", error=f"{self.name} failed")


def test_broker_backed_agent_uses_offer_and_logs_dispatch_provenance(caplog):
    provider = FakeProvider()
    broker = FakeBroker(provider)
    agent = BrokerBackedAgent(broker)

    with caplog.at_level(logging.INFO, logger="orchestrator.broker_dispatch"):
        result = agent.run(
            AgentRunRequest(
                project_path=Path("."),
                prompt="Udělej úkol.",
                caller="orchestrator.runner.run_task",
                source="api",
                selection_reason="explicit_provider",
            )
        )

    assert result.success is True
    assert broker.ask_calls[0]["command"] == "select_provider"
    assert broker.ask_calls[0]["task_profile"]["model_tier"] == "fast"
    assert broker.report_calls == []
    assert provider.last_request.requested_model == "claude-opus-4-6"
    assert provider.last_request.caller == "orchestrator.runner.run_task"
    assert provider.last_request.source == "api"
    messages = "\n".join(caplog.messages)
    assert "caller=orchestrator.runner.run_task" in messages
    assert "source=api" in messages
    assert "reason=explicit_provider" in messages
    assert "provider=claude-code" in messages
    assert "model=claude-opus-4-6" in messages
    assert "Return exactly one JSON object" in provider.last_request.prompt
    assert provider.last_request.output_schema == {
        "type": "object",
        "properties": {"answer": {"type": "string"}, "model": {"type": "string"}},
        "required": ["answer", "model"],
        "additionalProperties": False,
    }


def test_broker_backed_agent_passes_prepared_json_task_to_provider():
    """The AO dispatch contract forwards the prepared task as-is.

    Broker instructions and the provider receipt are separate metadata; they
    must not turn a machine-ready task into a second prose prompt.
    """
    provider = FakeProvider()
    broker = FakeBroker(provider)
    agent = BrokerBackedAgent(broker)
    prepared_task = json.dumps(
        {
            "task": "Vypiš přesně jedno slovo: OK.",
            "scope": "provider contract smoke test",
            "dod": ["Odpověď obsahuje přesně OK."],
            "constraints": ["Neměň soubory.", "Nepoužívej nástroje."],
            "verification": {"required": [], "acceptable": ["text"]},
            "dependencies": [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    task_schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }

    result = agent.run(
        AgentRunRequest(
            project_path=Path("."),
            prompt=prepared_task,
            output_schema=task_schema,
            caller="orchestrator.runner.run_task",
            source="inbox",
        )
    )

    assert result.success is True
    assert provider.last_request.prompt == prepared_task
    assert provider.last_request.output_schema == task_schema
    assert provider.last_request.receipt_prompt is None


def test_broker_backed_agent_keeps_caller_schema_free_of_receipt_wrapper():
    provider = FakeProvider()
    broker = FakeBroker(provider)
    agent = BrokerBackedAgent(broker)
    planner_schema = {
        "type": "object",
        "properties": {"tasks": {"type": "array"}},
        "required": ["tasks"],
        "additionalProperties": False,
    }

    result = agent.run(
        AgentRunRequest(
            project_path=Path("."),
            prompt="Naplánuj Inbox.",
            output_schema=planner_schema,
        )
    )

    assert result.success is True
    assert provider.last_request.output_schema == planner_schema
    assert "Return exactly one JSON object with answer and model." not in provider.last_request.prompt
    assert provider.last_request.receipt_prompt is None


def test_broker_backed_agent_renders_lang_required_schema_in_prompt():
    provider = FakeProvider()

    class SchemaBroker(FakeBroker):
        def ask(self, query):
            response = super().ask(query)
            response["offer"]["lang"]["input_from_ao"] = {
                "fields": {
                    "output_schema": "Požadavek na strukturu musí být součástí promptu."
                }
            }
            return response

    broker = SchemaBroker(provider)
    agent = BrokerBackedAgent(broker)
    schema = {
        "type": "object",
        "properties": {"tasks": {"type": "array"}},
        "required": ["tasks"],
        "additionalProperties": False,
    }

    result = agent.run(
        AgentRunRequest(project_path=Path("."), prompt="Naplánuj Inbox.", output_schema=schema)
    )

    assert result.success is True
    assert json.dumps(schema, ensure_ascii=False) in provider.last_request.prompt


class StatusProvider(Agent):
    def __init__(self, name):
        self.name = name

    def is_available(self):
        return True, "ok"

    @with_provider_status("groq")
    def run(self, request):
        return AgentRunResult(
            success=False,
            output_text="",
            error="TPM limit",
            limited=True,
            retry_after_seconds=15,
        )


def test_provider_status_sink_updates_broker_note_without_ao_report(tmp_path):
    providers = [StatusProvider(name) for name in ("groq", "antigravity", "claude-code", "codex")]
    broker = ProviderBroker(providers, info_dir=tmp_path / "info", lang_dir=tmp_path / "lang")

    result = broker.providers["groq"].run(
        AgentRunRequest(project_path=Path("."), prompt="limit test")
    )

    assert result.provider_status["state"] == "LIMITED"
    raw = (tmp_path / "info" / "groqinfo.json").read_text(encoding="utf-8")
    assert '"state": "LIMITED"' in raw
    assert "TPM limit" in raw


def test_broker_backed_agent_reasks_broker_after_opted_in_provider_failure():
    first = FailingProvider("groq")
    second = FailingProvider("antigravity", success=True)
    broker = SequencedBroker([first, second])
    agent = BrokerBackedAgent(broker)

    result = agent.run(
        AgentRunRequest(
            project_path=Path("."),
            prompt="retry test",
            failover_on_error=True,
        )
    )

    assert result.success is True
    assert len(first.run_calls) == 1
    assert len(second.run_calls) == 1
    assert broker.ask_calls[0]["command"] == "select_provider"
    assert broker.ask_calls[1]["command"] == "select_provider"
    assert broker.ask_calls[1]["exclude_providers"] == ["groq"]
    assert broker.ask_calls[0]["task_profile"] == broker.ask_calls[1]["task_profile"]
    assert result.selection_reason == "failover: groq -> antigravity"
    assert [event["provider"] for event in result.usage_events] == [
        "groq",
        "antigravity",
    ]


def test_broker_backed_agent_reasks_broker_after_provider_error_without_opt_in():
    first = FailingProvider("groq")
    second = FailingProvider("antigravity", success=True)
    broker = SequencedBroker([first, second])
    agent = BrokerBackedAgent(broker)

    result = agent.run(
        AgentRunRequest(
            project_path=Path("."),
            prompt="automatic error failover",
        )
    )

    assert result.success is True
    assert len(first.run_calls) == 1
    assert len(second.run_calls) == 1
    assert broker.ask_calls[1]["exclude_providers"] == ["groq"]
    assert result.selection_reason == "failover: groq -> antigravity"


def test_protocol_failover_asks_broker_before_next_autonomous_call():
    first = FailingProvider("groq", success=True)
    second = FailingProvider("antigravity", success=True)

    class ExcludingBroker:
        def __init__(self):
            self.providers = {first.name: first, second.name: second}
            self.ask_calls = []

        def ask(self, query):
            self.ask_calls.append(query)
            excluded = set(query.get("exclude_providers") or [])
            for provider in self.providers.values():
                if provider.name not in excluded:
                    return {
                        "offer": {
                            "provider": provider.name,
                            "model": f"{provider.name}-model",
                            "state": "AVAILABLE",
                            "lang": LANG,
                        }
                    }
            return {"offer": {"state": "NONE_AVAILABLE", "reason": "none"}}

    broker = ExcludingBroker()
    agent = BrokerBackedAgent(broker)
    agent.active_provider_name = "groq"

    assert agent.force_failover_on_protocol_error("invalid provider JSON") is True
    result = agent.run(AgentRunRequest(project_path=Path("."), prompt="continue"))

    assert result.success is True
    assert len(first.run_calls) == 0
    assert len(second.run_calls) == 1
    assert broker.ask_calls[0]["exclude_providers"] == ["groq"]
    assert broker.ask_calls[1]["exclude_providers"] == ["groq"]


def test_broker_backed_agent_stops_after_five_failed_provider_attempts():
    providers = [
        FailingProvider("groq"),
        FailingProvider("antigravity"),
        FailingProvider("claude-code"),
        FailingProvider("codex"),
        FailingProvider("provider-5"),
    ]
    broker = SequencedBroker(providers)
    agent = BrokerBackedAgent(broker)

    result = agent.run(
        AgentRunRequest(
            project_path=Path("."),
            prompt="five attempts",
            failover_on_error=True,
        )
    )

    assert result.success is False
    assert len(broker.ask_calls) == 5
    assert all(len(provider.run_calls) == 1 for provider in providers)
    assert result.selection_reason == (
        "failover: groq -> antigravity -> claude-code -> codex -> provider-5"
    )


def test_broker_backed_agent_preserves_earliest_retry_when_no_offer_remains():
    class LimitedProvider(FailingProvider):
        def __init__(self, name, retry_after_seconds):
            super().__init__(name)
            self.retry_after_seconds = retry_after_seconds

        def run(self, request):
            self.run_calls.append(request)
            return AgentRunResult(
                success=False,
                output_text="",
                error=f"{self.name} limited",
                limited=True,
                retry_after_seconds=self.retry_after_seconds,
            )

    class ExhaustedBroker:
        def __init__(self, providers):
            self.providers = {provider.name: provider for provider in providers}
            self.ask_calls = []

        def ask(self, query):
            self.ask_calls.append(query)
            excluded = set(query.get("exclude_providers") or [])
            for provider in self.providers.values():
                if provider.name not in excluded:
                    return {
                        "offer": {
                            "provider": provider.name,
                            "model": f"{provider.name}-model",
                            "state": "AVAILABLE",
                            "lang": LANG,
                        }
                    }
            return {
                "offer": {
                    "state": "NONE_AVAILABLE",
                    "reason": "no provider remains",
                }
            }

    providers = [
        LimitedProvider("groq", 120),
        LimitedProvider("codex", 45),
    ]
    agent = BrokerBackedAgent(ExhaustedBroker(providers))

    result = agent.run(
        AgentRunRequest(
            project_path=Path("."),
            prompt="retry evidence",
            failover_on_error=True,
        )
    )

    assert result.success is False
    assert result.limited is True
    assert result.retry_after_seconds == 45
    assert result.selection_reason == "failover: groq -> codex"
