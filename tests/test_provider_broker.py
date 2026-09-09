import json
from pathlib import Path

from orchestrator.provider_broker import ProviderBroker


class FakeProvider:
    def __init__(self, name, models):
        self.name = name
        self.models = models

    def probe_identity(self):
        return {
            "available": True,
            "response": f"{self.name} ready",
            "model": self.models[0]["id"] if self.models else None,
            "model_source": "reported",
            "probe_kind": "test",
            "full_response": {"provider": self.name},
        }

    def list_models(self):
        return {
            "state": "REPORTED",
            "source": "test-catalog",
            "models": self.models,
            "reason": f"{self.name} catalog",
            "full_response": {"models": self.models},
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
            json.dumps({"provider": provider, "model_selection": {"method": "test"}}),
            encoding="utf-8",
        )
    return directory


def test_refresh_stores_and_compares_catalog_without_writing_models_to_lang(tmp_path):
    catalog = {name: [{"id": f"{name}-one"}] for name in ("groq", "antigravity", "claude-code", "codex")}
    providers = [FakeProvider(name, catalog[name]) for name in catalog]
    lang_dir = _lang_dir(tmp_path)
    lang_before = {
        path.name: path.read_text(encoding="utf-8")
        for path in lang_dir.glob("lang*.json")
    }
    broker = ProviderBroker(
        providers,
        info_dir=tmp_path / "info",
        lang_dir=lang_dir,
    )

    first = broker.refresh_provider_notes()
    assert first["model_updates"] == {}
    assert json.loads((tmp_path / "info" / "groqinfo.json").read_text(encoding="utf-8"))["model_catalog"]["models"] == [
        {"id": "groq-one"}
    ]

    providers[0].models = [{"id": "groq-one"}, {"id": "groq-two"}]
    second = broker.refresh_provider_notes()
    assert second["model_updates"]["groq"]["added"] == ["groq-two"]
    assert second["model_updates"]["groq"]["removed"] == []
    assert all(path.read_text(encoding="utf-8") == lang_before[path.name] for path in lang_dir.glob("lang*.json"))


def test_forced_model_is_persisted_and_used_after_broker_reload(tmp_path):
    catalog = {
        name: [{"id": f"{name}-one"}]
        for name in ("groq", "antigravity", "claude-code", "codex")
    }
    catalog["groq"] = [
        {"id": "openai/gpt-oss-120b"},
        {"id": "openai/gpt-oss-20b"},
    ]
    providers = [FakeProvider(name, catalog[name]) for name in catalog]
    lang_dir = _lang_dir(tmp_path)
    broker = ProviderBroker(
        providers,
        info_dir=tmp_path / "info",
        lang_dir=lang_dir,
    )
    broker.refresh_provider_notes()

    forced = broker.ask(
        {
            "command": "set_provider_model",
            "provider": "groq",
            "model_id": "openai/gpt-oss-120b",
            "source": "user",
        }
    )
    assert forced["success"] is True
    note = json.loads((tmp_path / "info" / "groqinfo.json").read_text(encoding="utf-8"))
    assert note["selected_model"] == "openai/gpt-oss-120b"
    assert note["selection_mode"] == "FORCED"
    assert note["selection_source"] == "user"

    rejected = broker.ask(
        {
            "command": "set_provider_model",
            "provider": "groq",
            "model_id": "openai/gpt-oss-20b",
            "source": "ao",
        }
    )
    assert rejected["success"] is False
    assert "pouze explicitní uživatelský příkaz" in rejected["reason"]
    unchanged = json.loads((tmp_path / "info" / "groqinfo.json").read_text(encoding="utf-8"))
    assert unchanged["selected_model"] == "openai/gpt-oss-120b"

    user_override = broker.ask(
        {
            "command": "set_provider_model",
            "provider": "groq",
            "model_id": "openai/gpt-oss-20b",
            "source": "user",
        }
    )
    assert user_override["success"] is True

    reloaded = ProviderBroker(
        providers,
        info_dir=tmp_path / "info",
        lang_dir=lang_dir,
    )
    offer = reloaded.ask("select_provider")["offer"]
    assert offer["provider"] == "groq"
    assert offer["model"] == "openai/gpt-oss-20b"
    assert offer["model_source"] == "forced"
    assert offer["selection_mode"] == "FORCED"
    assert offer["lang_file"] == str(lang_dir / "langgroq.json")
    assert offer["lang"]["provider"] == "groq"
    assert offer["lang"]["model_selection"] == {"method": "test"}

    automatic = reloaded.ask(
        {
            "command": "set_provider_model",
            "provider": "groq",
            "mode": "AUTO",
            "source": "user",
        }
    )
    assert automatic["success"] is True
    cleared = json.loads((tmp_path / "info" / "groqinfo.json").read_text(encoding="utf-8"))
    assert cleared["selected_model"] is None
    assert cleared["selection_mode"] == "AUTO"


def test_empty_reported_catalog_keeps_last_known_models(tmp_path):
    catalog = {
        name: [{"id": f"{name}-one"}]
        for name in ("groq", "antigravity", "claude-code", "codex")
    }
    providers = [FakeProvider(name, catalog[name]) for name in catalog]
    lang_dir = _lang_dir(tmp_path)
    broker = ProviderBroker(
        providers,
        info_dir=tmp_path / "info",
        lang_dir=lang_dir,
    )
    broker.refresh_provider_notes()

    providers[0].models = []
    refreshed = broker.refresh_provider_notes()
    note = json.loads((tmp_path / "info" / "groqinfo.json").read_text(encoding="utf-8"))
    assert note["model_catalog"]["models"] == [{"id": "groq-one"}]
    assert note["model_catalog"]["models_source"] == "last_known"
    assert note["model_update"]["state"] == "NOT_COMPARABLE"
    assert refreshed["providers"]["groq"]["model_catalog"]["models"] == [{"id": "groq-one"}]

    reloaded = ProviderBroker(
        providers,
        info_dir=tmp_path / "info",
        lang_dir=lang_dir,
    )
    offer = reloaded.ask("select_provider")["offer"]
    assert offer["provider"] == "groq"
    assert note["model_catalog"]["models"] == [{"id": "groq-one"}]
