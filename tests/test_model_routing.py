import json
from pathlib import Path

from orchestrator.model_routing import derive_task_profile
from orchestrator.provider_broker import ProviderBroker


class CatalogProvider:
    def __init__(self, name, models):
        self.name = name
        self.models = models

    def probe_identity(self):
        return {
            "available": True,
            "response": f"{self.name} ready",
            "model": self.models[0]["id"],
            "model_source": "reported",
            "probe_kind": "test",
        }

    def list_models(self):
        return {
            "state": "REPORTED",
            "source": "test-catalog",
            "models": self.models,
            "reason": "test catalog",
        }


def _lang_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "lang"
    directory.mkdir()
    for provider in ("groq", "antigravity", "claude-code", "codex"):
        filename = {
            "groq": "langgroq.json",
            "antigravity": "langantigravity.json",
            "claude-code": "langclaude-code.json",
            "codex": "langcodex.json",
        }[provider]
        (directory / filename).write_text(
            json.dumps({"provider": provider}), encoding="utf-8"
        )
    return directory


def _broker(tmp_path: Path) -> ProviderBroker:
    catalogs = {
        "groq": [{"id": "openai/gpt-oss-120b"}],
        "antigravity": [{"id": "gemini-free"}],
        "claude-code": [{"id": "claude-sonnet-5"}],
        "codex": [
            {
                "id": "gpt-5.6-luna",
                "description": "Fast and affordable agentic coding model.",
                "visibility": "list",
            },
            {
                "id": "gpt-5.6-terra",
                "description": "Balanced agentic coding model for everyday work.",
                "visibility": "list",
            },
            {
                "id": "gpt-5.6-sol",
                "description": "Reliable agentic workhorse for everyday tasks.",
                "visibility": "list",
            },
        ],
    }
    return ProviderBroker(
        [CatalogProvider(name, models) for name, models in catalogs.items()],
        info_dir=tmp_path / "info",
        lang_dir=_lang_dir(tmp_path),
    )


def _make_codex_first(broker: ProviderBroker) -> None:
    for provider in ("groq", "antigravity", "claude-code"):
        broker.ask({
            "command": "report_provider_status",
            "provider": provider,
            "status": {"state": "LIMITED", "retry_at": "2099-01-01T00:00:00+00:00"},
        })


def test_profile_uses_task_content_and_keeps_phase_as_context_only():
    simple = derive_task_profile("Shrň tento krátký odstavec.", workflow_phase="implementation")
    complex_code = derive_task_profile(
        "Implementuj a otestuj komplexní změnu API, včetně migrace a integrace.",
        definition_of_done=[f"bod {index}" for index in range(1, 8)],
        workflow_phase="audit",
    )

    assert simple["model_tier"] == "fast"
    assert complex_code["model_tier"] == "strong"
    assert complex_code["needs_code_changes"] is True
    assert complex_code["workflow_phase"] == "audit"


def test_broker_selects_exact_catalog_model_for_task_profile(tmp_path):
    broker = _broker(tmp_path)
    broker.refresh_provider_notes()
    _make_codex_first(broker)

    fast = broker.ask({
        "command": "select_provider",
        "task_profile": {"model_tier": "fast", "work_type": "general"},
    })["offer"]
    strong = broker.ask({
        "command": "select_provider",
        "task_profile": {"model_tier": "strong", "work_type": "implementation", "needs_code_changes": True},
    })["offer"]

    assert fast["provider"] == "codex"
    assert fast["model"] == "gpt-5.6-luna"
    assert fast["model_source"] == "catalog_task"
    assert strong["model"] == "gpt-5.6-sol"
    assert strong["model_selection_reason"].startswith("catalog task routing")


def test_persistent_forced_model_has_precedence_over_task_routing(tmp_path):
    broker = _broker(tmp_path)
    broker.refresh_provider_notes()
    _make_codex_first(broker)
    broker.ask({
        "command": "set_provider_model",
        "provider": "codex",
        "model_id": "gpt-5.6-terra",
        "source": "user",
    })

    offer = broker.ask({
        "command": "select_provider",
        "task_profile": {"model_tier": "strong", "work_type": "implementation"},
    })["offer"]

    assert offer["model"] == "gpt-5.6-terra"
    assert offer["selection_mode"] == "FORCED"
    assert offer["model_selection_reason"] == "persistent user FORCED model has precedence"
