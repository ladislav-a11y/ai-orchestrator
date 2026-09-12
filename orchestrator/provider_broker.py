"""v2 offer-only broker for the four currently configured providers.

Schopnosti tohoto brokeru:

* přijímá pouze dotaz AO na výběr providera;
* pro běžný výběr prochází pevné pořadí Groq, Antigravity, Claude Code, Codex;
* čte jeden strojově zpracovatelný soubor ``<provider>info.json`` pro každého
  providera;
* při chybějící, neplatné, neznámé nebo chybové poznámce ověří pouze aktuální
  dostupnost příslušného providera přes ``is_available()``;
* ukládá úplnou odpověď kontroly a odděluje známé a neznámé odpovědi;
* na příkaz ``refresh_provider_notes`` postupně obnoví poznámky všech čtyř
  providerů;
* ``refresh_provider_notes`` je v2 aktivní výzkum stavu: znovu provede
  providerové probe/katalogové dotazy a zapíše aktuální potvrzené odpovědi,
  limity a modely do poznámek;
* AO vrací jméno providera, model, stav, důvod a cestu k poznámce;
* AO vrací také komunikační recept z příslušného ``lang*.json``;
* nepřijímá pracovní úkol a nikdy nevolá pracovní ``run()`` providera.
* přijímá providerem/AO publikovaný v2 status a ukládá jej do poznámky;
  broker status pouze eviduje a podle něj filtruje nabídky.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from orchestrator.model_routing import normalize_task_profile, profile_summary


PROVIDER_ORDER = ("groq", "antigravity", "claude-code", "codex")
LANG_FILES = {
    "groq": "langgroq.json",
    "antigravity": "langantigravity.json",
    "claude-code": "langclaude-code.json",
    "codex": "langcodex.json",
}
SELECT_PROVIDER_QUERY = "select_provider"
REFRESH_PROVIDER_NOTES_QUERY = "refresh_provider_notes"
SET_PROVIDER_MODEL_QUERY = "set_provider_model"
REPORT_PROVIDER_STATUS_QUERY = "report_provider_status"
USER_MODEL_SELECTION_SOURCE = "user"
VALID_STATES = {"AVAILABLE", "LIMITED", "UNAVAILABLE", "UNKNOWN", "ERROR"}
MODEL_CATALOG_STATES = {"REPORTED", "PARTIAL", "UNKNOWN", "ERROR"}
BROKER_MODEL_API_PROVIDERS = {"claude-code"}
BROKER_ONLY_API_KEY = "ANTHROPIC_API_KEY"
TASK_MODEL_PROVIDERS = {"claude-code", "codex"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retry_due(value: Optional[str]) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        retry_at = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= retry_at.astimezone(timezone.utc)


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)
    return value


def _selectable_model(value: Any) -> Optional[str]:
    """Return one executable model ID, never an aggregate observation."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or "," in normalized or "\n" in normalized or "\r" in normalized:
        return None
    return normalized


def _catalog_index(models: Any) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    if not isinstance(models, list):
        return indexed
    for item in models:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id") or item.get("model_id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        normalized = dict(item)
        normalized["id"] = model_id.strip()
        indexed[normalized["id"]] = normalized
    return indexed


def _compare_catalogs(previous: Any, current: dict[str, Any]) -> dict[str, Any]:
    previous = previous if isinstance(previous, dict) else {}
    current_state = current.get("state")
    if current_state != "REPORTED":
        return {
            "state": "NOT_COMPARABLE",
            "has_changes": False,
            "reason": "Aktuální katalog nebyl providerem potvrzen; předchozí katalog se neoznačuje jako odstraněný.",
            "added": [],
            "removed": [],
            "changed": [],
        }
    previous_models = _catalog_index(previous.get("models"))
    current_models = _catalog_index(current.get("models"))
    if previous.get("state") != "REPORTED":
        return {
            "state": "BASELINE_CREATED",
            "has_changes": False,
            "reason": "Vytvořen první porovnatelný katalog; skutečné update se vyhodnotí při další kontrole.",
            "added": [],
            "removed": [],
            "changed": [],
        }
    added = sorted(set(current_models) - set(previous_models))
    removed = sorted(set(previous_models) - set(current_models))
    changed = [
        {
            "id": model_id,
            "before": previous_models[model_id],
            "after": current_models[model_id],
        }
        for model_id in sorted(set(previous_models) & set(current_models))
        if previous_models[model_id] != current_models[model_id]
    ]
    has_changes = bool(added or removed or changed)
    return {
        "state": "UPDATED" if has_changes else "UNCHANGED",
        "has_changes": has_changes,
        "reason": (
            f"Katalog změněn: +{len(added)} / -{len(removed)} / změněno {len(changed)}."
            if has_changes
            else "Katalog je beze změny."
        ),
        "added": added,
        "removed": removed,
        "changed": changed,
    }


@dataclass
class ProviderInfo:
    provider: str
    # v2 note format; older notes are upgraded in memory and gain this field
    # on the next broker write.
    architecture_version: str = "v2"
    model: Optional[str] = None
    model_source: Optional[str] = None
    state: str = "UNKNOWN"
    checked_at: Optional[str] = None
    available_at: Optional[str] = None
    retry_at: Optional[str] = None
    reason: Optional[str] = None
    full_response: Any = None
    response_kind: str = "unknown"
    probe_kind: Optional[str] = None
    usage: Any = None
    # v2 evidence received from the provider/AO; broker never evaluates task
    # output and never invents a quota state.
    status_details: dict[str, Any] = field(default_factory=dict)
    known_responses: dict[str, int] = field(default_factory=dict)
    unknown_responses: list[dict[str, Any]] = field(default_factory=list)
    model_catalog: dict[str, Any] = field(
        default_factory=lambda: {"state": "UNKNOWN", "models": []}
    )
    model_update: dict[str, Any] = field(
        default_factory=lambda: {"state": "UNKNOWN", "has_changes": False}
    )
    selected_model: Optional[str] = None
    selection_mode: str = "AUTO"
    selection_source: Optional[str] = None
    selection_updated_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


@dataclass
class ProviderOffer:
    provider: Optional[str]
    model: Optional[str]
    model_source: Optional[str]
    selection_mode: str
    state: str
    reason: str
    info_file: Optional[str] = None
    lang_file: Optional[str] = None
    lang: Optional[dict[str, Any]] = None
    model_selection_reason: Optional[str] = None
    task_profile: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderBroker:
    """Select a provider for AO without executing provider work."""

    def __init__(
        self,
        providers: list[Any],
        models: Optional[Mapping[str, Optional[str]]] = None,
        info_dir: Path | str = Path("data") / "provider-info",
        logger: Optional[Any] = None,
        lang_dir: Path | str = Path("orchestrator") / "agents",
    ) -> None:
        by_name = {provider.name: provider for provider in providers}
        missing = [name for name in PROVIDER_ORDER if name not in by_name]
        if missing:
            raise ValueError(f"Chybí provideři brokeru: {', '.join(missing)}")
        self.providers = by_name
        for provider_name, provider in by_name.items():
            def publish(result: Any, selected_provider: str = provider_name) -> None:
                self.report_provider_result(selected_provider, result)

            # Provider-owned status publication. This does not make the
            # broker execute work; it only gives broker-created providers a
            # sink for their own post-run status receipt.
            setattr(provider, "_broker_status_publisher", publish)
        self.models = dict(models or {})
        self.info_dir = Path(info_dir)
        self.logger = logger
        self.lang_dir = Path(lang_dir)

    def _log(self, message: str) -> None:
        if self.logger is None:
            return
        if callable(self.logger):
            self.logger(message)
        elif hasattr(self.logger, "info"):
            self.logger.info(message)

    def _info_path(self, provider: str) -> Path:
        return self.info_dir / f"{provider}info.json"

    def _lang_path(self, provider: str) -> Path:
        return self.lang_dir / LANG_FILES[provider]

    def _lang_contract(self, provider: str) -> dict[str, Any]:
        path = self._lang_path(provider)
        try:
            contract = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(contract, dict) or contract.get("provider") != provider:
            return {}
        return contract

    def _model_selection_contract(self, provider: str) -> dict[str, Any]:
        selection = self._lang_contract(provider).get("model_selection")
        return dict(selection) if isinstance(selection, dict) else {}

    def _save_info(self, info: ProviderInfo) -> None:
        path = self._info_path(info.provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(info.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _load_info(self, provider: str) -> tuple[ProviderInfo, bool]:
        path = self._info_path(provider)
        if not path.is_file():
            return ProviderInfo(provider=provider, model=self.models.get(provider)), False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("provider") != provider:
                raise ValueError("poznámka nemá odpovídající název providera")
            state = raw.get("state", "UNKNOWN")
            if state not in VALID_STATES:
                raise ValueError(f"neplatný stav {state!r}")
            known_responses = raw.get("known_responses", {})
            unknown_responses = raw.get("unknown_responses", [])
            if not isinstance(known_responses, dict):
                raise ValueError("known_responses musí být objekt")
            if not isinstance(unknown_responses, list):
                raise ValueError("unknown_responses musí být seznam")
            info = ProviderInfo(
                provider=provider,
                architecture_version="v2",
                model=raw.get("model", self.models.get(provider)),
                model_source=raw.get("model_source"),
                state=state,
                checked_at=raw.get("checked_at"),
                available_at=raw.get("available_at"),
                retry_at=raw.get("retry_at"),
                reason=raw.get("reason"),
                full_response=raw.get("full_response"),
                response_kind=raw.get("response_kind", "unknown"),
                probe_kind=raw.get("probe_kind"),
                usage=raw.get("usage"),
                status_details=(
                    raw.get("status_details")
                    if isinstance(raw.get("status_details"), dict)
                    else {}
                ),
                known_responses=known_responses,
                unknown_responses=unknown_responses,
                model_catalog=(
                    raw.get("model_catalog")
                    if isinstance(raw.get("model_catalog"), dict)
                    else {"state": "UNKNOWN", "models": []}
                ),
                model_update=(
                    raw.get("model_update")
                    if isinstance(raw.get("model_update"), dict)
                    else {"state": "UNKNOWN", "has_changes": False}
                ),
                selected_model=raw.get("selected_model"),
                selection_mode=str(raw.get("selection_mode") or "AUTO").upper(),
                selection_source=raw.get("selection_source"),
                selection_updated_at=raw.get("selection_updated_at"),
            )
            return info, True
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return ProviderInfo(
                provider=provider,
                model=self.models.get(provider),
                state="ERROR",
                checked_at=_now(),
                reason=f"Neplatná poznámka providera: {exc}",
                full_response={"error": str(exc)},
                response_kind="note_error",
            ), False

    def _remember_probe_error(self, provider: str, exc: Exception) -> ProviderInfo:
        info, _ = self._load_info(provider)
        message = f"Kontrola dostupnosti selhala: {exc}"
        configured_model = self.models.get(provider)
        if isinstance(configured_model, str) and configured_model.strip():
            info.model = configured_model.strip()
        info.state = "ERROR"
        info.checked_at = _now()
        info.reason = message
        info.full_response = {
            "exception_type": type(exc).__name__,
            "exception": str(exc),
        }
        info.response_kind = "probe_error"
        self._save_info(info)
        self._log(f"{provider}: {message}")
        return info

    def _probe(self, provider: str) -> ProviderInfo:
        try:
            identity_probe = getattr(self.providers[provider], "probe_identity", None)
            if callable(identity_probe):
                probe = identity_probe()
            else:
                available, response = self.providers[provider].is_available()
                probe = {
                    "available": available,
                    "response": response,
                    "model": self.models.get(provider),
                    "model_source": "configured" if self.models.get(provider) else None,
                    "probe_kind": "availability_only",
                    "full_response": {"available": available, "response": response},
                }
        except Exception as exc:
            return self._remember_probe_error(provider, exc)

        info, _ = self._load_info(provider)
        configured_model = _selectable_model(self.models.get(provider))
        if configured_model:
            info.model = configured_model
        elif not isinstance(info.model, str) or not info.model.strip():
            info.model = None
        probe_model = probe.get("model") if isinstance(probe, dict) else None
        selectable_probe_model = _selectable_model(probe_model)
        if selectable_probe_model:
            info.model = selectable_probe_model
            info.model_source = probe.get("model_source") if isinstance(probe, dict) else None
        elif isinstance(probe_model, str) and probe_model.strip():
            # modelUsage may contain several exact models observed during one
            # run. It is valid evidence for notes, but never a valid --model
            # value for the next request. Let AUTO use the provider default.
            info.model = configured_model
            info.model_source = "reported_multiple"
        else:
            info.model_source = probe.get("model_source") if isinstance(probe, dict) else None
        info.probe_kind = probe.get("probe_kind") if isinstance(probe, dict) else None
        info.usage = probe.get("usage") if isinstance(probe, dict) else None
        info.status_details = (
            probe.get("status_details", probe.get("status", {}))
            if isinstance(probe, dict)
            and isinstance(probe.get("status_details", probe.get("status", {})), dict)
            else {}
        )
        info.checked_at = _now()
        available = probe.get("available") if isinstance(probe, dict) else None
        response = probe.get("response") if isinstance(probe, dict) else None
        info.full_response = probe.get("full_response", probe) if isinstance(probe, dict) else probe
        reported_status = probe.get("status") if isinstance(probe, dict) else None
        if isinstance(reported_status, dict) and reported_status.get("state") == "LIMITED":
            info.state = "LIMITED"
            info.available_at = None
            info.retry_at = reported_status.get("retry_at")
            info.reason = str(reported_status.get("reason") or "Provider je omezen.")
            info.response_kind = "limited"
            info.known_responses[info.response_kind] = (
                info.known_responses.get(info.response_kind, 0) + 1
            )
            self._save_info(info)
            self._log(f"{provider}: {info.state} — {info.reason}")
            return info
        if isinstance(available, bool) and isinstance(response, str):
            info.state = "AVAILABLE" if available else "UNAVAILABLE"
            info.available_at = info.checked_at if available else None
            info.reason = response
            info.response_kind = "available" if available else "unavailable"
            info.known_responses[info.response_kind] = (
                info.known_responses.get(info.response_kind, 0) + 1
            )
        else:
            info.state = "UNKNOWN"
            info.reason = "Provider vrátil odpověď v neznámém formátu."
            info.response_kind = "unknown"
            info.unknown_responses.append(
                {"checked_at": info.checked_at, "response": info.full_response}
            )
        self._save_info(info)
        self._log(f"{provider}: {info.state} — {info.reason}")
        return info

    def report_provider_status(
        self,
        provider: str,
        status: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist a v2 provider status; AO remains the direct task caller."""
        if provider not in PROVIDER_ORDER:
            raise ValueError(f"Neznámý provider {provider!r}.")
        if not isinstance(status, Mapping):
            raise ValueError("status providera musí být objekt.")
        state = str(status.get("state") or "UNKNOWN").upper()
        if state not in VALID_STATES:
            raise ValueError(f"Neplatný stav providera {state!r}.")
        info, _ = self._load_info(provider)
        checked_at = status.get("checked_at")
        if not isinstance(checked_at, str) or not checked_at.strip():
            checked_at = _now()
        info.state = state
        info.checked_at = checked_at
        info.reason = str(status.get("reason") or f"Provider nahlásil stav {state}.")
        info.response_kind = str(status.get("response_kind") or state.casefold())
        info.probe_kind = "provider_status"
        info.full_response = _json_safe(status.get("full_response", dict(status)))
        details = status.get("status_details", status.get("quota_snapshot", {}))
        info.status_details = _json_safe(details) if isinstance(details, dict) else {}
        if isinstance(status.get("usage"), dict):
            info.usage = _json_safe(status["usage"])
        model = status.get("model")
        selectable_model = _selectable_model(model)
        if selectable_model:
            info.model = selectable_model
            model_source = status.get("model_source")
            if isinstance(model_source, str) and model_source.strip():
                info.model_source = model_source.strip()
        elif isinstance(model, str) and model.strip():
            # Preserve the complete multi-model observation in full_response,
            # but never leak it into the executable AUTO offer.
            info.model = _selectable_model(self.models.get(provider))
            info.model_source = "reported_multiple"
        if state == "AVAILABLE":
            info.available_at = checked_at
            info.retry_at = None
        elif state == "LIMITED":
            info.available_at = None
            retry_at = status.get("retry_at")
            info.retry_at = retry_at if isinstance(retry_at, str) and retry_at.strip() else None
        else:
            info.available_at = None
            info.retry_at = status.get("retry_at") if isinstance(status.get("retry_at"), str) else None
        info.known_responses[info.response_kind] = info.known_responses.get(info.response_kind, 0) + 1
        self._save_info(info)
        self._log(f"{provider}: {info.state} — {info.reason}")
        return {
            "command": REPORT_PROVIDER_STATUS_QUERY,
            "success": True,
            "provider": provider,
            "state": info.state,
            "reason": info.reason,
            "retry_at": info.retry_at,
            "info_file": str(self._info_path(provider)),
        }

    def report_provider_result(self, provider: str, result: Any) -> Optional[dict[str, Any]]:
        """v2 AO handoff: publish provider-owned status after a direct run."""
        status = getattr(result, "provider_status", None)
        if not isinstance(status, Mapping):
            return None
        payload = dict(status)
        model = getattr(result, "model", None)
        model_source = getattr(result, "model_source", None)
        if model and "model" not in payload:
            payload["model"] = model
        if model_source and "model_source" not in payload:
            payload["model_source"] = model_source
        return self.report_provider_status(provider, payload)

    def _list_models_from_broker_api(
        self, provider: str
    ) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
        """Read a provider catalog through the broker-owned API boundary.

        The API credential is deliberately handled here, not by a provider
        adapter.  This path is read-only and is used only for model catalog
        refresh.  Provider identity probes and work remain CLI calls.
        """
        if provider not in BROKER_MODEL_API_PROVIDERS:
            return None, None
        provider_contract = self._lang_contract(provider)
        identity_probe = provider_contract.get("identity_probe", {})
        contract = identity_probe.get("model_catalog", {}) if isinstance(identity_probe, dict) else {}
        api = contract.get("api", {}) if isinstance(contract, dict) else {}
        if not isinstance(api, dict):
            return None, None
        key_env = str(api.get("api_key_env") or BROKER_ONLY_API_KEY)
        if key_env != BROKER_ONLY_API_KEY:
            return None, {
                "kind": "configuration_error",
                "reason": "Broker Models API musí používat pouze ANTHROPIC_API_KEY.",
            }
        api_key = os.environ.get(BROKER_ONLY_API_KEY, "").strip()
        if not api_key:
            return None, None

        base_url_env = str(api.get("base_url_env") or "ANTHROPIC_MODELS_API_BASE_URL")
        base_url = os.environ.get(base_url_env, "").strip() or str(
            api.get("default_base_url") or "https://api.anthropic.com"
        )
        path = str(api.get("path") or "/v1/models")
        version = str(api.get("anthropic_version") or "2023-06-01")
        try:
            page_limit = max(1, min(1000, int(api.get("page_limit", 1000))))
        except (TypeError, ValueError):
            page_limit = 1000
        models: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        after_id: Optional[str] = None
        seen_cursors: set[str] = set()
        try:
            timeout = min(90, max(10, int(self.providers[provider].config.timeout_seconds)))
        except (AttributeError, TypeError, ValueError):
            timeout = 60

        while True:
            query = {"limit": str(page_limit)}
            if after_id:
                query["after_id"] = after_id
            request = Request(
                f"{base_url.rstrip('/')}{path}?{urlencode(query)}",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "anthropic-version": version,
                    "accept": "application/json",
                },
                method="GET",
            )
            workspace_env = str(api.get("workspace_env") or "ANTHROPIC_WORKSPACE_ID")
            workspace_id = os.environ.get(workspace_env, "").strip()
            if workspace_id:
                request.add_header("anthropic-workspace-id", workspace_id)
            try:
                with urlopen(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                return None, {"kind": "http_error", "status": exc.code, "reason": str(exc.reason)}
            except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
                return None, {"kind": "request_error", "type": type(exc).__name__, "reason": str(exc)}
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                return None, {"kind": "response_error", "reason": "Models API nevrátilo očekávané pole data."}
            page_models = [item for item in payload["data"] if isinstance(item, dict)]
            models.extend(page_models)
            pages.append({
                "count": len(page_models),
                "first_id": payload.get("first_id"),
                "last_id": payload.get("last_id"),
                "has_more": bool(payload.get("has_more")),
            })
            if not payload.get("has_more"):
                break
            next_after = payload.get("last_id")
            if not isinstance(next_after, str) or not next_after.strip() or next_after in seen_cursors:
                return None, {"kind": "pagination_error", "reason": "Models API vrátilo neplatný stránkovací kurzor."}
            seen_cursors.add(next_after)
            after_id = next_after

        return {
            "state": "REPORTED",
            "source": "anthropic_models_api",
            "models": models,
            "picker_choices": [],
            "reason": f"Broker přes Anthropic Models API potvrdil úplný katalog ({len(models)} modelů).",
            "full_response": {
                "catalog_endpoint": path,
                "page_count": len(pages),
                "pages": pages,
                "complete_account_catalog": True,
            },
        }, None

    def _refresh_model_catalog(self, provider: str, info: ProviderInfo) -> ProviderInfo:
        previous = info.model_catalog
        checked_at = _now()
        result, api_error = self._list_models_from_broker_api(provider)
        if result is None:
            catalog_probe = getattr(self.providers[provider], "list_models", None)
            try:
                result = catalog_probe() if callable(catalog_probe) else {
                    "state": "UNKNOWN",
                    "source": "adapter",
                    "models": [],
                    "reason": "Provider adapter katalog modelů neposkytuje.",
                    "full_response": {},
                }
            except Exception as exc:
                result = {
                    "state": "ERROR",
                    "source": "adapter",
                    "models": [],
                    "reason": f"Čtení katalogu selhalo: {exc}",
                    "full_response": {"exception_type": type(exc).__name__, "exception": str(exc)},
                }
            if api_error and isinstance(result, dict):
                result = dict(result)
                full_response = result.get("full_response")
                full_response = dict(full_response) if isinstance(full_response, dict) else {"provider_result": full_response}
                full_response["broker_api_error"] = api_error
                result["full_response"] = full_response
        if not isinstance(result, dict):
            result = {
                "state": "ERROR",
                "source": "adapter",
                "models": [],
                "reason": "Adapter vrátil katalog v neznámém formátu.",
                "full_response": result,
            }
        state = str(result.get("state") or "UNKNOWN").upper()
        if state not in MODEL_CATALOG_STATES:
            state = "ERROR"
        reported = state in {"REPORTED", "PARTIAL"}
        previous_models = previous.get("models", []) if isinstance(previous, dict) else []
        reported_models = result.get("models")
        previous_has_models = bool(_catalog_index(previous_models))
        retained_previous = False
        if reported:
            if not isinstance(reported_models, list):
                retained_previous = previous_has_models
            elif previous_has_models and not _catalog_index(reported_models):
                retained_previous = True
            models = previous_models if retained_previous else (
                reported_models if isinstance(reported_models, list) else []
            )
            if retained_previous:
                state = "UNKNOWN"
        else:
            models = previous_models
        reason = result.get("reason")
        if retained_previous:
            reason = (
                f"{reason or 'Provider vrátil prázdný nebo neplatný katalog.'} "
                "Poslední potvrzený katalog byl zachován."
            )
        current = {
            "state": state,
            "source": result.get("source"),
            "checked_at": checked_at,
            "models": models,
            "picker_choices": result.get("picker_choices", []),
            "models_source": "last_known" if retained_previous else ("provider_response" if state == "PARTIAL" else ("provider_catalog" if reported else ("last_known" if models else None))),
            "reason": reason,
            "selection": self._model_selection_contract(provider),
            "full_response": _json_safe(result.get("full_response", result)),
        }
        update = _compare_catalogs(previous, current)
        update["checked_at"] = checked_at
        info.model_catalog = current
        info.model_update = update
        self._save_info(info)
        if update.get("has_changes"):
            self._log(f"{provider}: UPDATE MODEL KATALOGU — {update['reason']}")
        else:
            self._log(f"{provider}: model katalog — {update.get('reason')}")
        return info

    def _catalog_model_for_task(
        self, info: ProviderInfo, task_profile: Mapping[str, Any]
    ) -> tuple[Optional[str], Optional[str]]:
        """Choose an exact visible model using only the broker note catalog.

        Catalog descriptions and IDs are provider evidence.  The broker uses
        them as routing hints; it never invents an ID and never treats this
        ephemeral choice as a persistent FORCED setting.
        """
        catalog = info.model_catalog if isinstance(info.model_catalog, dict) else {}
        if catalog.get("state") != "REPORTED":
            return None, None
        tier = str(task_profile.get("model_tier") or "balanced").casefold()
        tier_terms = {
            "fast": ("fast", "affordable", "haiku", "luna"),
            "balanced": ("balanced", "everyday", "sonnet", "terra"),
            "strong": ("reliable", "workhorse", "proven", "opus", "sol"),
        }
        terms = tier_terms.get(tier, tier_terms["balanced"])
        candidates: list[tuple[int, str, str]] = []
        for model in _catalog_index(catalog.get("models")).values():
            model_id = model["id"]
            visibility = str(model.get("visibility") or "list").casefold()
            if visibility in {"hide", "hidden", "internal"}:
                continue
            if "auto-review" in model_id.casefold() or "review" in model_id.casefold():
                continue
            haystack = " ".join(
                str(model.get(key) or "")
                for key in ("id", "slug", "display_name", "description")
            ).casefold()
            score = sum(2 if term in haystack else 0 for term in terms)
            if task_profile.get("needs_code_changes") and "coding" in haystack:
                score += 2
            if task_profile.get("work_type") == "research" and "general" in haystack:
                score += 1
            created_at = str(model.get("created_at") or "")
            candidates.append((score, created_at, model_id))
        if not candidates:
            return None, None
        score, _created_at, model_id = max(candidates, key=lambda item: (item[0], item[1], item[2]))
        if score <= 0:
            return None, None
        return model_id, f"catalog task routing: tier={tier}; {profile_summary(task_profile)}"

    def _offer(
        self, info: ProviderInfo, task_profile: Optional[Mapping[str, Any]] = None
    ) -> ProviderOffer:
        profile = normalize_task_profile(task_profile)
        if info.state == "AVAILABLE":
            forced = info.selection_mode == "FORCED" and info.selected_model
            task_model = None
            task_reason = None
            if not forced and info.provider in TASK_MODEL_PROVIDERS and profile:
                task_model, task_reason = self._catalog_model_for_task(info, profile)
            selected_model = info.selected_model if forced else (task_model or info.model)
            selected_source = "forced" if forced else ("catalog_task" if task_model else info.model_source)
            return ProviderOffer(
                provider=info.provider,
                model=selected_model,
                model_source=selected_source,
                selection_mode="FORCED" if forced else "AUTO",
                state=info.state,
                reason=info.reason or "Provider je dostupný.",
                info_file=str(self._info_path(info.provider)),
                lang_file=str(self._lang_path(info.provider)),
                lang=self._lang_contract(info.provider),
                model_selection_reason=(
                    "persistent user FORCED model has precedence"
                    if forced else task_reason
                ),
                task_profile=profile or None,
            )
        return ProviderOffer(
            provider=None,
            model=None,
            model_source=None,
            selection_mode=info.selection_mode,
            state=info.state,
            reason=info.reason or f"Provider není dostupný: {info.state}.",
            info_file=str(self._info_path(info.provider)),
            task_profile=profile or None,
        )

    @staticmethod
    def _provider_suitable_for_task(
        provider: str, task_profile: Optional[Mapping[str, Any]]
    ) -> tuple[bool, Optional[str]]:
        """Return whether an AVAILABLE provider should receive this task.

        Suitability is a per-task routing decision, not provider health.
        Groq Free remains AVAILABLE when AO skips a task that its bounded
        TPM/tool-loop budget is unlikely to handle reliably.
        """
        profile = normalize_task_profile(task_profile)
        if provider != "groq" or not profile:
            return True, None

        complexity = str(profile.get("complexity") or "").casefold()
        model_tier = str(profile.get("model_tier") or "").casefold()
        if complexity == "complex" or model_tier == "strong":
            return (
                False,
                "Groq Free preflight přeskočil úlohu klasifikovanou jako "
                f"complex/strong ({profile_summary(profile)}).",
            )
        return True, None

    def _first_available(
        self,
        infos: Mapping[str, ProviderInfo],
        excluded: set[str] | None = None,
        task_profile: Optional[Mapping[str, Any]] = None,
    ) -> ProviderOffer:
        excluded = excluded or set()
        skipped_for_task: list[str] = []
        for provider in PROVIDER_ORDER:
            if provider in excluded:
                continue
            info = infos[provider]
            if info.state == "AVAILABLE":
                suitable, reason = self._provider_suitable_for_task(
                    provider, task_profile
                )
                if suitable:
                    return self._offer(info, task_profile)
                skipped_for_task.append(reason or provider)
        return ProviderOffer(
            provider=None,
            model=None,
            model_source=None,
            selection_mode="AUTO",
            state="NONE_AVAILABLE",
            reason=(
                "Žádný provider mimo již neúspěšné pokusy nemá stav AVAILABLE "
                "a zároveň není vyřazen task-level preflightem."
                if excluded
                else (
                    "Žádný provider není pro tento task použitelný: "
                    + "; ".join(skipped_for_task)
                    if skipped_for_task
                    else "Žádný provider nemá stav AVAILABLE."
                )
            ),
        )

    def select_provider(
        self,
        excluded: set[str] | None = None,
        task_profile: Optional[Mapping[str, Any]] = None,
    ) -> ProviderOffer:
        excluded = excluded or set()
        infos: dict[str, ProviderInfo] = {}
        for provider in PROVIDER_ORDER:
            info, valid = self._load_info(provider)
            # UNAVAILABLE (e.g. a CLI that failed to launch, a broken
            # identity probe) is retried here just like ERROR/UNKNOWN -
            # without this, a provider that failed once stays cached
            # UNAVAILABLE forever and every later dispatch attempt (for
            # example right after another provider times out and this
            # method is called again to find a replacement) never gives it
            # another chance, even though the underlying cause (a transient
            # launcher failure, an app mid-update, ...) may already be gone.
            if (
                not valid
                or info.state in {"UNKNOWN", "ERROR", "UNAVAILABLE"}
                or (info.state == "LIMITED" and _retry_due(info.retry_at))
            ):
                info = self._probe(provider)
            infos[provider] = info
            if provider not in excluded and info.state == "AVAILABLE":
                suitable, reason = self._provider_suitable_for_task(
                    provider, task_profile
                )
                if suitable:
                    return self._offer(info, task_profile)
                self._log(reason or f"{provider}: task-level preflight skip")
        return self._first_available(infos, excluded, task_profile)

    def select_named_provider(
        self, provider: str, task_profile: Optional[Mapping[str, Any]] = None
    ) -> ProviderOffer:
        """Return the offer for an explicitly requested provider."""
        if provider not in PROVIDER_ORDER:
            raise ValueError(f"Neznámý provider {provider!r}.")
        info, valid = self._load_info(provider)
        if (
            not valid
            or info.state in {"UNKNOWN", "ERROR", "UNAVAILABLE"}
            or (info.state == "LIMITED" and _retry_due(info.retry_at))
        ):
            info = self._probe(provider)
        return self._offer(info, task_profile)

    def refresh_provider_notes(self) -> dict[str, Any]:
        infos = {
            provider: self._refresh_model_catalog(provider, self._probe(provider))
            for provider in PROVIDER_ORDER
        }
        offer = self._first_available(infos)
        updates = {
            provider: infos[provider].model_update
            for provider in PROVIDER_ORDER
            if infos[provider].model_update.get("has_changes")
        }
        return {
            "command": REFRESH_PROVIDER_NOTES_QUERY,
            "order": list(PROVIDER_ORDER),
            "providers": {provider: infos[provider].to_dict() for provider in PROVIDER_ORDER},
            "model_updates": updates,
            "offer": offer.to_dict(),
        }

    def set_provider_model(
        self,
        provider: str,
        model_id: Optional[str],
        *,
        source: str = "ao",
        mode: str = "FORCED",
    ) -> dict[str, Any]:
        """Persist an exact model selection in the provider note JSON."""
        if provider not in PROVIDER_ORDER:
            raise ValueError(f"Neznámý provider {provider!r}.")
        info, _ = self._load_info(provider)
        normalized_source = str(source or "ao").strip().lower()
        if info.selection_mode == "FORCED" and normalized_source != USER_MODEL_SELECTION_SOURCE:
            return {
                "command": SET_PROVIDER_MODEL_QUERY,
                "success": False,
                "provider": provider,
                "model_id": model_id,
                "reason": "FORCED model může přepsat pouze explicitní uživatelský příkaz source='user'.",
                "selected_model": info.selected_model,
                "selection_mode": info.selection_mode,
                "info_file": str(self._info_path(provider)),
            }
        normalized_mode = str(mode or "FORCED").upper()
        if normalized_mode == "AUTO":
            info.selected_model = None
            info.selection_mode = "AUTO"
            info.selection_source = normalized_source
            info.selection_updated_at = _now()
        elif normalized_mode == "FORCED":
            normalized_model = (model_id or "").strip()
            if not normalized_model:
                raise ValueError("FORCED výběr vyžaduje neprázdný přesný model_id.")
            catalog = info.model_catalog if isinstance(info.model_catalog, dict) else {}
            if catalog.get("state") == "REPORTED" and normalized_model not in _catalog_index(catalog.get("models")):
                return {
                    "command": SET_PROVIDER_MODEL_QUERY,
                    "success": False,
                    "provider": provider,
                    "model_id": normalized_model,
                    "reason": "model_id není v posledním providerem nahlášeném katalogu; poznámka nebyla změněna.",
                    "available_model_ids": sorted(_catalog_index(catalog.get("models"))),
                    "info_file": str(self._info_path(provider)),
                }
            info.selected_model = normalized_model
            info.selection_mode = "FORCED"
            info.selection_source = normalized_source
            info.selection_updated_at = _now()
        else:
            raise ValueError("selection mode musí být FORCED nebo AUTO.")
        self._save_info(info)
        return {
            "command": SET_PROVIDER_MODEL_QUERY,
            "success": True,
            "provider": provider,
            "model_id": info.selected_model,
            "selection_mode": info.selection_mode,
            "selection_source": info.selection_source,
            "selection_updated_at": info.selection_updated_at,
            "info_file": str(self._info_path(provider)),
        }

    def ask(self, query: str | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(query, Mapping):
            command = query.get("command")
            if command == SELECT_PROVIDER_QUERY and query.get("provider"):
                offer = self.select_named_provider(
                    str(query.get("provider")), query.get("task_profile")
                )
                return {
                    "command": SELECT_PROVIDER_QUERY,
                    "order": list(PROVIDER_ORDER),
                    "offer": offer.to_dict(),
                }
            if command == SELECT_PROVIDER_QUERY:
                raw_excluded = query.get("exclude_providers", [])
                excluded = {
                    str(provider).strip()
                    for provider in raw_excluded
                    if isinstance(provider, str) and provider.strip()
                } if isinstance(raw_excluded, list) else set()
                return {
                    "command": SELECT_PROVIDER_QUERY,
                    "order": list(PROVIDER_ORDER),
                    "offer": self.select_provider(
                        excluded=excluded, task_profile=query.get("task_profile")
                    ).to_dict(),
                }
            if command == SET_PROVIDER_MODEL_QUERY:
                return self.set_provider_model(
                    str(query.get("provider") or ""),
                    query.get("model_id"),
                    source=str(query.get("source") or "ao"),
                    mode=str(query.get("mode") or "FORCED"),
                )
            if command == REPORT_PROVIDER_STATUS_QUERY:
                return self.report_provider_status(
                    str(query.get("provider") or ""),
                    query.get("status") if isinstance(query.get("status"), Mapping) else query,
                )
            raise ValueError(f"Neznámý příkaz brokeru {command!r}.")
        if query == REFRESH_PROVIDER_NOTES_QUERY:
            return self.refresh_provider_notes()
        if query != SELECT_PROVIDER_QUERY:
            raise ValueError(
                f"Neznámý dotaz brokeru {query!r}; očekává se {SELECT_PROVIDER_QUERY!r}."
            )
        return {
            "command": SELECT_PROVIDER_QUERY,
            "order": list(PROVIDER_ORDER),
            "offer": self.select_provider().to_dict(),
        }


def build_provider_broker(
    config: Any,
    logger: Optional[Callable[[str], None]] = None,
    agent_builder: Optional[Callable[[str, Any], Any]] = None,
) -> ProviderBroker:
    """Build the broker with exactly the four providers in its fixed order."""
    if agent_builder is None:
        from orchestrator.agents.antigravity import AntigravityAgent
        from orchestrator.agents.claude_code import ClaudeCodeAgent
        from orchestrator.agents.codex import CodexAgent
        from orchestrator.agents.groq import GroqAgent

        providers = [
            GroqAgent(config.groq),
            AntigravityAgent(config.antigravity),
            ClaudeCodeAgent(config.claude_code),
            CodexAgent(config.codex),
        ]
    else:
        providers = [agent_builder(provider, config) for provider in PROVIDER_ORDER]

    models = {
        "groq": getattr(config.groq, "model", None),
        "antigravity": getattr(config.antigravity, "model", None),
        "claude-code": getattr(config.claude_code, "model", None),
        "codex": getattr(config.codex, "model", None),
    }
    return ProviderBroker(
        providers=providers,
        models=models,
        info_dir=Path(config.data_dir) / "provider-info",
        logger=logger,
        lang_dir=Path(__file__).with_name("agents"),
    )
