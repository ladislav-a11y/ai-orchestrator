"""Provider-local token and cost ledgers.

The current-run file is replaced by the latest AO run for that provider.
The lifetime file is updated atomically and accumulates only provider-reported
usage. Values are kept under the exact model identifier returned by the
provider; no model name is shortened or inferred here.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import wraps
from pathlib import Path
import json
import threading
from typing import Any


PROVIDERS = ("groq", "antigravity", "claude-code", "codex")
USAGE_FIELDS = ("input_tokens", "output_tokens", "thinking_tokens", "total_tokens", "cost_usd")
_LOCK = threading.RLock()


def usage_paths(provider: str, directory: Path | str | None = None) -> tuple[Path, Path]:
    if provider not in PROVIDERS:
        raise ValueError(f"Neznámý provider {provider!r}.")
    base = Path(directory) if directory is not None else Path(__file__).resolve().parent
    return base / f"usage_{provider}.json", base / f"usage_{provider}_lifetime.json"


def _empty(provider: str) -> dict[str, Any]:
    return {"provider": provider, "models": {}}


def _read(path: Path, provider: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return _empty(provider)
    if not isinstance(raw, dict) or raw.get("provider") != provider:
        return _empty(provider)
    models = raw.get("models")
    if not isinstance(models, dict):
        raw["models"] = {}
    return raw


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _numeric(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _normalise_metrics(provider: str, metrics: Mapping[str, Any]) -> dict[str, int | float | None]:
    normalised: dict[str, int | float | None] = {}
    for field in USAGE_FIELDS:
        value = _numeric(metrics.get(field))
        normalised[field] = 0 if value is None else value
    return normalised


def _merge_metrics(target: dict[str, Any], delta: Mapping[str, Any]) -> None:
    for field in USAGE_FIELDS:
        value = _numeric(delta.get(field))
        if value is None:
            continue
        previous = _numeric(target.get(field))
        target[field] = value if previous is None else previous + value


def _valid_model(model: Any) -> str | None:
    if not isinstance(model, str) or not model.strip():
        return None
    return model.strip()


def write_provider_usage(
    provider: str,
    current_by_model: Mapping[str, Mapping[str, Any]],
    lifetime_delta_by_model: Mapping[str, Mapping[str, Any]],
    *,
    directory: Path | str | None = None,
) -> tuple[Path, Path]:
    """Write one provider's current snapshot and add one lifetime delta.

    Both mappings are keyed by the exact, provider-reported model identifier.
    Empty or unknown model identifiers are ignored rather than replaced by a
    shortened or invented name.
    """
    current_path, lifetime_path = usage_paths(provider, directory)
    current = _empty(provider)
    for model, metrics in current_by_model.items():
        exact_model = _valid_model(model)
        if exact_model is not None and isinstance(metrics, Mapping):
            current["models"][exact_model] = _normalise_metrics(provider, metrics)

    with _LOCK:
        lifetime = _read(lifetime_path, provider)
        for model, metrics in lifetime_delta_by_model.items():
            exact_model = _valid_model(model)
            if exact_model is None or not isinstance(metrics, Mapping):
                continue
            bucket = lifetime["models"].setdefault(
                exact_model,
                {field: None for field in USAGE_FIELDS},
            )
            _merge_metrics(bucket, _normalise_metrics(provider, metrics))
        _write(current_path, current)
        _write(lifetime_path, lifetime)
    return current_path, lifetime_path


def metrics_from_result(provider: str, result: Any) -> tuple[str | None, dict[str, Any]]:
    model = _valid_model(getattr(result, "model", None))
    metrics = {
        field: getattr(result, field, None)
        for field in USAGE_FIELDS
    }
    metrics = _normalise_metrics(provider, metrics)
    return model, metrics


def reset_provider_current(provider: str, *, directory: Path | str | None = None) -> Path:
    current_path, _ = usage_paths(provider, directory)
    with _LOCK:
        _write(current_path, _empty(provider))
    return current_path


def record_result(
    provider: str,
    result: Any,
    current_by_model: dict[str, dict[str, Any]],
    *,
    directory: Path | str | None = None,
) -> tuple[Path, Path] | None:
    model, metrics = metrics_from_result(provider, result)
    if model is None or not any(_numeric(metrics.get(field)) is not None for field in USAGE_FIELDS):
        return None
    bucket = current_by_model.setdefault(model, {field: None for field in USAGE_FIELDS})
    _merge_metrics(bucket, metrics)
    return write_provider_usage(
        provider,
        current_by_model,
        {model: metrics},
        directory=directory,
    )


def record_events(
    events: list[Mapping[str, Any]],
    *,
    directory: Path | str | None = None,
) -> list[tuple[Path, Path]]:
    """Persist one completed AO run represented by usage events."""
    by_provider: dict[str, dict[str, dict[str, Any]]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        provider = event.get("provider")
        model = _valid_model(event.get("model"))
        if provider not in PROVIDERS or model is None:
            continue
        metrics = _normalise_metrics(provider, event)
        if not any(_numeric(metrics.get(field)) is not None for field in USAGE_FIELDS):
            continue
        bucket = by_provider.setdefault(provider, {}).setdefault(
            model,
            {field: None for field in USAGE_FIELDS},
        )
        _merge_metrics(bucket, metrics)

    paths: list[tuple[Path, Path]] = []
    for provider, models in by_provider.items():
        paths.append(write_provider_usage(provider, models, models, directory=directory))
    return paths


def record_provider_run(provider: str):
    """Decorate one provider adapter's run method with provider-local usage."""
    def decorate(run):
        @wraps(run)
        def wrapped(self, request, *args, **kwargs):
            reset_provider_current(provider)
            result = run(self, request, *args, **kwargs)
            if getattr(result, "model_source", None) in {"reported", "reported_receipt"}:
                try:
                    record_result(provider, result, {})
                except (OSError, TypeError, ValueError):
                    # Usage persistence must not turn a completed provider
                    # response into a failed AO task.
                    pass
            return result

        return wrapped

    return decorate
