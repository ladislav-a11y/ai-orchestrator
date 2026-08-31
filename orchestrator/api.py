"""Local-only HTTP API.

Binds to 127.0.0.1 by default (config.api.host is validated at load time to
never be anything else). Not exposed to the internet. Intended as the future
attachment point for an external bridge (e.g. sending tasks from ChatGPT)
without that bridge needing to know anything about the CLI or the queue.
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from orchestrator.models import TaskStatus
from orchestrator.service import OrchestratorService

app = FastAPI(title="ai-orchestrator", version="0.1.0")
_service: Optional[OrchestratorService] = None


def get_service() -> OrchestratorService:
    global _service
    if _service is None:
        # This process blocks forever in uvicorn.run() (see serve() below),
        # so its OrchestratorService's internal waiting worker thread will
        # actually get a chance to resume a WAITING_FOR_PROVIDER task on its
        # own - unlike the CLI's one-shot `autonomous`/`run` commands, which
        # exit right after their single task finishes. See
        # OrchestratorService.__init__'s `persistent` parameter.
        _service = OrchestratorService(persistent=True)
    return _service


class NewTaskRequest(BaseModel):
    project: str
    prompt: str
    agent: Optional[str] = None
    test_command: Optional[str] = None
    auto_commit: Optional[bool] = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/tasks")
def create_task(req: NewTaskRequest) -> dict:
    service = get_service()
    try:
        task = service.submit_async(
            project_ref=req.project,
            prompt=req.prompt,
            agent_name=req.agent,
            test_command_override=req.test_command,
            auto_commit=req.auto_commit,
            source="api",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return task.to_dict()


@app.get("/tasks")
def list_tasks(status: Optional[str] = None, limit: int = 50) -> list[dict]:
    service = get_service()
    status_enum = TaskStatus(status) if status else None
    return [t.to_dict() for t in service.list_tasks(status=status_enum, limit=limit)]


@app.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    service = get_service()
    task = service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Úkol nenalezen")
    return task.to_dict()


@app.get("/tasks/{task_id}/result")
def get_task_result(task_id: str) -> dict:
    service = get_service()
    task = service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Úkol nenalezen")
    return {
        "id": task.id,
        "status": task.status.value,
        "result": task.result,
        "error": task.error,
        "tests_passed": task.tests_passed,
        "committed": task.committed,
        "commit_hash": task.commit_hash,
    }


def serve() -> None:
    import uvicorn

    service = get_service()
    uvicorn.run(app, host=service.config.api.host, port=service.config.api.port)
