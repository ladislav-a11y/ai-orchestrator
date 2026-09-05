"""CLI-surface tests.

AI Project Manager drives ai-orchestrator by shelling out to this CLI (see
PROJECT_HANDOVER_2026-08-25.md, commit b545d94 "Add run-id support for
autonomous runs"): `orchestrator.py autonomous --project ... --spec ...
--run-id <trello-card-id>`, then reads `outbox/autonomous-<run-id>.json` for
the result to post back to the Trello card. Every other test in this suite
calls `service.run_autonomous()` directly and never goes through argparse,
so none of them actually verify that CLI flags - especially --run-id, the
external contract AI Project Manager relies on - really reach the service
the way a real subprocess invocation would.
"""

import json
import io
import re
from pathlib import Path

import pytest

from orchestrator import cli
from orchestrator import service as service_module
from orchestrator.agents.base import Agent, AgentRunResult
from orchestrator.autonomous import AUDIT_MARKER
from orchestrator.config import ApiConfig, Config, GitConfig, PathsConfig, ProjectEntry, TestingConfig
from orchestrator.service import OrchestratorService


def _audit_response(request):
    indices = [int(value) for value in re.findall(r"(?m)^(\d+)\. ", request.prompt)]
    return json.dumps({
        "items": [{"index": value, "accepted": True, "method": "static: kontrola projektu", "evidence": f"{request.project_path.name}: audit evidence"} for value in indices],
        "notes": "audit ok",
    })


class FakeAgent(Agent):
    name = "fake"

    def is_available(self):
        return True, "fake agent always available"

    def run(self, request):
        if AUDIT_MARKER in request.prompt:
            return AgentRunResult(success=True, output_text=_audit_response(request))
        return AgentRunResult(
            success=True,
            output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
        )


def make_cfg(tmp_path: Path) -> Config:
    return Config(
        projects={
            "station-agent": ProjectEntry(name="station-agent", path=str(tmp_path / "workspace" / "station-agent")),
        },
        git=GitConfig(auto_commit=False),
        testing=TestingConfig(),
        api=ApiConfig(),
        paths=PathsConfig(
            inbox=str(tmp_path / "inbox"),
            outbox=str(tmp_path / "outbox"),
            logs=str(tmp_path / "logs"),
            data=str(tmp_path / "data"),
        ),
        workspace_root=str(tmp_path / "workspace"),
    )


def test_autonomous_cli_passes_run_id_and_writes_outbox(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "build_agent", lambda name, config: FakeAgent())

    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg)
    monkeypatch.setattr(cli, "OrchestratorService", lambda: service)

    spec_path = tmp_path / "dod.md"
    spec_path.write_text("- [ ] Over health endpoint\n", encoding="utf-8")

    try:
        exit_code = cli.main(
            [
                "autonomous",
                "--project", "station-agent",
                "--spec", str(spec_path),
                "--run-id", "trello-card-42",
                "--max-iterations", "3",
                "--no-commit",
            ]
        )

        assert exit_code == 0

        outbox_path = cfg.outbox_dir / "autonomous-trello-card-42.json"
        assert outbox_path.exists()
        payload = json.loads(outbox_path.read_text(encoding="utf-8"))
        assert payload["run_id"] == "trello-card-42"
        assert payload["status"] == "completed"
        assert payload["done"] is True
        assert payload["dod_items"] == [{
            "text": "Over health endpoint",
            "done": True,
            "live_verification": None,
            "live_evidence": None,
        }]
    finally:
        service.shutdown()


def test_autonomous_cli_accepts_model_override():
    args = cli.build_parser().parse_args([
        "autonomous",
        "--project", "station-agent",
        "--goal", "cil",
        "--agent", "claude-code",
        "--model", "claude-opus-4-1",
    ])

    assert args.model == "claude-opus-4-1"


def test_plan_inbox_schema_uses_codex_compatible_json_schema(monkeypatch, capsys):
    seen = {}

    class PlanningAgent(Agent):
        name = "codex"

        def is_available(self):
            return True, "planner test agent"

        def run(self, request):
            seen["schema"] = request.output_schema
            return AgentRunResult(
                success=True,
                output_text=json.dumps({
                    "tasks": [{
                        "scope": "oprava",
                        "task": "Opravit station agenta.",
                        "next_step": "Prověřit reprodukci.",
                        "priority": 5.01,
                        "priority_reason": "Potvrzená regrese.",
                        "work_type": "implementation",
                        "split_reason": "Jeden koherentní výsledek opravy.",
                        "depends_on": [],
                    }]
                }),
            )

    monkeypatch.setattr(cli, "build_agent", lambda name, config: PlanningAgent())
    monkeypatch.setattr(cli, "load_config", lambda: Config())
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        io.StringIO(json.dumps({"card": {"name": "oprava station agent"}})),
    )

    assert cli.main(["plan-inbox", "--agent", "codex"]) == 0
    assert "uniqueItems" not in seen["schema"]["properties"]["tasks"]["items"]["properties"]["depends_on"]
    json.loads(capsys.readouterr().out)


def test_autonomous_cli_accepts_scoped_provider_order():
    args = cli.build_parser().parse_args([
        "autonomous",
        "--project", "station-agent",
        "--goal", "cil",
        "--agent", "auto",
        "--provider-order", "gemini,antigravity,claude-code,codex",
    ])

    assert args.provider_order == "gemini,antigravity,claude-code,codex"


def test_autonomous_cli_reports_missing_live_evidence(tmp_path, monkeypatch, capsys):
    """A DoD item declaring LIVE-EVIDENCE without a matching LIVE-RESULT must
    stay open (see orchestrator.autonomous._enforce_live_evidence) and the
    CLI output must say so explicitly - not just print the same bare "[ ]"
    as an ordinary unmet item - so an operator/AI Project Manager reading
    the run output knows a real integration check is still owed, not more
    local implementation work."""
    monkeypatch.setattr(service_module, "build_agent", lambda name, config: FakeAgent())

    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg)
    monkeypatch.setattr(cli, "OrchestratorService", lambda: service)

    spec_path = tmp_path / "dod.md"
    spec_path.write_text(
        '- [x] Trello obsahuje projektovy label '
        '<!-- LIVE-EVIDENCE: {"command":"nacti labely karty","expect":"project_key"} -->\n',
        encoding="utf-8",
    )

    try:
        exit_code = cli.main(
            [
                "autonomous",
                "--project", "station-agent",
                "--spec", str(spec_path),
                "--run-id", "trello-card-live-missing",
                "--max-iterations", "1",
                "--no-commit",
            ]
        )
        assert exit_code != 0
        out = capsys.readouterr().out
        assert "[ ] 0. Trello obsahuje projektovy label" in out
        assert "živý důkaz vyžadován: 'nacti labely karty' -> 'project_key'" in out
        assert "CHYBÍ" in out
    finally:
        service.shutdown()


def test_autonomous_cli_commit_flag_commits_even_when_config_default_is_off(tmp_path, monkeypatch, git_repo):
    # Regression: an explicitly approved run (--commit on the CLI, the only
    # channel AI Project Manager or a human operator has to approve a single
    # invocation - see module docstring) must actually produce a commit once
    # the Definition of Done is met and tests pass, even though the
    # deployment's config.yaml still has git.auto_commit: false (the safe
    # default - see README "Bezpečnostní pravidla").
    class WritingAgent(Agent):
        name = "fake"

        def is_available(self):
            return True, "fake agent always available"

        def run(self, request):
            if AUDIT_MARKER in request.prompt:
                return AgentRunResult(success=True, output_text=_audit_response(request))
            (git_repo / "feature.txt").write_text("nova funkce\n", encoding="utf-8")
            return AgentRunResult(
                success=True,
                output_text='{"items": [{"index": 0, "done": true}], "notes": "hotovo"}',
            )

    monkeypatch.setattr(service_module, "build_agent", lambda name, config: WritingAgent())

    cfg = Config(
        projects={"station-agent": ProjectEntry(name="station-agent", path=str(git_repo))},
        git=GitConfig(auto_commit=False),
        testing=TestingConfig(),
        api=ApiConfig(),
        paths=PathsConfig(
            inbox=str(tmp_path / "inbox"),
            outbox=str(tmp_path / "outbox"),
            logs=str(tmp_path / "logs"),
            data=str(tmp_path / "data"),
        ),
        workspace_root=str(git_repo.parent),
    )
    service = OrchestratorService(cfg)
    monkeypatch.setattr(cli, "OrchestratorService", lambda: service)

    spec_path = tmp_path / "dod.md"
    spec_path.write_text("- [ ] Priprav feature.txt\n", encoding="utf-8")

    try:
        exit_code = cli.main(
            [
                "autonomous",
                "--project", "station-agent",
                "--spec", str(spec_path),
                "--run-id", "trello-card-commit",
                "--max-iterations", "3",
                "--commit",
            ]
        )

        assert exit_code == 0

        outbox_path = cfg.outbox_dir / "autonomous-trello-card-commit.json"
        payload = json.loads(outbox_path.read_text(encoding="utf-8"))
        assert payload["status"] == "completed"
        assert payload["committed"] is True
        assert payload["commit_hash"]
    finally:
        service.shutdown()


@pytest.mark.parametrize("subcommand, extra_args", [
    ("run", ["prompt text", "--project", "station-agent"]),
    ("autonomous", ["--project", "station-agent", "--goal", "cil"]),
])
def test_commit_and_no_commit_flags_are_mutually_exclusive(subcommand, extra_args):
    # --commit and --no-commit express opposite explicit intents for the
    # same invocation - argparse must refuse both at once instead of
    # silently picking one, so a caller never gets a surprising outcome.
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([subcommand, *extra_args, "--commit", "--no-commit"])


def test_autonomous_cli_nonzero_exit_when_not_completed(tmp_path, monkeypatch):
    class StuckAgent(Agent):
        name = "fake"

        def is_available(self):
            return True, "fake agent always available"

        def run(self, request):
            if AUDIT_MARKER in request.prompt:
                return AgentRunResult(success=True, output_text=_audit_response(request))
            # Never marks the DoD item done - loop exhausts max_iterations.
            return AgentRunResult(success=True, output_text='{"items": [], "notes": "pracuji"}')

    monkeypatch.setattr(service_module, "build_agent", lambda name, config: StuckAgent())

    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg)
    monkeypatch.setattr(cli, "OrchestratorService", lambda: service)

    spec_path = tmp_path / "dod.md"
    spec_path.write_text("- [ ] Over health endpoint\n", encoding="utf-8")

    try:
        exit_code = cli.main(
            [
                "autonomous",
                "--project", "station-agent",
                "--spec", str(spec_path),
                "--run-id", "trello-card-99",
                "--max-iterations", "1",
                "--no-commit",
            ]
        )

        assert exit_code == 1

        outbox_path = cfg.outbox_dir / "autonomous-trello-card-99.json"
        payload = json.loads(outbox_path.read_text(encoding="utf-8"))
        assert payload["run_id"] == "trello-card-99"
        assert payload["done"] is False
        assert payload["status"] != "completed"
    finally:
        service.shutdown()


def test_autonomous_cli_waiting_for_provider_prints_auto_resume_inactive(tmp_path, monkeypatch, capsys):
    """DoD (produkční incident cb501524e47e, 26.8.2026): the CLI output for
    a WAITING_FOR_PROVIDER outcome must explicitly say that no persistent
    worker/scheduler is behind THIS invocation (the real `orchestrator.py
    autonomous` usage - a one-shot process, see README ch.9) - never leave
    the caller to assume it will resume on its own."""

    class AlwaysLimitedAgent(Agent):
        name = "fake"

        def is_available(self):
            return True, "fake agent always available"

        def run(self, request):
            return AgentRunResult(
                success=False, output_text="", error="rate limit reached",
                limited=True, retry_after_seconds=120.0,
            )

    monkeypatch.setattr(service_module, "build_agent", lambda name, config: AlwaysLimitedAgent())

    cfg = make_cfg(tmp_path)
    service = OrchestratorService(cfg)
    monkeypatch.setattr(cli, "OrchestratorService", lambda: service)

    spec_path = tmp_path / "dod.md"
    spec_path.write_text("- [ ] Over health endpoint\n", encoding="utf-8")

    try:
        exit_code = cli.main(
            [
                "autonomous",
                "--project", "station-agent",
                "--spec", str(spec_path),
                "--run-id", "trello-card-waiting",
                "--max-iterations", "3",
                "--no-commit",
            ]
        )

        assert exit_code == 1
        out = capsys.readouterr().out
        assert "120" in out
        assert "NENÍ aktivní" in out
        assert "trello-card-waiting" in out

        outbox_path = cfg.outbox_dir / "autonomous-trello-card-waiting.json"
        payload = json.loads(outbox_path.read_text(encoding="utf-8"))
        assert payload["status"] == "waiting_for_provider"
        assert payload["auto_resume_active"] is False
        assert payload["done"] is False
    finally:
        service.shutdown()
