"""v2 task-driven model routing shared by AO dispatch and the broker.

The workflow phase is retained as context only.  The routing decision is
derived from the concrete task text and its Definition of Done, then the
broker resolves the selected tier to an exact model ID from its provider note.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional


_CODE_WORDS = (
    "implement", "implementation", "implementovat", "oprav", "oprava",
    "kód", "code", "soubor", "file", "test", "pytest", "refactor",
    "funkc", "api", "integrat", "config", "konfigur",
)
_RESEARCH_WORDS = (
    "research", "rešer", "rešerš", "prověř", "prozkoum", "zjisti",
    "analyz", "porovnej", "compare", "investigat", "dokument",
)
_EVIDENCE_WORDS = (
    "důkaz", "evidence", "ověř", "ověření", "verify", "audit", "test",
    "snapshot", "readback",
)
_COMPLEX_WORDS = (
    "komplex", "složit", "podstatně", "hloub", "end-to-end", "e2e",
    "vícevrstv", "architecture", "architektur", "migrat", "bezpečnost",
)


def _contains_any(text: str, words: Iterable[str]) -> bool:
    return any(word in text for word in words)


def _items_text(items: Optional[Iterable[Any]]) -> str:
    values: list[str] = []
    for item in items or ():
        value = item.text if hasattr(item, "text") else item
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
        elif isinstance(item, Mapping):
            value = item.get("text") or item.get("task")
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
    return "\n".join(values)


def derive_task_profile(
    prompt: str,
    *,
    definition_of_done: Optional[Iterable[Any]] = None,
    workflow_phase: Optional[str] = None,
) -> dict[str, Any]:
    """Derive a small, deterministic profile from the actual task.

    This is deliberately a classifier, not an LLM call and not a phase-to-
    model table.  It provides explainable hints to the broker; the broker
    remains the only component allowed to turn a hint into an exact model ID.
    """
    prompt_text = str(prompt or "").strip()
    dod_text = _items_text(definition_of_done)
    text = f"{prompt_text}\n{dod_text}".casefold()
    length = len(prompt_text) + len(dod_text)
    dod_count = sum(1 for line in dod_text.splitlines() if line.strip())
    needs_code_changes = _contains_any(text, _CODE_WORDS)
    research = _contains_any(text, _RESEARCH_WORDS) and not needs_code_changes
    evidence = _contains_any(text, _EVIDENCE_WORDS)
    complexity_signals = sum(
        (
            length >= 1800,
            length >= 900,
            dod_count >= 6,
            _contains_any(text, _COMPLEX_WORDS),
            text.count("\n") >= 12,
        )
    )
    if complexity_signals >= 2 or length >= 2600:
        complexity = "complex"
    elif complexity_signals >= 1 or length >= 700 or dod_count >= 3:
        complexity = "medium"
    else:
        complexity = "simple"

    if complexity == "complex":
        model_tier = "strong"
    elif complexity == "medium":
        model_tier = "balanced"
    else:
        model_tier = "fast"

    if research and complexity == "complex":
        selection_reason = "complex research task from concrete task content"
    elif needs_code_changes and complexity == "complex":
        selection_reason = "complex code-changing task from concrete task content"
    elif research:
        selection_reason = "research task from concrete task content"
    elif needs_code_changes:
        selection_reason = "code-changing task from concrete task content"
    elif evidence:
        selection_reason = "evidence task from concrete task content"
    else:
        selection_reason = "task size and concrete content"

    profile = {
        "source": "ao_task_content",
        "work_type": "research" if research else ("implementation" if needs_code_changes else "general"),
        "complexity": complexity,
        "model_tier": model_tier,
        "needs_code_changes": needs_code_changes,
        "evidence_task": evidence,
        "selection_reason": selection_reason,
        "prompt_chars": len(prompt_text),
        "dod_items": dod_count,
    }
    if isinstance(workflow_phase, str) and workflow_phase.strip():
        profile["workflow_phase"] = workflow_phase.strip()
    return profile


def normalize_task_profile(value: Any) -> dict[str, Any]:
    """Accept only small scalar routing hints from an AO caller."""
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "source", "work_type", "complexity", "model_tier", "needs_code_changes",
        "evidence_task", "selection_reason", "prompt_chars", "dod_items",
        "workflow_phase",
    }
    result: dict[str, Any] = {}
    for key in allowed:
        item = value.get(key)
        if isinstance(item, (str, bool, int, float)):
            result[key] = item
    return result


def profile_summary(profile: Mapping[str, Any]) -> str:
    """Stable one-line explanation suitable for logs and broker offers."""
    fields = (
        ("work_type", profile.get("work_type")),
        ("complexity", profile.get("complexity")),
        ("tier", profile.get("model_tier")),
    )
    return ", ".join(f"{key}={value}" for key, value in fields if value)
