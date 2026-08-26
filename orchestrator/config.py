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

# The only `--mode` values the real `agy` CLI accepts (confirmed via
# `agy --help`). "" means "don't pass --mode at all" - confirmed against the
# real CLI to still deny-by-default (not bypass) anything outside file edits.
# There is no mode value that means "skip permissions" - that only exists as
# the separate `--dangerously-skip-permissions` boolean flag, which
# AntigravityAgent never passes regardless of this setting - this allowlist
# is a second, independent guard against a typo'd/malicious config.yaml.
ANTIGRAVITY_ALLOWED_MODES = {"accept-edits", "plan", ""}

# The only `--sandbox` values CodexAgent may pass to the `codex` CLI.
# "danger-full-access" is deliberately excluded - that is Codex's equivalent
# of an unrestricted-write sandbox and, combined with the separate
# `--dangerously-bypass-approvals-and-sandbox` flag (which CodexAgent never
# passes regardless of this setting), would remove the filesystem/network
# containment this orchestrator relies on. "workspace-write" is the default:
# it auto-permits edits inside the project directory but still denies
# network access and writes outside it; "read-only" is available for a
# read-only/inspection run.
CODEX_ALLOWED_SANDBOX_MODES = {"read-only", "workspace-write"}

# Supported provider implementations in the orchestrator registry.
AVAILABLE_AGENTS = ["claude-code", "antigravity", "codex"]

# Default provider failover order for autonomous mode.
DEFAULT_PROVIDER_ORDER = ["claude-code", "antigravity", "codex"]



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
class AntigravityAgentConfig:
    cli_path: str = ""  # empty = auto-detect ("agy" in PATH)
    model: str = ""  # empty = CLI default
    # Passed as --mode to `agy`. "accept-edits" auto-approves file edits but
    # still denies anything else (e.g. shell commands) by default - NEVER set
    # this up to bypass permissions; that is only possible via the separate
    # --dangerously-skip-permissions flag, which this adapter never passes
    # regardless of config (see orchestrator/agents/antigravity.py).
    mode: str = "accept-edits"
    timeout_seconds: int = 1800


@dataclass
class CodexAgentConfig:
    cli_path: str = ""  # empty = auto-detect ("codex" in PATH)
    model: str = ""  # empty = CLI default
    # Passed as --sandbox to `codex exec`. "workspace-write" auto-approves
    # file edits inside the project directory but still denies network
    # access and writes outside it - NEVER set this to "danger-full-access";
    # config.py rejects that at load time, and this adapter never passes
    # --dangerously-bypass-approvals-and-sandbox regardless of this setting.
    sandbox_mode: str = "workspace-write"
    timeout_seconds: int = 1800


@dataclass
class GitConfig:
    # Default for every task/run that does not explicitly override
    # `auto_commit=...` itself (CLI `--commit`/`--no-commit`, or the API/
    # service `auto_commit` param) - see OrchestratorService.submit()/
    # run_autonomous(). An explicit per-call override always wins over this
    # default in either direction; it is never additionally ANDed with it
    # (see runner.py `_maybe_commit` / autonomous.py `_commit_if_ready`).
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
    provider_order: list[str] = field(default_factory=lambda: list(DEFAULT_PROVIDER_ORDER))
    projects: dict[str, ProjectEntry] = field(default_factory=dict)
    claude_code: ClaudeCodeAgentConfig = field(default_factory=ClaudeCodeAgentConfig)
    antigravity: AntigravityAgentConfig = field(default_factory=AntigravityAgentConfig)
    codex: CodexAgentConfig = field(default_factory=CodexAgentConfig)
    git: GitConfig = field(default_factory=GitConfig)
    testing: TestingConfig = field(default_factory=TestingConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    # Prázdné = kořen pracovního prostoru je nadřazený adresář tohoto projektu
    # (typicky D:\orchestrator). Žádný projekt (registrovaný ani zadaný jako
    # syrová cesta) nesmí ležet mimo tento adresář - viz _ensure_within_workspace.
    workspace_root: str = ""
    source_path: Optional[Path] = None

    def resolve_path(self, relative_or_absolute: str) -> Path:
        p = Path(relative_or_absolute)
        if p.is_absolute():
            return p
        return PROJECT_ROOT / p

    @property
    def workspace_root_dir(self) -> Path:
        raw = self.workspace_root or str(PROJECT_ROOT.parent)
        return Path(raw).resolve()

    def _ensure_within_workspace(self, path: Path) -> Path:
        resolved = path.resolve()
        root = self.workspace_root_dir
        if resolved != root and root not in resolved.parents:
            raise ValueError(
                f"Cesta '{resolved}' je mimo povolený pracovní prostor "
                f"'{root}' (workspace_root v config.yaml). Orchestrátor a "
                "ClaudeCodeAgent smí pracovat jen uvnitř tohoto adresáře."
            )
        return resolved

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
            # Registered entries are already validated against workspace_root
            # in load_config() at startup - re-checked here too (cheap, and
            # protects callers that construct a Config by hand).
            entry = self.projects[project_ref]
            self._ensure_within_workspace(Path(entry.path))
            return entry
        p = Path(project_ref)
        if p.exists():
            resolved = self._ensure_within_workspace(p)
            return ProjectEntry(name=project_ref, path=str(resolved))
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

    ag_raw = raw.get("antigravity") or {}
    antigravity_mode = ag_raw.get("mode", "accept-edits") or ""
    if antigravity_mode not in ANTIGRAVITY_ALLOWED_MODES:
        raise ValueError(
            f"config.yaml nastavuje antigravity.mode='{antigravity_mode}', ale to není mezi "
            f"povolenými hodnotami {sorted(ANTIGRAVITY_ALLOWED_MODES - {''})} (nebo prázdné). "
            "Non-interactive běh nikdy nesmí obcházet kontrolu oprávnění."
        )
    antigravity = AntigravityAgentConfig(
        cli_path=ag_raw.get("cli_path", ""),
        model=ag_raw.get("model", ""),
        mode=antigravity_mode,
        timeout_seconds=int(ag_raw.get("timeout_seconds", 1800)),
    )

    codex_raw = raw.get("codex") or {}
    codex_sandbox_mode = codex_raw.get("sandbox_mode", "workspace-write") or ""
    if codex_sandbox_mode not in CODEX_ALLOWED_SANDBOX_MODES:
        raise ValueError(
            f"config.yaml nastavuje codex.sandbox_mode='{codex_sandbox_mode}', ale to není mezi "
            f"povolenými hodnotami {sorted(CODEX_ALLOWED_SANDBOX_MODES)}. Non-interactive běh "
            "nikdy nesmí obcházet sandbox (danger-full-access je zakázané)."
        )
    codex = CodexAgentConfig(
        cli_path=codex_raw.get("cli_path", ""),
        model=codex_raw.get("model", ""),
        sandbox_mode=codex_sandbox_mode,
        timeout_seconds=int(codex_raw.get("timeout_seconds", 1800)),
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

    provider_order_raw = raw.get("provider_order")
    if provider_order_raw is None:
        provider_order_raw = raw.get("failover_order")

    if provider_order_raw is not None:
        if isinstance(provider_order_raw, str):
            provider_order = [s.strip() for s in provider_order_raw.split(",") if s.strip()]
        elif isinstance(provider_order_raw, list):
            provider_order = [str(x) for x in provider_order_raw]
        else:
            raise ValueError(
                f"config.yaml nastavuje neplatný provider_order: '{provider_order_raw}' "
                "(musí být seznam nebo řetězec oddělený čárkami)."
            )
    else:
        provider_order = list(DEFAULT_PROVIDER_ORDER)

    for p_name in provider_order:
        if p_name not in AVAILABLE_AGENTS:
            raise ValueError(
                f"config.yaml nastavuje neznámého providera '{p_name}' v provider_order. "
                f"Podporovaní provideři jsou: {', '.join(AVAILABLE_AGENTS)}."
            )

    config = Config(
        default_agent=raw.get("default_agent", "claude-code"),
        provider_order=provider_order,
        projects=projects,
        claude_code=claude_code,
        antigravity=antigravity,
        codex=codex,
        git=git,
        testing=testing,
        api=api,
        paths=paths,
        workspace_root=raw.get("workspace_root", ""),
        source_path=cfg_path,
    )

    # Fail fast: reject any registered project that lies outside
    # workspace_root, at load time, so a bad config.yaml is caught before
    # any task runs (not just when that particular project is used).
    for name, entry in projects.items():
        try:
            config._ensure_within_workspace(Path(entry.path))
        except ValueError as e:
            raise ValueError(f"projekt '{name}' v config.yaml: {e}") from e

    return config
