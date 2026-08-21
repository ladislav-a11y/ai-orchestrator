"""Logging setup: one rotating orchestrator-wide log, plus a per-task log file."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False


def setup_logging(logs_dir: Path, verbose: bool = False) -> logging.Logger:
    global _configured
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("orchestrator")
    if _configured:
        return logger

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(
        logs_dir / "orchestrator.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    _configured = True
    return logger


def task_log_path(logs_dir: Path, task_id: str) -> Path:
    d = logs_dir / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{task_id}.log"


def write_task_log(logs_dir: Path, task) -> Path:
    """Write a full human-readable transcript of one task. Returns the path written."""
    path = task_log_path(logs_dir, task.id)
    lines = [
        f"Task {task.id}",
        f"projekt: {task.project} ({task.project_path})",
        f"agent: {task.agent}",
        f"vytvořeno: {task.created_at}  aktualizováno: {task.updated_at}",
        f"stav: {task.status.value}  pokusů: {task.attempts}",
        "",
        "--- zadání ---",
        task.prompt,
        "",
        "--- výsledek agenta ---",
        task.result or "(žádný)",
    ]
    if task.test_command:
        lines += [
            "",
            f"--- testy ({task.test_command}) ---",
            f"prošly: {task.tests_passed}",
            task.test_output or "(žádný výstup)",
        ]
    if task.committed:
        lines += ["", "--- commit ---", f"hash: {task.commit_hash}"]
    if task.error:
        lines += ["", "--- chyba ---", task.error]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def autonomous_log_path(logs_dir: Path, run_id: str) -> Path:
    d = logs_dir / "autonomous"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{run_id}.log"


def write_autonomous_log(logs_dir: Path, run_id: str, project: str, goal: str, result) -> Path:
    """Write a full human-readable transcript of one autonomous run, every
    iteration included. Overwritten (not appended) after each iteration by the
    caller, so a run that crashes mid-way still leaves a full record on disk
    of every iteration completed so far."""
    path = autonomous_log_path(logs_dir, run_id)
    lines = [
        f"Autonomní běh {run_id}",
        f"projekt: {project}",
        f"cíl: {goal}",
        f"stav: {result.status.value}",
        "",
        "--- Definition of Done ---",
    ]
    for i, item in enumerate(result.dod_items):
        mark = "[x]" if item.done else "[ ]"
        lines.append(f"{mark} {i}. {item.text}")

    for it in result.iterations:
        lines += [
            "",
            f"=== iterace {it.index} ===",
            f"testy prošly: {it.tests_passed}  |  dávka: {it.requested_indices}  |  "
            f"prompt: {it.prompt_chars} znaků (~{it.prompt_chars // 4} tokenů)  |  "
            f"protokolová chyba: {it.protocol_error}  |  repair pokus: {it.repair_attempted}"
            f"{' (uspěl)' if it.repair_succeeded else ''}  |  audit proveden: {it.audit_performed}"
            + (f"  |  audit odmítl: {it.audit_rejected_indices}" if it.audit_rejected_indices else ""),
            "--- zadání agentovi ---",
            it.prompt,
            "",
            "--- výstup agenta ---",
            it.agent_output or "(žádný)",
        ]
        if it.agent_error:
            lines += ["", "--- chyba ---", it.agent_error]
        if it.test_output:
            lines += ["", "--- výstup testů ---", it.test_output]
        if it.note:
            lines += ["", f"poznámka: {it.note}"]

    if result.committed:
        lines += ["", "--- commit ---", f"hash: {result.commit_hash}"]
    if result.error:
        lines += ["", "--- chyba běhu ---", result.error]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
