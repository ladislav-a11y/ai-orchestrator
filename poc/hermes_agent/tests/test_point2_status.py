from poc.hermes_agent import point2_status
from poc.hermes_agent.point2_status import compute_status


def test_point2_status_reports_git_memory_skills_verified():
    summary = compute_status()

    assert summary["components"]["git"]["verified"] is True
    assert summary["components"]["memory"]["verified"] is True
    assert summary["components"]["skills"]["verified"] is True
    assert summary["components"]["trello"]["details"]["fake_contract_verified"] is True


def test_point2_status_trello_blocked_reflects_gate(monkeypatch, tmp_path):
    monkeypatch.delenv("TRELLO_LIVE_READ_HUMAN_CONSENT", raising=False)
    monkeypatch.setattr(
        point2_status,
        "LIVE_TRELLO_ARTIFACT",
        tmp_path / "missing.json",
    )

    summary = compute_status()

    gate = summary["components"]["trello"]["details"]["authenticated_read_gate"]
    assert gate["human_consent_present"] is False
    assert gate["blocked"] is True
    assert summary["components"]["trello"]["verified"] is False
    assert "trello" in summary["blocked_on"]
    assert summary["all_verified"] is False


def test_point2_status_gate_alone_is_not_live_verification(monkeypatch, tmp_path):
    monkeypatch.setenv("TRELLO_KEY", "k")
    monkeypatch.setenv("TRELLO_TOKEN", "t")
    monkeypatch.setenv("TRELLO_BOARD_ID", "b")
    monkeypatch.setenv("TRELLO_INBOX_LIST", "l")
    monkeypatch.setenv(
        "TRELLO_LIVE_READ_HUMAN_CONSENT", "I_CONSENT_TO_SEND_TRELLO_CREDENTIALS"
    )
    monkeypatch.setattr(point2_status, "LIVE_TRELLO_ARTIFACT", tmp_path / "missing.json")

    summary = compute_status()

    assert summary["components"]["trello"]["verified"] is False
    assert summary["all_verified"] is False


def test_point2_status_all_verified_with_live_read_artifact(monkeypatch, tmp_path):
    artifact = tmp_path / "live.json"
    artifact.write_text(
        '{"ok": true, "verification": "authenticated_read"}', encoding="utf-8"
    )
    monkeypatch.setattr(point2_status, "LIVE_TRELLO_ARTIFACT", artifact)

    summary = compute_status()

    assert summary["components"]["trello"]["verified"] is True
    assert summary["all_verified"] is True
    assert summary["blocked_on"] == []
