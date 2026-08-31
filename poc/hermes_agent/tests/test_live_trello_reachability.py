"""Opt-in live test for `check_trello_api_reachable` - mirrors the project's
own `tests/test_codex_agent.py::test_live_smoke_reads_project_state_without_changes`
gating pattern (`AI_ORCHESTRATOR_RUN_LIVE_CODEX_TEST`) and this PoC's own
`test_live_free_provider.py`: skipped by default so the main
`python -m pytest -q` run never depends on real network access, only runs
when explicitly requested.
"""

from __future__ import annotations

import os

import pytest

from poc.hermes_agent.live_trello_reachability_smoke import main

LIVE_ENV_VAR = "AI_ORCHESTRATOR_RUN_LIVE_TRELLO_TEST"


@pytest.mark.skipif(
    os.environ.get(LIVE_ENV_VAR) != "1",
    reason=f"pouze explicitně přes {LIVE_ENV_VAR}=1 - vyžaduje skutečný přístup k internetu",
)
def test_live_smoke_reaches_real_trello_api():
    """Real network smoke: confirms the real `api.trello.com` responds per
    its documented, credential-required contract (HTTP 400/401 without a
    key/token) - the maximum that can be verified live without a real
    Trello credential (none exists in this repository). Never runs in
    normal test runs (see skipif above)."""
    summary = main()

    assert summary["reachable"] is True, summary.get("note")
    assert summary["http_status"] in (400, 401)
    assert "invalid token" in summary["body"].lower()
    assert summary["ok"] is True
