"""Groq API implementation agent with a project-scoped local tool loop.

Groq is an API provider rather than a local coding CLI. To give it the same
implementation role as the CLI-backed providers without widening the trust
boundary, this adapter exposes only a small set of local tools implemented
here: project file listing/search/read/write/exact replacement and read-only
Git status/diff. There is deliberately no generic shell, test runner, commit,
push, delete, or path escape capability. Tests and commits remain exclusively
owned by the orchestrator pipeline.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

from orchestrator.agents.base import Agent, AgentRunRequest, AgentRunResult
from orchestrator.config import GROQ_FREE_MODEL, GroqAgentConfig

try:
    from groq import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        Groq,
        PermissionDeniedError,
        RateLimitError,
    )
except ImportError:  # pragma: no cover - exercised through is_available()
    Groq = None  # type: ignore[assignment]

    class _MissingGroqError(Exception):
        pass

    APIConnectionError = _MissingGroqError  # type: ignore[assignment]
    APIStatusError = _MissingGroqError  # type: ignore[assignment]
    APITimeoutError = _MissingGroqError  # type: ignore[assignment]
    AuthenticationError = _MissingGroqError  # type: ignore[assignment]
    PermissionDeniedError = _MissingGroqError  # type: ignore[assignment]
    RateLimitError = _MissingGroqError  # type: ignore[assignment]


NO_COMMIT_INSTRUCTION = (
    "Never create a Git commit, amend history, push, reset, rebase, or delete Git history. "
    "The orchestrator owns tests and commits after your implementation is finished."
)

_SYSTEM_PROMPT = """You are the Groq implementation provider inside ai-orchestrator.
Work only through the supplied project-scoped tools. Inspect the real checkout before editing.
Make minimal changes directly in the project to satisfy the user's task. For a small edit to
an existing file, prefer replace_text over rewriting the whole file with write_file. Tool
arguments must be valid JSON and must contain raw file content, never line-numbered read_file
output. Never claim a change without verifying it by reading the resulting file or Git diff.
Do not attempt to run tests;
the orchestrator runs them independently after you return. Do not attempt Git commit/push or
history rewriting. Never access paths outside the supplied project root. When the work is done,
return a concise final result. If the caller requires a JSON schema, a separate schema-constrained
finalization call will be made after tool use.
""" + NO_COMMIT_INSTRUCTION

_IGNORED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"}
_MAX_TOOL_TEXT = 6000
_MAX_TOOL_RESULT_JSON = 7000
# Keep every individual Groq request below a conservative free-tier budget.
# The provider's rolling TPM usage is not visible before a call, so this local
# ceiling prevents oversized requests while leaving Groq first in the
# failover order for smaller tasks.
_MAX_SAFE_REQUEST_TOKENS = 6000
_TOKEN_ESTIMATE_CHARS_PER_TOKEN = 3
_REQUEST_FIXED_OVERHEAD_TOKENS = 64
_MIN_OUTPUT_TOKENS = 256
_MAX_RETAINED_HISTORY_MESSAGES = 6


class _GroqWallClockTimeout(Exception):
    pass


class _GroqRequestBudgetExceeded(Exception):
    pass


def _tool_schema() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files/directories under a project-relative path.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Project-relative directory, default '.'"},
                        "max_entries": {"type": "integer", "minimum": 1, "maximum": 500},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read UTF-8 text from a project-relative file with optional line window.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "max_lines": {"type": "integer", "minimum": 1, "maximum": 250},
                        "line_start": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Alias for start_line.",
                        },
                        "line_end": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Inclusive end line; use with line_start.",
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_text",
                "description": "Literal text search across UTF-8 project files. No regex.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string", "description": "Project-relative file or directory, default '.'"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Create a new UTF-8 text file or fully replace one only when full replacement is intended. For small edits to existing files, use replace_text. Content must be raw file text without read_file line-number prefixes. Uses LF and no BOM.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "replace_text",
                "description": "Replace one exact unique text block in a UTF-8 project file. Fails unless the old block occurs exactly once.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old": {"type": "string"},
                        "new": {"type": "string"},
                    },
                    "required": ["path", "old", "new"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "git_status",
                "description": "Read-only git status --short for the project.",
                "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "git_diff",
                "description": "Read-only git --no-pager diff. Optionally restrict to one project-relative path.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _safe_path(project_path: Path, relative: str, *, must_exist: bool = False) -> Path:
    root = project_path.resolve()
    raw = Path(relative or ".")
    if raw.is_absolute():
        raise ValueError("absolute paths are not allowed")
    candidate = (root / raw).resolve(strict=False)
    if candidate != root and root not in candidate.parents:
        raise ValueError("path escapes project root")
    relative_parts = candidate.relative_to(root).parts if candidate != root else ()
    if ".git" in relative_parts:
        raise ValueError("access to .git internals is forbidden")
    if must_exist and not candidate.exists():
        raise FileNotFoundError(f"path does not exist: {relative}")
    return candidate


def _normalize_text(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _is_probably_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(4096)
    except OSError:
        return False


def _tool_list_files(project_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    target = _safe_path(project_path, str(args.get("path", ".")), must_exist=True)
    if not target.is_dir():
        raise ValueError("path is not a directory")
    max_entries = min(500, max(1, int(args.get("max_entries", 200))))
    entries: list[str] = []
    for child in sorted(target.rglob("*"), key=lambda p: str(p).casefold()):
        relative_parts = child.relative_to(project_path.resolve()).parts
        if any(part in _IGNORED_DIRS for part in relative_parts):
            continue
        suffix = "/" if child.is_dir() else ""
        entries.append(child.relative_to(project_path.resolve()).as_posix() + suffix)
        if len(entries) >= max_entries:
            break
    return {"entries": entries, "truncated": len(entries) >= max_entries}


def _tool_read_file(project_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    target = _safe_path(project_path, str(args["path"]), must_exist=True)
    if not target.is_file():
        raise ValueError("path is not a file")
    if _is_probably_binary(target):
        raise ValueError("binary files are not supported")
    text = target.read_text(encoding="utf-8")
    lines = text.splitlines()

    has_native_window = "start_line" in args or "max_lines" in args
    has_alias_window = "line_start" in args or "line_end" in args
    if has_native_window and has_alias_window:
        raise ValueError("read_file line window must use either start_line/max_lines or line_start/line_end")

    if has_alias_window:
        start = max(1, int(args.get("line_start", 1)))
        end = int(args.get("line_end", start + 399))
        if end < start:
            raise ValueError("line_end must be greater than or equal to line_start")
        max_lines = min(250, end - start + 1)
    else:
        start = max(1, int(args.get("start_line", 1)))
        max_lines = min(250, max(1, int(args.get("max_lines", 120))))

    selected = lines[start - 1 : start - 1 + max_lines]
    rendered = "\n".join(f"{start + i}: {line}" for i, line in enumerate(selected))
    return {
        "path": target.relative_to(project_path.resolve()).as_posix(),
        "text": rendered[:_MAX_TOOL_TEXT],
        "total_lines": len(lines),
        "truncated": len(rendered) > _MAX_TOOL_TEXT or start - 1 + max_lines < len(lines),
    }


def _iter_search_files(target: Path):
    if target.is_file():
        yield target
        return
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(target).parts if target.is_dir() else path.parts
        if any(part in _IGNORED_DIRS for part in rel_parts):
            continue
        yield path


def _tool_search_text(project_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args["query"])
    if not query:
        raise ValueError("query must not be empty")
    target = _safe_path(project_path, str(args.get("path", ".")), must_exist=True)
    max_results = min(200, max(1, int(args.get("max_results", 80))))
    results: list[dict[str, Any]] = []
    root = project_path.resolve()
    for path in _iter_search_files(target):
        if _is_probably_binary(path):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(lines, 1):
            if query in line:
                results.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "line": line_no,
                        "text": line[:500],
                    }
                )
                if len(results) >= max_results:
                    return {"results": results, "truncated": True}
    return {"results": results, "truncated": False}


def _tool_write_file(project_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    target = _safe_path(project_path, str(args["path"]))
    if target.exists() and target.is_dir():
        raise ValueError("path is a directory")
    if target.exists() and _is_probably_binary(target):
        raise ValueError("refusing to overwrite a binary file")
    content = _normalize_text(str(args["content"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")
    return {
        "path": target.relative_to(project_path.resolve()).as_posix(),
        "chars_written": len(content),
    }


def _tool_replace_text(project_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    target = _safe_path(project_path, str(args["path"]), must_exist=True)
    if not target.is_file() or _is_probably_binary(target):
        raise ValueError("path must be a UTF-8 text file")
    old = str(args["old"])
    new = _normalize_text(str(args["new"]))
    if not old:
        raise ValueError("old text must not be empty")
    text = target.read_text(encoding="utf-8")
    occurrences = text.count(old)
    if occurrences != 1:
        raise ValueError(f"exact old block must occur once, found {occurrences}")
    updated = _normalize_text(text.replace(old, new, 1))
    target.write_text(updated, encoding="utf-8", newline="\n")
    return {"path": target.relative_to(project_path.resolve()).as_posix(), "replacements": 1}


def _run_git(project_path: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "--no-pager", *args],
        cwd=str(project_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    if proc.returncode != 0:
        raise RuntimeError(output or f"git exited with {proc.returncode}")
    return output[:_MAX_TOOL_TEXT]


def _tool_git_status(project_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    return {"output": _run_git(project_path, ["status", "--short"])}


def _tool_git_diff(project_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    command = ["diff"]
    raw_path = args.get("path")
    if raw_path:
        target = _safe_path(project_path, str(raw_path), must_exist=False)
        rel = target.relative_to(project_path.resolve()).as_posix()
        command += ["--", rel]
    return {"output": _run_git(project_path, command)}


_TOOL_HANDLERS = {
    "list_files": _tool_list_files,
    "read_file": _tool_read_file,
    "search_text": _tool_search_text,
    "write_file": _tool_write_file,
    "replace_text": _tool_replace_text,
    "git_status": _tool_git_status,
    "git_diff": _tool_git_diff,
}



def _bounded_tool_result_payload(result: Any) -> dict[str, Any]:
    """Return a valid JSON payload that cannot dominate later Groq requests."""
    payload = {"ok": True, "result": result}
    rendered = json.dumps(payload, ensure_ascii=False)
    if len(rendered) <= _MAX_TOOL_RESULT_JSON:
        return payload

    marker = "\n...[tool output truncated for Groq context budget]..."
    if isinstance(result, dict):
        compact = dict(result)
        for field in ("text", "output"):
            value = compact.get(field)
            if isinstance(value, str):
                available = max(0, _MAX_TOOL_RESULT_JSON - 500 - len(marker))
                compact[field] = value[:available] + marker
                compact["truncated"] = True
                payload = {"ok": True, "result": compact}
                if len(json.dumps(payload, ensure_ascii=False)) <= _MAX_TOOL_RESULT_JSON:
                    return payload

        for field in ("results", "entries"):
            value = compact.get(field)
            if isinstance(value, list):
                compact[field] = list(value)
                while compact[field]:
                    compact[field] = compact[field][:-1]
                    compact["truncated"] = True
                    payload = {"ok": True, "result": compact}
                    if len(json.dumps(payload, ensure_ascii=False)) <= _MAX_TOOL_RESULT_JSON:
                        return payload

    return {
        "ok": True,
        "result": {
            "truncated": True,
            "summary": "Tool output omitted because it exceeded the Groq context budget.",
        },
    }


def _compact_message_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the task and newest complete tool exchanges for the next request."""
    if len(messages) <= _MAX_RETAINED_HISTORY_MESSAGES + 3:
        return messages

    prefix = messages[:2]
    tail = messages[-_MAX_RETAINED_HISTORY_MESSAGES:]
    while tail and tail[0].get("role") == "tool":
        tail.pop(0)
    omitted = max(0, len(messages) - len(prefix) - len(tail))
    marker = {
        "role": "user",
        "content": (
            "Earlier Groq conversation history was compacted to stay within the request budget; "
            f"{omitted} older message(s) were omitted. Re-read project files if their details are needed."
        ),
    }
    return [*prefix, marker, *tail]


def _estimate_request_tokens(
    messages: list[dict[str, Any]],
    *,
    tools: Optional[list[dict[str, Any]]] = None,
    response_format: Optional[dict[str, Any]] = None,
    max_tokens: int = 0,
) -> int:
    """Conservatively estimate one serialized Groq request before sending it."""
    payload: dict[str, Any] = {"messages": messages}
    if tools is not None:
        payload["tools"] = tools
    if response_format is not None:
        payload["response_format"] = response_format
    rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    character_estimate = (
        len(rendered) + _TOKEN_ESTIMATE_CHARS_PER_TOKEN - 1
    ) // _TOKEN_ESTIMATE_CHARS_PER_TOKEN
    return (
        character_estimate
        + len(messages) * 4
        + _REQUEST_FIXED_OVERHEAD_TOKENS
        + max(0, int(max_tokens))
    )


def _execute_tool(project_path: Path, name: str, raw_arguments: str) -> str:
    try:
        args = json.loads(raw_arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be a JSON object")
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            raise ValueError(f"unknown tool: {name}")
        result = handler(project_path, args)
        return json.dumps(_bounded_tool_result_payload(result), ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - tool errors are data for the model
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def _message_dict(message: Any) -> dict[str, Any]:
    if hasattr(message, "model_dump"):
        data = message.model_dump(exclude_none=True)
        if isinstance(data, dict):
            return data
    result: dict[str, Any] = {
        "role": getattr(message, "role", "assistant"),
        "content": getattr(message, "content", None),
    }
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        serialized = []
        for call in tool_calls:
            serialized.append(
                {
                    "id": getattr(call, "id", ""),
                    "type": "function",
                    "function": {
                        "name": getattr(getattr(call, "function", None), "name", ""),
                        "arguments": getattr(getattr(call, "function", None), "arguments", "{}"),
                    },
                }
            )
        result["tool_calls"] = serialized
    return result


def _response_dict(response: Any) -> Optional[dict[str, Any]]:
    if hasattr(response, "model_dump"):
        value = response.model_dump(exclude_none=True)
        return value if isinstance(value, dict) else None
    return None


def _usage_values(response: Any) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None, None, None
    input_tokens = getattr(usage, "prompt_tokens", None)
    output_tokens = getattr(usage, "completion_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    thinking_tokens = None
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        thinking_tokens = getattr(details, "reasoning_tokens", None)
    return input_tokens, output_tokens, thinking_tokens, total_tokens


def _add_usage(total: dict[str, int], response: Any) -> None:
    input_tokens, output_tokens, thinking_tokens, total_tokens = _usage_values(response)
    for key, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("thinking_tokens", thinking_tokens),
        ("total_tokens", total_tokens),
    ):
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value




def _malformed_tool_arguments_failure(exc: Exception) -> bool:
    """Recognize Groq rejecting a model-emitted tool call before message delivery."""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        response = getattr(exc, "response", None)
        json_method = getattr(response, "json", None)
        if callable(json_method):
            try:
                body = json_method()
            except Exception:  # noqa: BLE001 - provider diagnostics are best-effort
                body = None
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    if not isinstance(error, dict) or error.get("code") != "tool_use_failed":
        return False
    message = str(error.get("message") or "")
    return "failed to parse tool call arguments as json" in message.casefold()




def _tool_schema_validation_failure(exc: Exception) -> bool:
    """Recognize Groq rejecting valid JSON tool arguments that violate the declared schema."""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        response = getattr(exc, "response", None)
        json_method = getattr(response, "json", None)
        if callable(json_method):
            try:
                body = json_method()
            except Exception:  # noqa: BLE001 - provider diagnostics are best-effort
                body = None
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    if not isinstance(error, dict) or error.get("code") != "tool_use_failed":
        return False
    message = str(error.get("message") or "").casefold()
    return "parameters for tool" in message and "did not match schema" in message


def _output_parse_failure(exc: Exception) -> bool:
    """Recognize Groq rejecting free-form text when a tool call was expected."""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        response = getattr(exc, "response", None)
        json_method = getattr(response, "json", None)
        if callable(json_method):
            try:
                body = json_method()
            except Exception:  # noqa: BLE001 - provider diagnostics are best-effort
                body = None
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    if not isinstance(error, dict):
        return False
    return str(error.get("code") or "").casefold() == "output_parse_failed"


def _schema_finalization_tool_failure(
    exc: Exception,
    output_schema: Optional[dict[str, Any]],
) -> bool:
    """Return True when Groq encoded the requested final JSON as a fake tool call.

    Some OpenAI-compatible models can respond to a tool-enabled turn by wrapping
    the requested final JSON object in an invented function call. Groq rejects
    that response before returning a message because the function is not in the
    supplied tool list. When the invented call's arguments match the caller's
    output schema at the top level, the safe recovery is to leave the project
    tool loop and use the existing tool-free, schema-constrained finalization
    call.
    """
    if not isinstance(output_schema, dict):
        return False

    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        response = getattr(exc, "response", None)
        json_method = getattr(response, "json", None)
        if callable(json_method):
            try:
                body = json_method()
            except Exception:  # noqa: BLE001 - provider error payload is best-effort diagnostics
                body = None
    if not isinstance(body, dict):
        return False

    error = body.get("error")
    if not isinstance(error, dict) or error.get("code") != "tool_use_failed":
        return False
    failed_generation = error.get("failed_generation")
    if not isinstance(failed_generation, str):
        return False

    try:
        generation = json.loads(failed_generation)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(generation, dict):
        return False

    arguments = generation.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
    if not isinstance(arguments, dict):
        return False

    required = output_schema.get("required")
    if isinstance(required, list):
        required_keys = {str(key) for key in required}
        if not required_keys.issubset(arguments):
            return False

    properties = output_schema.get("properties")
    if output_schema.get("additionalProperties") is False and isinstance(properties, dict):
        if not set(arguments).issubset(properties):
            return False

    return True



def _schema_finalization_instructions(output_schema: dict[str, Any]) -> str:
    """Render concise prompt constraints that models must obey in addition to response_format."""
    instructions = ["Follow the JSON schema exactly."]
    properties = output_schema.get("properties")
    if isinstance(properties, dict):
        for name, definition in properties.items():
            if not isinstance(definition, dict) or definition.get("type") != "array":
                continue
            minimum = definition.get("minItems")
            maximum = definition.get("maxItems")
            if isinstance(minimum, int) and isinstance(maximum, int) and minimum == maximum:
                instructions.append(f"Array `{name}` must contain exactly {minimum} item(s).")
            elif isinstance(maximum, int):
                instructions.append(f"Array `{name}` must contain at most {maximum} item(s).")
            elif isinstance(minimum, int):
                instructions.append(f"Array `{name}` must contain at least {minimum} item(s).")
    return " ".join(instructions)



def _provider_rate_limit_failure(exc: Exception) -> bool:
    """Recognize Groq provider quota/rate-limit errors even when HTTP status is not 429."""
    if isinstance(exc, RateLimitError) or getattr(exc, "status_code", None) == 429:
        return True

    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        response = getattr(exc, "response", None)
        json_method = getattr(response, "json", None)
        if callable(json_method):
            try:
                body = json_method()
            except Exception:  # noqa: BLE001 - provider diagnostics are best-effort
                body = None
    if not isinstance(body, dict):
        return False

    error = body.get("error")
    if not isinstance(error, dict):
        return False
    return str(error.get("code") or "").casefold() == "rate_limit_exceeded"


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class GroqAgent(Agent):
    name = "groq"

    def __init__(self, config: GroqAgentConfig):
        self.config = config
        if config.free_only and config.model != GROQ_FREE_MODEL:
            raise ValueError(
                f"groq.free_only dovoluje pouze ověřený free model {GROQ_FREE_MODEL!r}; "
                f"nakonfigurováno {config.model!r}."
            )
        self._client = None
        self._sessions: dict[str, list[dict[str, Any]]] = {}

    def is_available(self) -> tuple[bool, str]:
        if Groq is None:
            return False, "Python balíček 'groq' není nainstalovaný."
        if not os.environ.get("GROQ_API_KEY", "").strip():
            return False, "Chybí proměnná prostředí GROQ_API_KEY."
        return True, f"Groq API připraveno (model {self.config.model}, free_only={self.config.free_only})."

    def _effective_model(self, request: AgentRunRequest) -> tuple[Optional[str], Optional[str], Optional[str]]:
        requested = (request.requested_model or "").strip()
        if requested:
            if self.config.free_only and requested != GROQ_FREE_MODEL:
                return None, None, (
                    f"Groq free-only režim odmítl requested_model={requested!r}; "
                    f"povolen je pouze {GROQ_FREE_MODEL!r}."
                )
            return requested, "requested", None
        return self.config.model, "configured", None

    def _client_for(self, timeout_seconds: float):
        if self._client is None:
            self._client = Groq(
                api_key=os.environ.get("GROQ_API_KEY"),
                timeout=max(1.0, float(timeout_seconds)),
            )
        with_options = getattr(self._client, "with_options", None)
        if callable(with_options):
            return with_options(timeout=max(1.0, float(timeout_seconds)))
        return self._client

    def _create_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        remaining_seconds: float,
        tools: Optional[list[dict[str, Any]]] = None,
        response_format: Optional[dict[str, Any]] = None,
    ):
        bounded_messages = _compact_message_history(messages)
        messages[:] = bounded_messages
        input_tokens = _estimate_request_tokens(
            bounded_messages,
            tools=tools,
            response_format=response_format,
        )
        configured_output_tokens = max(1, int(self.config.max_output_tokens))
        available_output_tokens = _MAX_SAFE_REQUEST_TOKENS - input_tokens
        minimum_output_tokens = min(_MIN_OUTPUT_TOKENS, configured_output_tokens)
        if available_output_tokens < minimum_output_tokens:
            raise _GroqRequestBudgetExceeded(
                "Groq request odhadnut na nejméně "
                f"{input_tokens + minimum_output_tokens} tokenů přesahuje bezpečný limit "
                f"{_MAX_SAFE_REQUEST_TOKENS} tokenů."
            )
        effective_output_tokens = min(configured_output_tokens, available_output_tokens)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "service_tier": "on_demand",
            "reasoning_effort": self.config.reasoning_effort,
            "include_reasoning": False,
            "max_tokens": effective_output_tokens,
        }
        if tools is not None:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
            kwargs["parallel_tool_calls"] = False
        if response_format is not None:
            kwargs["response_format"] = response_format
        client = self._client_for(remaining_seconds)
        return client.chat.completions.create(**kwargs)

    def _failure_result(
        self,
        exc: Exception,
        request: AgentRunRequest,
        model: Optional[str],
        model_source: Optional[str],
        session_id: Optional[str],
        usage: dict[str, int],
    ) -> AgentRunResult:
        common = dict(
            success=False,
            output_text="",
            session_id=session_id,
            model=model,
            model_source=model_source,
            selection_reason=request.selection_reason,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            thinking_tokens=usage.get("thinking_tokens"),
            total_tokens=usage.get("total_tokens"),
        )
        if isinstance(exc, _GroqRequestBudgetExceeded):
            return AgentRunResult(
                **common,
                error=f"LIMITED: {exc}",
                limited=True,
            )
        if _provider_rate_limit_failure(exc):
            return AgentRunResult(
                **common,
                error=f"LIMITED: {exc}",
                limited=True,
                retry_after_seconds=_retry_after_seconds(exc),
            )
        if isinstance(exc, (APITimeoutError, _GroqWallClockTimeout)):
            return AgentRunResult(**common, error=str(exc), timed_out=True)
        if isinstance(exc, (AuthenticationError, PermissionDeniedError, APIConnectionError)):
            return AgentRunResult(**common, error=str(exc), unavailable=True)
        return AgentRunResult(**common, error=str(exc))

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        available, message = self.is_available()
        if not available:
            return AgentRunResult(
                success=False,
                output_text="",
                error=message,
                unavailable=True,
                selection_reason=request.selection_reason,
            )

        model, model_source, model_error = self._effective_model(request)
        if model_error or model is None:
            return AgentRunResult(
                success=False,
                output_text="",
                error=model_error or "Groq model není nakonfigurován.",
                model=model,
                model_source=model_source,
                selection_reason=request.selection_reason,
            )

        session_id = request.session_id or uuid.uuid4().hex
        prior = self._sessions.get(session_id)
        if prior is None:
            messages: list[dict[str, Any]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
        else:
            messages = [dict(item) for item in prior]

        prompt = request.prompt
        if request.context:
            prompt += "\n\nAdditional context from the orchestrator:\n" + request.context
        messages.append({"role": "user", "content": prompt})

        started = time.monotonic()
        usage: dict[str, int] = {}
        last_response = None
        final_content = ""

        try:
            for _ in range(self.config.max_tool_rounds):
                remaining = self.config.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise _GroqWallClockTimeout(
                        f"Groq překročil celkový timeout {self.config.timeout_seconds}s."
                    )
                try:
                    response = self._create_completion(
                        messages=messages,
                        model=model,
                        remaining_seconds=remaining,
                        tools=_tool_schema(),
                    )
                except Exception as exc:
                    if _schema_finalization_tool_failure(exc, request.output_schema):
                        final_content = ""
                        break
                    if _malformed_tool_arguments_failure(exc):
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Your previous tool call arguments were rejected as invalid JSON. "
                                    "Retry with one valid tool call. For a small edit to an existing file, "
                                    "use replace_text with exact raw text; do not copy read_file line numbers "
                                    "into file content and do not rewrite the whole file unless required."
                                ),
                            }
                        )
                        continue
                    if _tool_schema_validation_failure(exc):
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Your previous tool call arguments violated the supplied tool schema. "
                                    "Retry with exactly one valid supplied tool call and obey every declared "
                                    "argument bound. For read_file, max_lines must be at most 250."
                                ),
                            }
                        )
                        continue
                    if _output_parse_failure(exc):
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Your previous response was rejected because this turn requires a tool call, "
                                    "not free-form prose. Retry with exactly one valid supplied tool call. "
                                    "Do not narrate your plan before the tool call."
                                ),
                            }
                        )
                        continue
                    raise
                last_response = response
                _add_usage(usage, response)
                choice = response.choices[0]
                assistant_message = choice.message
                messages.append(_message_dict(assistant_message))
                tool_calls = getattr(assistant_message, "tool_calls", None) or []
                if not tool_calls:
                    final_content = getattr(assistant_message, "content", None) or ""
                    break
                for tool_call in tool_calls:
                    function = getattr(tool_call, "function", None)
                    name = getattr(function, "name", "")
                    arguments = getattr(function, "arguments", "{}")
                    result_text = _execute_tool(request.project_path, name, arguments)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": getattr(tool_call, "id", ""),
                            "name": name,
                            "content": result_text,
                        }
                    )
            else:
                self._sessions[session_id] = messages
                return AgentRunResult(
                    success=False,
                    output_text="",
                    error=f"Groq překročil max_tool_rounds={self.config.max_tool_rounds} bez finální odpovědi.",
                    session_id=session_id,
                    model=getattr(last_response, "model", model) if last_response else model,
                    model_source="reported" if last_response and getattr(last_response, "model", None) else model_source,
                    selection_reason=request.selection_reason,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    thinking_tokens=usage.get("thinking_tokens"),
                    total_tokens=usage.get("total_tokens"),
                )

            if request.output_schema is not None:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Return the final result for the orchestrator now. Do not call tools. "
                            "Base it on the work and verification already performed in this conversation. "
                            + _schema_finalization_instructions(request.output_schema)
                        ),
                    }
                )
                remaining = self.config.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise _GroqWallClockTimeout(
                        f"Groq překročil celkový timeout {self.config.timeout_seconds}s."
                    )
                response = self._create_completion(
                    messages=messages,
                    model=model,
                    remaining_seconds=remaining,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "orchestrator_result",
                            "strict": True,
                            "schema": request.output_schema,
                        },
                    },
                )
                last_response = response
                _add_usage(usage, response)
                assistant_message = response.choices[0].message
                messages.append(_message_dict(assistant_message))
                final_content = getattr(assistant_message, "content", None) or ""

            self._sessions[session_id] = messages
            reported_model = getattr(last_response, "model", None) if last_response else None
            return AgentRunResult(
                success=True,
                output_text=final_content,
                raw_response=_response_dict(last_response) if last_response else None,
                session_id=session_id,
                model=reported_model or model,
                model_source="reported" if reported_model else model_source,
                selection_reason=request.selection_reason,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                thinking_tokens=usage.get("thinking_tokens"),
                total_tokens=usage.get("total_tokens"),
            )
        except Exception as exc:  # expected provider/network failures are normalized below
            self._sessions[session_id] = messages
            return self._failure_result(exc, request, model, model_source, session_id, usage)
