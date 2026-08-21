from pathlib import Path

import pytest

from orchestrator.config import load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "config.example.yaml"


def test_load_example_config():
    cfg = load_config(EXAMPLE, create_if_missing=False)
    assert cfg.default_agent == "claude-code"
    assert cfg.claude_code.permission_mode == "acceptEdits"
    assert cfg.api.host == "127.0.0.1"


def test_forbidden_permission_mode_rejected(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text(
        "claude_code:\n  permission_mode: bypassPermissions\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="bypassPermissions"):
        load_config(bad, create_if_missing=False)


def test_non_localhost_api_host_rejected(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("api:\n  host: 0.0.0.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="localhost"):
        load_config(bad, create_if_missing=False)


def test_project_registry_parsing(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    cfg_path.write_text(
        f"projects:\n  myproj:\n    path: \"{project_dir.as_posix()}\"\n    test_command: pytest\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path, create_if_missing=False)
    entry = cfg.resolve_project("myproj")
    assert entry.test_command == "pytest"
    assert Path(entry.path) == project_dir


def test_resolve_project_by_raw_path(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("projects: {}\n", encoding="utf-8")
    cfg = load_config(cfg_path, create_if_missing=False)
    entry = cfg.resolve_project(str(tmp_path))
    assert Path(entry.path) == tmp_path.resolve()


def test_resolve_unknown_project_raises(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("projects: {}\n", encoding="utf-8")
    cfg = load_config(cfg_path, create_if_missing=False)
    with pytest.raises(ValueError):
        cfg.resolve_project("does-not-exist-anywhere-xyz")
