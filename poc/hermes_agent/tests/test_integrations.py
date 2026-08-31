import subprocess
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from poc.hermes_agent.integrations import (
    FakeTrelloClient,
    GitCheckError,
    MemoryStore,
    SkillRegistry,
    TRELLO_HUMAN_CONSENT_ENV_VAR,
    TRELLO_HUMAN_CONSENT_VALUE,
    verify_trello_read_access,
    git_read_only_status,
    trello_authenticated_read_gate_status,
    word_count_skill,
    check_trello_api_reachable,
)


class _JsonResponse:
    def __init__(self, value):
        import json
        self._body = json.dumps(value).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_verify_trello_read_access_is_read_only_and_redacts_credentials(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        if request.full_url.endswith("filter=open"):
            return _JsonResponse([{"id": "card-1"}])
        return _JsonResponse({"idBoard": "board-1"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = verify_trello_read_access(
        key="secret-key", token="secret-token", board_id="board-1", list_id="list-1"
    )

    assert result == {
        "authenticated": True,
        "list_read": True,
        "cards_read": True,
        "list_belongs_to_configured_board": True,
        "open_card_count": 1,
        "ok": True,
    }
    assert all(request.get_method() == "GET" for request in requests)
    assert "secret" not in repr(result)


@pytest.fixture
def scratch_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


def test_git_read_only_status_on_clean_repo(scratch_git_repo: Path):
    status = git_read_only_status(scratch_git_repo)
    assert status["is_inside_work_tree"] == "true"
    assert status["status_porcelain"] == ""


def test_git_read_only_status_detects_uncommitted_change(scratch_git_repo: Path):
    (scratch_git_repo / "new_file.txt").write_text("hello\n", encoding="utf-8")
    status = git_read_only_status(scratch_git_repo)
    assert "new_file.txt" in status["status_porcelain"]


def test_git_read_only_status_raises_for_non_repo():
    # Deliberately NOT `tmp_path`: this project's pytest.ini sets
    # `--basetemp=.pytest-tmp`, which resolves *inside* this very Git
    # repository - `git rev-parse --is-inside-work-tree` would then
    # (correctly) report "true" for the enclosing ai-orchestrator repo
    # instead of failing, defeating the point of this test. A real OS temp
    # dir (same mechanism `e2e_smoke.py` uses) guarantees no enclosing repo.
    with tempfile.TemporaryDirectory(prefix="hermes-poc-not-a-repo-") as scratch:
        not_a_repo = Path(scratch)
        with pytest.raises(GitCheckError):
            git_read_only_status(not_a_repo)


def test_memory_store_roundtrip(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory")
    assert store.load("missing") is None

    store.save("note", {"a": 1, "b": [1, 2, 3]})
    assert store.load("note") == {"a": 1, "b": [1, 2, 3]}
    assert store.list_keys() == ["note"]


def test_skill_registry_invokes_registered_skill():
    registry = SkillRegistry()
    registry.register("word_count", word_count_skill)
    assert registry.invoke("word_count", text="one two three") == 3
    assert registry.names() == ["word_count"]


def test_skill_registry_raises_for_unknown_skill():
    registry = SkillRegistry()
    with pytest.raises(KeyError):
        registry.invoke("does-not-exist")


def test_fake_trello_client_labels_and_comments():
    client = FakeTrelloClient()
    client.seed_card("card-1", labels=["project_key"])
    assert client.get_labels("card-1") == ["project_key"]

    client.add_comment("card-1", "hello")
    assert client.get_comments("card-1") == ["hello"]


def test_fake_trello_client_raises_for_unknown_card():
    client = FakeTrelloClient()
    with pytest.raises(KeyError):
        client.get_labels("unknown-card")


def test_trello_reachability_accepts_expected_credential_error():
    error = urllib.error.HTTPError(
        "https://api.trello.com/1/members/me",
        400,
        "Bad Request",
        {},
        MagicMock(read=MagicMock(return_value=b"invalid token")),
    )
    with patch("poc.hermes_agent.integrations.urllib.request.urlopen", side_effect=error):
        result = check_trello_api_reachable()

    assert result["reachable"] is True
    assert result["http_status"] == 400
    assert result["body"] == "invalid token"


def test_trello_reachability_reports_network_failure():
    with patch(
        "poc.hermes_agent.integrations.urllib.request.urlopen",
        side_effect=urllib.error.URLError("offline"),
    ):
        result = check_trello_api_reachable()

    assert result["reachable"] is False
    assert result["http_status"] is None
    assert "offline" in result["note"]


def test_trello_gate_status_blocked_when_credentials_missing():
    status = trello_authenticated_read_gate_status(env={})

    assert status == {
        "credentials_present": False,
        "human_consent_present": False,
        "blocked": True,
        "reason": (
            "missing environment variables: "
            "['TRELLO_KEY', 'TRELLO_TOKEN', 'TRELLO_BOARD_ID', 'TRELLO_INBOX_LIST']"
        ),
    }


def test_trello_gate_status_blocked_when_consent_missing_but_credentials_present():
    env = {
        "TRELLO_KEY": "k",
        "TRELLO_TOKEN": "t",
        "TRELLO_BOARD_ID": "b",
        "TRELLO_INBOX_LIST": "l",
    }

    status = trello_authenticated_read_gate_status(env=env)

    assert status["credentials_present"] is True
    assert status["human_consent_present"] is False
    assert status["blocked"] is True
    assert TRELLO_HUMAN_CONSENT_ENV_VAR in status["reason"]


def test_trello_gate_status_unblocked_when_consent_and_credentials_present():
    env = {
        "TRELLO_KEY": "k",
        "TRELLO_TOKEN": "t",
        "TRELLO_BOARD_ID": "b",
        "TRELLO_INBOX_LIST": "l",
        TRELLO_HUMAN_CONSENT_ENV_VAR: TRELLO_HUMAN_CONSENT_VALUE,
    }

    status = trello_authenticated_read_gate_status(env=env)

    assert status == {
        "credentials_present": True,
        "human_consent_present": True,
        "blocked": False,
        "reason": "unblocked: credentials and human consent are both present",
    }


def test_trello_gate_status_never_exposes_credential_values():
    env = {
        "TRELLO_KEY": "super-secret-key",
        "TRELLO_TOKEN": "super-secret-token",
        "TRELLO_BOARD_ID": "b",
        "TRELLO_INBOX_LIST": "l",
    }

    status = trello_authenticated_read_gate_status(env=env)

    assert "super-secret-key" not in repr(status)
    assert "super-secret-token" not in repr(status)
