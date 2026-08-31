import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


def pytest_configure(config):
    """Make an explicit nested --basetemp robust on Windows.

    Pytest creates the leaf directory, but not a missing parent such as
    ``.tmp``.  Ensure only that parent exists so a healthy suite cannot turn
    into setup errors before any application test runs.
    """
    if config.option.basetemp is not None:
        Path(config.option.basetemp).parent.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture(autouse=True)
def disable_slack_notifications(monkeypatch):
    monkeypatch.delenv("AI_PM_SLACK_ENABLED", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
