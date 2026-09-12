import json
from pathlib import Path
from types import SimpleNamespace

import orchestrator.agents.usage_ledger as usage_ledger
from orchestrator.agents.usage_ledger import record_events, record_provider_run, usage_paths


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_usage_is_split_by_exact_model_and_lifetime_is_accumulated(tmp_path):
    events = [
        {
            "provider": "groq",
            "model": "openai/gpt-oss-120b",
            "input_tokens": 10,
            "output_tokens": 4,
            "thinking_tokens": None,
            "total_tokens": 14,
            "cost_usd": None,
        },
        {
            "provider": "groq",
            "model": "openai/gpt-oss-120b",
            "input_tokens": 5,
            "output_tokens": 2,
            "thinking_tokens": 1,
            "total_tokens": 7,
            "cost_usd": 0.25,
        },
        {
            "provider": "groq",
            "model": "other/full-model-id",
            "input_tokens": 3,
            "output_tokens": 1,
            "thinking_tokens": 0,
            "total_tokens": 4,
            "cost_usd": 1.5,
        },
    ]

    record_events(events, directory=tmp_path)
    current_path, lifetime_path = usage_paths("groq", tmp_path)
    current = _read(current_path)
    lifetime = _read(lifetime_path)

    assert set(current["models"]) == {"openai/gpt-oss-120b", "other/full-model-id"}
    assert current["models"]["openai/gpt-oss-120b"] == {
        "input_tokens": 15,
        "output_tokens": 6,
        "thinking_tokens": 1,
        "total_tokens": 21,
        "cost_usd": 0.25,
    }
    assert current["models"]["other/full-model-id"]["cost_usd"] == 1.5
    assert lifetime == current

    record_events(
        [
            {
                "provider": "groq",
                "model": "openai/gpt-oss-120b",
                "input_tokens": None,
                "output_tokens": None,
                "thinking_tokens": None,
                "total_tokens": None,
                "cost_usd": None,
            }
        ],
        directory=tmp_path,
    )
    current = _read(current_path)
    lifetime = _read(lifetime_path)
    assert current["models"]["openai/gpt-oss-120b"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0,
    }
    assert lifetime["models"]["openai/gpt-oss-120b"]["input_tokens"] == 15
    assert lifetime["models"]["openai/gpt-oss-120b"]["cost_usd"] == 0.25


def test_limit_result_with_known_model_is_persisted(monkeypatch, tmp_path):
    def paths(provider, directory=None):
        return tmp_path / f"usage_{provider}.json", tmp_path / f"usage_{provider}_lifetime.json"

    monkeypatch.setattr(usage_ledger, "usage_paths", paths)

    def limited_run(_self, _request):
        return SimpleNamespace(
            model="openai/gpt-oss-120b",
            model_source="configured",
            input_tokens=12,
            output_tokens=0,
            thinking_tokens=0,
            total_tokens=12,
            cost_usd=0,
            limited=True,
        )

    result = record_provider_run("groq")(limited_run)(object(), object())

    assert result.limited is True
    current_path, lifetime_path = usage_paths("groq", tmp_path)
    current = _read(current_path)
    lifetime = _read(lifetime_path)
    expected = {
        "input_tokens": 12,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "total_tokens": 12,
        "cost_usd": 0,
    }
    assert current["models"]["openai/gpt-oss-120b"] == expected
    assert lifetime["models"]["openai/gpt-oss-120b"] == expected
