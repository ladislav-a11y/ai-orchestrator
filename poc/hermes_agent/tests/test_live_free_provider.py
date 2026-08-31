"""Opt-in live test for `OpenCodeFreeTransport` - mirrors the project's own
`tests/test_codex_agent.py::test_live_smoke_reads_project_state_without_changes`
gating pattern (`AI_ORCHESTRATOR_RUN_LIVE_CODEX_TEST`): skipped by default so
the main `python -m pytest -q` run never depends on real network access,
only runs when explicitly requested.
"""

from __future__ import annotations

import os

import pytest

from poc.hermes_agent.live_free_provider_smoke import main

LIVE_ENV_VAR = "AI_ORCHESTRATOR_RUN_LIVE_HERMES_FREE_TEST"


@pytest.mark.skipif(
    os.environ.get(LIVE_ENV_VAR) != "1",
    reason=f"pouze explicitně přes {LIVE_ENV_VAR}=1 - vyžaduje skutečný přístup k internetu",
)
def test_live_smoke_completes_real_free_completion():
    """Real network smoke: one $0 completion against Hermes Agent's built-in
    keyless free provider. Never runs in normal test runs (see skipif
    above)."""
    summary = main()

    assert summary["available"] is True, summary.get("availability_message")
    assert summary["success"] is True, summary.get("error")
    assert summary["cost_usd"] == 0.0
    assert summary["output_text"]
    assert summary["vram"]["applicability"] == "client_gpu_only_remote_provider"
    assert summary["vram"]["provider_vram_mb"] is None
    assert "client_gpu_before_mb" in summary["vram"]
    assert "client_gpu_after_mb" in summary["vram"]
    assert summary["ok"] is True
