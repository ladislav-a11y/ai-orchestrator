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
