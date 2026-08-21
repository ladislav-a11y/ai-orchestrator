from pathlib import Path

from orchestrator import doctor
from orchestrator.config import Config, ProjectEntry


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


def test_run_doctor_uses_real_config():
    report = doctor.run_doctor(live=False)
    names = [c.name for c in report.checks]
    assert "Python" in names
    assert "Git" in names
    assert "Claude Code CLI" in names
