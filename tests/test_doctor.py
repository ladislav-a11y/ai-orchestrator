import sys
from pathlib import Path

from orchestrator import doctor
from orchestrator.agents.base import AgentRunResult
from orchestrator.agents.codex import CodexAgent
from orchestrator.config import CodexAgentConfig, Config, ProjectEntry


def test_check_python_ok():
    check = doctor._check_python()
    assert check.ok is True


def test_check_git_ok():
    check = doctor._check_git()
    assert check.ok is True


def test_check_dirs_creates_missing(tmp_path):
    cfg = Config()
    cfg.paths.inbox = str(tmp_path / "inbox")
    cfg.paths.outbox = str(tmp_path / "outbox")
    cfg.paths.logs = str(tmp_path / "logs")
    cfg.paths.data = str(tmp_path / "data")

    check = doctor._check_dirs(cfg)
    assert check.ok is True
    assert (tmp_path / "inbox").exists()
    assert (tmp_path / "outbox").exists()


def test_check_config_notes_missing_project_path_within_workspace(tmp_path):
    cfg = Config()
    cfg.workspace_root = str(tmp_path)
    cfg.projects = {"ghost": ProjectEntry(name="ghost", path=str(tmp_path / "does-not-exist"))}
    check = doctor._check_config(cfg)
    assert check.ok is True
    assert "ghost" in check.message
    assert "vytvoří se automaticky" in check.message


def test_check_config_ok_when_paths_exist(tmp_path):
    cfg = Config()
    cfg.projects = {"real": ProjectEntry(name="real", path=str(tmp_path))}
    check = doctor._check_config(cfg)
    assert check.ok is True


def test_check_claude_settings_prepares_existing_project(tmp_path):
    project_dir = tmp_path / "existing-project"
    project_dir.mkdir()
    cfg = Config()
    cfg.projects = {"existing": ProjectEntry(name="existing", path=str(project_dir))}

    check = doctor._check_claude_settings(cfg)

    assert check.ok is True
    assert "existing" in check.message
    assert (project_dir / ".claude" / "settings.local.json").exists()


def test_check_claude_settings_skips_project_that_does_not_exist_yet(tmp_path):
    cfg = Config()
    cfg.projects = {"ghost": ProjectEntry(name="ghost", path=str(tmp_path / "nope"))}

    check = doctor._check_claude_settings(cfg)

    assert check.ok is True
    assert not (tmp_path / "nope").exists()


def test_run_doctor_uses_real_config():
    report = doctor.run_doctor(live=False)
    names = [c.name for c in report.checks]
    assert "Python" in names
    assert "Git" in names
    assert "Claude Code CLI" in names
    assert "Codex CLI" in names


def _codex_config() -> CodexAgentConfig:
    # cli_path must point at a file that actually exists so find_codex_cli()
    # (and therefore CodexAgent construction) succeeds without needing a
    # real 'codex' binary installed - the tests below fake CodexAgent.run()
    # entirely, they never invoke it.
    return CodexAgentConfig(cli_path=sys.executable, sandbox_mode="workspace-write")


def test_check_codex_live_passes_on_schema_conformant_no_mutation_response(tmp_path, monkeypatch):
    (tmp_path / "marker.txt").write_text("hello", encoding="utf-8")
    cfg = Config()
    cfg.codex = _codex_config()
    cfg.paths.data = str(tmp_path / "data")

    def fake_run(self, request):
        assert self.config.sandbox_mode == "read-only", "doctor --live must force read-only, never the configured default"
        return AgentRunResult(
            success=True, output_text='{"ok": true}', cost_usd=0.01,
            input_tokens=120, output_tokens=8, total_tokens=128,
        )

    monkeypatch.setattr(CodexAgent, "run", fake_run)

    check = doctor._check_codex_live(cfg, tmp_path)

    assert check.ok is True
    assert "kontrakt ověřen" in check.message
    assert "input=120, output=8, total=128, zdroj=reported" in check.message
    assert not doctor._codex_live_receipt_path(cfg).exists()


def test_run_doctor_live_is_the_only_path_that_requests_a_receipt(monkeypatch, tmp_path):
    cfg = Config()
    cfg.codex = _codex_config()
    monkeypatch.setattr(doctor, "load_config", lambda: cfg)
    monkeypatch.setattr(doctor, "_check_dirs", lambda config: doctor.Check("dirs", True, "ok"))
    monkeypatch.setattr(doctor, "_check_config", lambda config: doctor.Check("config", True, "ok"))
    monkeypatch.setattr(doctor, "_check_claude_settings", lambda config: doctor.Check("claude", True, "ok"))
    monkeypatch.setattr(doctor, "_check_claude_cli", lambda config: (doctor.Check("claude cli", False, "skip"), None))
    monkeypatch.setattr(doctor, "_check_codex_cli", lambda config: (doctor.Check("codex cli", True, "ok"), object()))
    monkeypatch.setattr(doctor, "_check_codex_live_receipt", lambda config: doctor.Check("receipt", True, "ok"))
    calls = []

    def fake_live(config, project_path, *, write_receipt=False):
        calls.append((project_path, write_receipt))
        return doctor.Check("live", True, "ok")

    monkeypatch.setattr(doctor, "_check_codex_live", fake_live)

    doctor.run_doctor(live=True, live_project=str(tmp_path))

    assert calls == [(tmp_path, True)]


def test_check_codex_live_receipt_rejects_non_codex_executable(tmp_path):
    cfg = Config()
    cfg.paths.data = str(tmp_path / "data")
    doctor._write_codex_live_receipt(cfg, "Python 3.14.7")

    check = doctor._check_codex_live_receipt(cfg)

    assert check.ok is False
    assert "neidentifikuje reálný Codex CLI" in check.message


def test_check_codex_live_fails_when_directory_is_mutated(tmp_path, monkeypatch):
    (tmp_path / "marker.txt").write_text("hello", encoding="utf-8")
    cfg = Config()
    cfg.codex = _codex_config()

    def fake_run(self, request):
        # Simulates a Codex CLI that violated --sandbox read-only and wrote
        # a file anyway - the live check must not report this as verified.
        (request.project_path / "unexpected.txt").write_text("oops", encoding="utf-8")
        return AgentRunResult(success=True, output_text='{"ok": true}')

    monkeypatch.setattr(CodexAgent, "run", fake_run)

    check = doctor._check_codex_live(cfg, tmp_path)

    assert check.ok is False
    assert "kontrakt porušen" in check.message


def test_check_codex_live_fails_on_non_conformant_response(tmp_path, monkeypatch):
    (tmp_path / "marker.txt").write_text("hello", encoding="utf-8")
    cfg = Config()
    cfg.codex = _codex_config()

    def fake_run(self, request):
        return AgentRunResult(success=True, output_text="tohle vubec neni JSON")

    monkeypatch.setattr(CodexAgent, "run", fake_run)

    check = doctor._check_codex_live(cfg, tmp_path)

    assert check.ok is False
    assert "output-schema" in check.message


def test_check_codex_live_fails_when_agent_run_fails(tmp_path, monkeypatch):
    (tmp_path / "marker.txt").write_text("hello", encoding="utf-8")
    cfg = Config()
    cfg.codex = _codex_config()

    def fake_run(self, request):
        return AgentRunResult(success=False, output_text="", error="CLI selhalo")

    monkeypatch.setattr(CodexAgent, "run", fake_run)

    check = doctor._check_codex_live(cfg, tmp_path)

    assert check.ok is False
    assert "CLI selhalo" in check.message
