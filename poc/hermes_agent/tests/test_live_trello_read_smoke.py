"""Deterministic, offline tests for the human-consent gate in
`live_trello_read_smoke.py`. These never touch the network - they prove the
gate itself works, independent of whether a live, credential-bearing run is
ever authorized (see README.md "Sandbox limitations" / DoD point 2).
"""

from __future__ import annotations

import json

from poc.hermes_agent import live_trello_read_smoke as smoke


def test_blocks_without_explicit_human_consent(monkeypatch, tmp_path):
    monkeypatch.delenv(smoke.CONSENT_ENV_VAR, raising=False)
    monkeypatch.setenv("TRELLO_KEY", "k")
    monkeypatch.setenv("TRELLO_TOKEN", "t")
    monkeypatch.setenv("TRELLO_BOARD_ID", "b")
    monkeypatch.setenv("TRELLO_INBOX_LIST", "l")
    monkeypatch.setattr(smoke, "ARTIFACT", tmp_path / "result.json")

    result = smoke.main()

    assert result["ok"] is False
    assert "missing explicit human consent" in result["blocked_reason"]
    assert result["gate_status"] == {
        "credentials_present": True,
        "human_consent_present": False,
        "blocked": True,
        "reason": (
            f"credentials present but {smoke.CONSENT_ENV_VAR} was not set "
            "by a human right before this check"
        ),
    }
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == result


def test_wrong_consent_value_still_blocks(monkeypatch, tmp_path):
    monkeypatch.setenv(smoke.CONSENT_ENV_VAR, "yes")
    monkeypatch.setattr(smoke, "ARTIFACT", tmp_path / "result.json")

    result = smoke.main()

    assert result["ok"] is False
    assert "blocked_reason" in result


def test_proceeds_to_credential_check_once_consent_granted(monkeypatch, tmp_path):
    monkeypatch.setenv(smoke.CONSENT_ENV_VAR, smoke.CONSENT_VALUE)
    monkeypatch.delenv("TRELLO_KEY", raising=False)
    monkeypatch.delenv("TRELLO_TOKEN", raising=False)
    monkeypatch.delenv("TRELLO_BOARD_ID", raising=False)
    monkeypatch.delenv("TRELLO_INBOX_LIST", raising=False)
    monkeypatch.setattr(smoke, "ARTIFACT", tmp_path / "result.json")

    result = smoke.main()

    assert result["ok"] is False
    assert result["missing_environment_variables"] == [
        "TRELLO_KEY",
        "TRELLO_TOKEN",
        "TRELLO_BOARD_ID",
        "TRELLO_INBOX_LIST",
    ]


def test_successful_live_read_is_marked_as_authenticated(monkeypatch, tmp_path):
    monkeypatch.setenv(smoke.CONSENT_ENV_VAR, smoke.CONSENT_VALUE)
    for name in (
        "TRELLO_KEY",
        "TRELLO_TOKEN",
        "TRELLO_BOARD_ID",
        "TRELLO_INBOX_LIST",
    ):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setattr(smoke, "ARTIFACT", tmp_path / "result.json")
    monkeypatch.setattr(smoke, "verify_trello_read_access", lambda **kwargs: {"ok": True})

    result = smoke.main()

    assert result == {"ok": True, "verification": "authenticated_read"}
