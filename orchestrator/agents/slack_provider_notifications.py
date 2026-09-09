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


def _usage_from_result(result: Any) -> list[dict[str, Any]]:
    """Use the exact in-memory values that the usage ledger just persisted."""
    model = getattr(result, "model", None)
    return [
        {
            "model": model.strip() if isinstance(model, str) and model.strip() else "",
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


def _format_cost(value: int | float) -> str:
    if value == 0:
        return "0"
    return f"{value:.6f}".rstrip("0").rstrip(".")


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
        messages.append(
            f"[{timestamp}] provider: `{provider}`{model_part} | "
            f"úkol: {_task_text(task)} | "
            f"stav: `{_status(result)}` | "
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
