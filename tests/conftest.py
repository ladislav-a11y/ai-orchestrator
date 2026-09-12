import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture(autouse=True)
def isolate_provider_slack(monkeypatch, request):
    """Keep provider unit tests out of the production Slack channel.

    Provider adapters notify Slack through a decorator.  The test checkout can
    still resolve the production token file, so fake provider calls must not
    accidentally become real operational messages.  The dedicated Slack
    notification tests call the notification module directly and therefore
    remain able to exercise its HTTP/API handling.
    """
    if request.path.name == "test_slack_provider_notifications.py":
        return
    module = __import__(
        "orchestrator.agents.slack_provider_notifications",
        fromlist=["notify_provider_result"],
    )
    monkeypatch.setattr(module, "notify_provider_result", lambda *args, **kwargs: False)


@pytest.fixture(autouse=True)
def isolate_provider_usage(monkeypatch, tmp_path):
    """Keep fake provider runs out of production current/lifetime ledgers."""
    module = __import__(
        "orchestrator.agents.usage_ledger",
        fromlist=["reset_provider_current", "record_result"],
    )
    usage_directory = tmp_path / "provider-usage"
    original_reset = module.reset_provider_current
    original_record = module.record_result

    def isolated_reset(provider, **_kwargs):
        return original_reset(provider, directory=usage_directory)

    def isolated_record(provider, result, current_by_model, **_kwargs):
        return original_record(
            provider,
            result,
            current_by_model,
            directory=usage_directory,
        )

    monkeypatch.setattr(module, "reset_provider_current", isolated_reset)
    monkeypatch.setattr(module, "record_result", isolated_record)


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
