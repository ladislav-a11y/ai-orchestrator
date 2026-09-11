"""Provider-owned Slack notifications based on the provider run result.

This module deliberately has no broker dependency and does not read environment
variables.  A provider wrapper calls :func:`notify_provider_run` after the
usage wrapper has persisted ``usage_<provider>.json``.  The provider already
has the same exact model and usage values in its result, so the notification
uses that in-memory result and does not reopen the JSON file.

Slack delivery is best-effort.  A delivery failure must never change the
provider result returned to AO.  Slack success is determined by the API JSON
field ``ok``; an HTTP status code alone is never treated as proof of success.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from functools import wraps
import json
from pathlib import Path
from typing import Any, TypeVar
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SLACK_CHANNEL_ID = "C0BRZ6N7J3Y"  # #ai-status
SLACK_API_URL = "https://slack.com/api/chat.postMessage"
SLACK_TOKEN_FILE = "config/slack_bot_token.txt"
SLACK_TIMEOUT_SECONDS = 3.0
_T = TypeVar("_T")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _token(path: Path | None = None) -> str | None:
    token_path = path or (_repo_root() / SLACK_TOKEN_FILE)
    try:
        value = token_path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    return value or None


def _number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, value)


def _reported_model(result: Any) -> str:
    """Return only a model identity confirmed by the provider result.

    ``model`` is the primary provider-reported identity.  Some adapters keep
    the same exact identity separately as ``receipt_model`` when the provider
    returned it in the task receipt but omitted it from top-level metadata.
    As a final provider-response fallback, inspect only the response's own
    ``model``/single-entry ``modelUsage`` fields.  Never use
    ``requested_model`` here: Slack must not turn a request into evidence.
    """
    for value in (
        getattr(result, "model", None),
        getattr(result, "receipt_model", None),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()

    raw = getattr(result, "raw_response", None)
    if not isinstance(raw, Mapping):
        return ""

    direct_model = raw.get("model")
    if isinstance(direct_model, str) and direct_model.strip():
        return direct_model.strip()

    for value in (raw.get("result"), getattr(result, "output_text", None)):
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = value.strip()
        if candidate.startswith("```") and candidate.endswith("```"):
            lines = candidate.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            candidate = "\n".join(lines).strip()
        try:
            receipt = json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(receipt, Mapping):
            receipt_model = receipt.get("model")
            if isinstance(receipt_model, str) and receipt_model.strip():
                return receipt_model.strip()

    model_usage = raw.get("modelUsage")
    if isinstance(model_usage, Mapping) and len(model_usage) == 1:
        model_id = next(iter(model_usage), None)
        if isinstance(model_id, str) and model_id.strip():
            return model_id.strip()
    return ""


def _usage_from_result(result: Any) -> list[dict[str, Any]]:
    """Use the exact in-memory values that the usage ledger just persisted."""
    model = _reported_model(result)
    return [
        {
            "model": model,
            "input_tokens": _number(getattr(result, "input_tokens", None)),
            "output_tokens": _number(getattr(result, "output_tokens", None)),
            "thinking_tokens": _number(getattr(result, "thinking_tokens", None)),
            "total_tokens": _number(getattr(result, "total_tokens", None)),
            "cost_usd": _number(getattr(result, "cost_usd", None)),
        }
    ]


def _status(result: Any) -> str:
    if getattr(result, "success", False):
        return "completed"
    if getattr(result, "limited", False) or getattr(result, "token_budget_exceeded", False):
        return "limited"
    if getattr(result, "unavailable", False):
        return "unavailable"
    return "failed"


def _task_text(task: Any) -> str:
    if not isinstance(task, str) or not task.strip():
        return "neuvedený úkol"
    compact = " ".join(task.split()).replace("`", "'")
    if len(compact) > 1000:
        return compact[:997] + "..."
    return compact


def _reason_text(result: Any) -> str:
    """Return the provider's failure reason without exposing raw metadata."""
    error = getattr(result, "error", None)
    if isinstance(error, str) and error.strip():
        return _task_text(error)
    status = getattr(result, "provider_status", None)
    if isinstance(status, Mapping):
        reason = status.get("reason")
        if isinstance(reason, str) and reason.strip():
            return _task_text(reason)
    return "neuvedený důvod"


def _retry_text(result: Any) -> str:
    retry_after = getattr(result, "retry_after_seconds", None)
    if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
        return _format_cost(max(0, retry_after))
    return "neuvedeno"


def _format_cost(value: int | float) -> str:
    if value == 0:
        return "0"
    return f"{value:.6f}".rstrip("0").rstrip(".")


def format_provider_wait_message(
    *,
    project: str,
    task: str | None,
    reason: str | None,
    retry_after_seconds: int | float | None,
    auto_resume_active: bool,
    now: datetime | None = None,
) -> str:
    """Format an AO-owned notification when broker dispatch cannot start."""
    timestamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    retry = (
        f"{_format_cost(max(0, retry_after_seconds))} s"
        if isinstance(retry_after_seconds, (int, float))
        and not isinstance(retry_after_seconds, bool)
        else "neuvedeno"
    )
    resume = "aktivní" if auto_resume_active else "neaktivní"
    return (
        f"[{timestamp}] provider-broker | projekt: {_task_text(project)} | "
        f"úkol: {_task_text(task)} | stav: `waiting_for_provider` | "
        f"důvod: {_task_text(reason)} | retry za: {retry} | "
        f"auto-resume: `{resume}`"
    )


def notify_provider_wait(
    *,
    project: str,
    task: str | None,
    reason: str | None,
    retry_after_seconds: int | float | None,
    auto_resume_active: bool,
    token_path: Path | None = None,
) -> bool:
    """Send one broker-level wait notification without changing workflow state."""
    return _post(
        format_provider_wait_message(
            project=project,
            task=task,
            reason=reason,
            retry_after_seconds=retry_after_seconds,
            auto_resume_active=auto_resume_active,
        ),
        token_path=token_path,
    )


def format_messages(
    provider: str,
    result: Any,
    usage: Iterable[Mapping[str, Any]],
    *,
    task: str | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Format one concise human-readable message per usage model bucket."""
    timestamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    rows = list(usage)
    if not rows:
        rows = [
            {
                "model": "",
                "input_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0,
            }
        ]
    messages: list[str] = []
    for row in rows:
        model = row.get("model")
        model_part = f" | LLM: `{model}`" if isinstance(model, str) and model else ""
        status = _status(result)
        reason_part = ""
        if status in {"failed", "limited", "unavailable"}:
            reason_part = f" | důvod: {_reason_text(result)}"
            if status in {"limited", "unavailable"}:
                reason_part += f" | retry za: {_retry_text(result)} s"
        messages.append(
            f"[{timestamp}] provider: `{provider}`{model_part} | "
            f"úkol: {_task_text(task)} | "
            f"stav: `{status}`{reason_part} | "
            f"tokeny: input {_number(row.get('input_tokens'))}, "
            f"output {_number(row.get('output_tokens'))}, "
            f"thinking {_number(row.get('thinking_tokens'))}, "
            f"celkem {_number(row.get('total_tokens'))} | "
            f"cena: {_format_cost(_number(row.get('cost_usd')))} USD"
        )
    return messages


def _post(message: str, *, token_path: Path | None = None) -> bool:
    token = _token(token_path)
    if token is None:
        return False
    request = Request(
        SLACK_API_URL,
        data=json.dumps({"channel": SLACK_CHANNEL_ID, "text": message}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=SLACK_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload, Mapping) and payload.get("ok") is True


def notify_provider_result(
    provider: str,
    result: Any,
    *,
    task: str | None = None,
    token_path: Path | None = None,
) -> bool:
    """Send one provider-run notification without rereading the usage JSON."""
    messages = format_messages(
        provider,
        result,
        _usage_from_result(result),
        task=task,
    )
    delivered = True
    for message in messages:
        delivered = _post(message, token_path=token_path) and delivered
    return delivered


def notify_provider_run(provider: str):
    """Decorator to notify after ``record_provider_run`` has persisted usage."""
    def decorate(run: Callable[..., _T]):
        @wraps(run)
        def wrapped(self: Any, request: Any, *args: Any, **kwargs: Any) -> _T:
            result = run(self, request, *args, **kwargs)
            try:
                notify_provider_result(
                    provider,
                    result,
                    task=getattr(request, "prompt", None),
                )
            except Exception:
                # Slack is observability only; never alter the provider result.
                pass
            return result

        return wrapped

    return decorate
