from pathlib import Path

from orchestrator.config import ApiConfig, Config, GitConfig, PathsConfig, ProjectEntry, TestingConfig
from orchestrator.service import OrchestratorService


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


def test_submit_creates_missing_registered_project_dir(tmp_path):
    cfg = make_cfg(tmp_path)
    project_dir = tmp_path / "workspace" / "station-agent"
    assert not project_dir.exists()

    service = OrchestratorService(cfg)
    task = service.submit(project_ref="station-agent", prompt="zaloz projekt")

    assert project_dir.exists() and project_dir.is_dir()
    assert task.project_path == str(project_dir)


def test_submit_leaves_existing_project_dir_untouched(tmp_path):
    cfg = make_cfg(tmp_path)
    project_dir = tmp_path / "workspace" / "station-agent"
    project_dir.mkdir(parents=True)
    marker = project_dir / "keep.txt"
    marker.write_text("existing content", encoding="utf-8")

    service = OrchestratorService(cfg)
    service.submit(project_ref="station-agent", prompt="pokracuj")

    assert marker.read_text(encoding="utf-8") == "existing content"
