"""Offer-only broker for the four currently configured providers.

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
* AO vrací jméno providera, model, stav, důvod a cestu k poznámce;
* nepřijímá pracovní úkol a nikdy nevolá pracovní ``run()`` providera.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


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
USER_MODEL_SELECTION_SOURCE = "user"
VALID_STATES = {"AVAILABLE", "UNAVAILABLE", "UNKNOWN", "ERROR"}
MODEL_CATALOG_STATES = {"REPORTED", "UNKNOWN", "ERROR"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)
    return value


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

    def _model_selection_contract(self, provider: str) -> dict[str, Any]:
        path = self._lang_path(provider)
        try:
            contract = json.loads(path.read_text(encoding="utf-8"))
            selection = contract.get("model_selection")
            return dict(selection) if isinstance(selection, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            return {}

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
        configured_model = self.models.get(provider)
        if isinstance(configured_model, str) and configured_model.strip():
            info.model = configured_model.strip()
        elif not isinstance(info.model, str) or not info.model.strip():
            info.model = None
        probe_model = probe.get("model") if isinstance(probe, dict) else None
        if isinstance(probe_model, str) and probe_model.strip():
            info.model = probe_model.strip()
        info.model_source = probe.get("model_source") if isinstance(probe, dict) else None
        info.probe_kind = probe.get("probe_kind") if isinstance(probe, dict) else None
        info.usage = probe.get("usage") if isinstance(probe, dict) else None
        info.checked_at = _now()
        available = probe.get("available") if isinstance(probe, dict) else None
        response = probe.get("response") if isinstance(probe, dict) else None
        info.full_response = probe.get("full_response", probe) if isinstance(probe, dict) else probe
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

    def _refresh_model_catalog(self, provider: str, info: ProviderInfo) -> ProviderInfo:
        catalog_probe = getattr(self.providers[provider], "list_models", None)
        previous = info.model_catalog
        checked_at = _now()
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
        reported = state == "REPORTED"
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
            "models_source": "last_known" if retained_previous else ("provider_catalog" if reported else ("last_known" if models else None)),
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

    def _offer(self, info: ProviderInfo) -> ProviderOffer:
        if info.state == "AVAILABLE":
            forced = info.selection_mode == "FORCED" and info.selected_model
            return ProviderOffer(
                provider=info.provider,
                model=info.selected_model if forced else info.model,
                model_source="forced" if forced else info.model_source,
                selection_mode="FORCED" if forced else "AUTO",
                state=info.state,
                reason=info.reason or "Provider je dostupný.",
                info_file=str(self._info_path(info.provider)),
            )
        return ProviderOffer(
            provider=None,
            model=None,
            model_source=None,
            selection_mode=info.selection_mode,
            state=info.state,
            reason=info.reason or f"Provider není dostupný: {info.state}.",
            info_file=str(self._info_path(info.provider)),
        )

    def _first_available(self, infos: Mapping[str, ProviderInfo]) -> ProviderOffer:
        for provider in PROVIDER_ORDER:
            info = infos[provider]
            if info.state == "AVAILABLE":
                return self._offer(info)
        return ProviderOffer(
            provider=None,
            model=None,
            model_source=None,
            selection_mode="AUTO",
            state="NONE_AVAILABLE",
            reason="Žádný provider nemá stav AVAILABLE.",
        )

    def select_provider(self) -> ProviderOffer:
        infos: dict[str, ProviderInfo] = {}
        for provider in PROVIDER_ORDER:
            info, valid = self._load_info(provider)
            if not valid or info.state in {"UNKNOWN", "ERROR"}:
                info = self._probe(provider)
            infos[provider] = info
            if info.state == "AVAILABLE":
                return self._offer(info)
        return self._first_available(infos)

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
            if command == SET_PROVIDER_MODEL_QUERY:
                return self.set_provider_model(
                    str(query.get("provider") or ""),
                    query.get("model_id"),
                    source=str(query.get("source") or "ao"),
                    mode=str(query.get("mode") or "FORCED"),
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
