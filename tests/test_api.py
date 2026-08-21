from fastapi.testclient import TestClient

from orchestrator import api
from orchestrator.models import TaskStatus
from orchestrator.queue import make_task


class FakeService:
    def __init__(self):
        self.tasks = {}

    def submit_async(self, project_ref, prompt, agent_name=None, test_command_override=None,
                      auto_commit=None, source="api"):
        task = make_task(project_ref, "D:/demo", prompt, agent_name or "claude-code", test_command_override, 2,
                          bool(auto_commit), source)
        self.tasks[task.id] = task
        return task

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def list_tasks(self, status=None, limit=50):
        tasks = list(self.tasks.values())
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        return tasks[:limit]


def make_client(monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(api, "get_service", lambda: fake)
    return TestClient(api.app), fake


def test_health(monkeypatch):
    client, _ = make_client(monkeypatch)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_and_get_task(monkeypatch):
    client, fake = make_client(monkeypatch)
    resp = client.post("/tasks", json={"project": "demo", "prompt": "udelej neco"})
    assert resp.status_code == 200
    task_id = resp.json()["id"]

    resp2 = client.get(f"/tasks/{task_id}")
    assert resp2.status_code == 200
    assert resp2.json()["prompt"] == "udelej neco"


def test_get_missing_task_404(monkeypatch):
    client, _ = make_client(monkeypatch)
    resp = client.get("/tasks/does-not-exist")
    assert resp.status_code == 404


def test_get_task_result(monkeypatch):
    client, fake = make_client(monkeypatch)
    resp = client.post("/tasks", json={"project": "demo", "prompt": "udelej neco"})
    task_id = resp.json()["id"]
    fake.tasks[task_id].status = TaskStatus.DONE
    fake.tasks[task_id].result = "hotovo"

    resp2 = client.get(f"/tasks/{task_id}/result")
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["status"] == "done"
    assert body["result"] == "hotovo"


def test_list_tasks(monkeypatch):
    client, _ = make_client(monkeypatch)
    client.post("/tasks", json={"project": "demo", "prompt": "a"})
    client.post("/tasks", json={"project": "demo", "prompt": "b"})
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert len(resp.json()) == 2
