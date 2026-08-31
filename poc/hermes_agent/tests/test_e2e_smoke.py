from poc.hermes_agent.e2e_smoke import main


def test_e2e_smoke_runs_end_to_end_without_touching_this_repo():
    summary = main()

    assert summary["ok"] is True
    step_names = {step["step"] for step in summary["steps"]}
    assert step_names == {
        "local_provider_run",
        "git_read_only_status",
        "memory_roundtrip",
        "skill_invocation",
        "trello_fake_contract",
        "fallback_recovery",
        "benchmark",
        "security_boundaries",
    }

    by_step = {step["step"]: step for step in summary["steps"]}
    assert by_step["local_provider_run"]["success"] is True
    assert by_step["git_read_only_status"]["result"]["is_inside_work_tree"] == "true"
    assert by_step["memory_roundtrip"]["loaded"] == {"note": "e2e smoke run"}
    assert by_step["skill_invocation"]["result"] == 4
    assert by_step["trello_fake_contract"]["labels"] == ["project_key"]
    gate = by_step["trello_fake_contract"]["authenticated_read_gate"]
    assert set(gate) == {"credentials_present", "human_consent_present", "blocked", "reason"}
    assert by_step["fallback_recovery"]["provider_used"] == "hermes-agent-poc"
    assert by_step["fallback_recovery"]["success"] is True
    assert by_step["security_boundaries"]["dangerous_commands_rejected"] == 3
    assert by_step["security_boundaries"]["transport_calls_for_rejected_commands"] == 0
    assert by_step["security_boundaries"]["outside_workspace_path_rejected"] is True

    # Real, deterministic keyword-overlap quality score of the fake
    # transport's canned output (methodology proof, not a real-model claim
    # - see benchmark.py docstring).
    assert by_step["benchmark"]["quality_score"] == 1.0
    # vram_mb is a real nvidia-smi sample where a GPU/driver is present in
    # this environment, None otherwise - never asserted to a fixed value
    # since it's genuinely hardware-dependent.
    vram_mb = by_step["benchmark"]["vram_mb"]
    assert vram_mb is None or (isinstance(vram_mb, (int, float)) and vram_mb >= 0)
