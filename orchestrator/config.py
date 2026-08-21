"""Configuration loading for the orchestrator.

Config lives in config/config.yaml (created from config/config.example.yaml on
first run by `doctor` if missing). Every path in the config is resolved
relative to the project root unless it is already absolute.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
CONFIG_PATH = CONFIG_DIR / "config.yaml"
CONFIG_EXAMPLE_PATH = CONFIG_DIR / "config.example.yaml"

# This value must never appear in a resolved config. It is the CLI flag we are
# forbidden from ever passing to `claude`, so we hard-block it at load time
# even if someone edits the YAML by hand.
FORBIDDEN_PERMISSION_MODES = {"bypassPermissions"}


@dataclass
class ProjectEntry:
    name: str
    path: str
    test_command: Optional[str] = None


@dataclass
class ClaudeCodeAgentConfig:
    cli_path: str = ""  # empty = auto-detect
    model: str = ""  # empty = CLI default
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    max_budget_usd: Optional[float] = None
    timeout_seconds: int = 1800


@dataclass
class GitConfig:
    auto_commit: bool = False
    commit_message_prefix: str = "[ai-orchestrator] "


@dataclass
class TestingConfig:
    test_command: str = ""
    max_fix_attempts: int = 2


@dataclass
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class PathsConfig:
    inbox: str = "inbox"
    outbox: str = "outbox"
    logs: str = "logs"
    data: str = "data"


@dataclass
class Config:
    default_agent: str = "claude-code"
    projects: dict[str, ProjectEntry] = field(default_factory=dict)
    claude_code: ClaudeCodeAgentConfig = field(default_factory=ClaudeCodeAgentConfig)
    git: GitConfig = field(default_factory=GitConfig)
    testing: TestingConfig = field(default_factory=TestingConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    source_path: Optional[Path] = None

    def resolve_path(self, relative_or_absolute: str) -> Path:
        p = Path(relative_or_absolute)
        if p.is_absolute():
            return p
        return PROJECT_ROOT / p

    @property
    def inbox_dir(self) -> Path:
        return self.resolve_path(self.paths.inbox)

    @property
    def outbox_dir(self) -> Path:
        return self.resolve_path(self.paths.outbox)

    @property
    def logs_dir(self) -> Path:
        return self.resolve_path(self.paths.logs)

    @property
    def data_dir(self) -> Path:
        return self.resolve_path(self.paths.data)

    def resolve_project(self, project_ref: str) -> ProjectEntry:
        """Resolve a project by its registry name, or accept a raw filesystem path."""
        if project_ref in self.projects:
            return self.projects[project_ref]
        p = Path(project_ref)
        if p.exists():
            return ProjectEntry(name=project_ref, path=str(p.resolve()))
        raise ValueError(
            f"Neznámý projekt '{project_ref}'. Není ani v config.yaml (projects), "
            f"ani to není existující cesta na disku."
        )


def _ensure_config_exists() -> None:
    if CONFIG_PATH.exists():
        return
    if not CONFIG_EXAMPLE_PATH.exists():
        raise FileNotFoundError(
            f"Chybí jak {CONFIG_PATH}, tak vzorový {CONFIG_EXAMPLE_PATH}. "
            "Projekt je poškozený nebo neúplně nakopírovaný."
        )
    shutil.copyfile(CONFIG_EXAMPLE_PATH, CONFIG_PATH)


def load_config(path: Optional[Path] = None, create_if_missing: bool = True) -> Config:
    cfg_path = path or CONFIG_PATH
    if cfg_path == CONFIG_PATH and create_if_missing:
        _ensure_config_exists()
    if not cfg_path.exists():
        raise FileNotFoundError(f"Konfigurační soubor nenalezen: {cfg_path}")

    raw: dict[str, Any] = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    projects: dict[str, ProjectEntry] = {}
    for name, entry in (raw.get("projects") or {}).items():
        if isinstance(entry, str):
            projects[name] = ProjectEntry(name=name, path=entry)
        else:
            projects[name] = ProjectEntry(
                name=name,
                path=entry["path"],
                test_command=entry.get("test_command"),
            )

    cc_raw = raw.get("claude_code") or {}
    permission_mode = cc_raw.get("permission_mode", "acceptEdits")
    if permission_mode in FORBIDDEN_PERMISSION_MODES:
        raise ValueError(
            f"config.yaml nastavuje claude_code.permission_mode='{permission_mode}', "
            f"ale tento orchestrátor to nikdy nesmí použít (rovná se obejití "
            f"kontroly oprávnění). Zvol jiný mód, např. 'acceptEdits'."
        )
    claude_code = ClaudeCodeAgentConfig(
        cli_path=cc_raw.get("cli_path", ""),
        model=cc_raw.get("model", ""),
        permission_mode=permission_mode,
        allowed_tools=list(cc_raw.get("allowed_tools", []) or []),
        disallowed_tools=list(cc_raw.get("disallowed_tools", []) or []),
        max_budget_usd=cc_raw.get("max_budget_usd"),
        timeout_seconds=int(cc_raw.get("timeout_seconds", 1800)),
    )

    git_raw = raw.get("git") or {}
    git = GitConfig(
        auto_commit=bool(git_raw.get("auto_commit", False)),
        commit_message_prefix=git_raw.get("commit_message_prefix", "[ai-orchestrator] "),
    )

    testing_raw = raw.get("testing") or {}
    testing = TestingConfig(
        test_command=testing_raw.get("test_command", ""),
        max_fix_attempts=int(testing_raw.get("max_fix_attempts", 2)),
    )

    api_raw = raw.get("api") or {}
    host = api_raw.get("host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"config.yaml nastavuje api.host='{host}'. V této fázi smí API "
            "poslouchat pouze na localhost (127.0.0.1 / localhost / ::1)."
        )
    api = ApiConfig(host=host, port=int(api_raw.get("port", 8765)))

    paths_raw = raw.get("paths") or {}
    paths = PathsConfig(
        inbox=paths_raw.get("inbox", "inbox"),
        outbox=paths_raw.get("outbox", "outbox"),
        logs=paths_raw.get("logs", "logs"),
        data=paths_raw.get("data", "data"),
    )

    return Config(
        default_agent=raw.get("default_agent", "claude-code"),
        projects=projects,
        claude_code=claude_code,
        git=git,
        testing=testing,
        api=api,
        paths=paths,
        source_path=cfg_path,
    )
