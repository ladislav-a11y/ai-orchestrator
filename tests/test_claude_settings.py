import json
from pathlib import Path

from orchestrator.claude_settings import (
    ALLOWED_RULES,
    DENIED_RULES,
    SETTINGS_FILENAME,
    build_settings,
    ensure_project_claude_settings,
)


def test_allows_project_file_access_and_creation():
    assert "Read" in ALLOWED_RULES
    assert "Edit" in ALLOWED_RULES
    assert "Write" in ALLOWED_RULES


def test_allows_local_python_and_test_runners():
    assert "Bash(python:*)" in ALLOWED_RULES
    assert "Bash(pytest:*)" in ALLOWED_RULES
    assert any(".venv" in rule for rule in ALLOWED_RULES)


def test_allows_only_the_non_destructive_git_commands():
    expected = {
        "Bash(git init:*)",
        "Bash(git status:*)",
        "Bash(git diff:*)",
        "Bash(git add:*)",
        "Bash(git log:*)",
    }
    assert expected.issubset(set(ALLOWED_RULES))
    # None of the allowed rules grant a bare/unrestricted Bash or PowerShell.
    for rule in ALLOWED_RULES:
        assert rule not in ("Bash", "Bash(*)", "Bash(*:*)")


def test_does_not_allow_agent_to_commit():
    # Committing is exclusively the orchestrator's own job (see AGENTS.md
    # rule 11) - the agent must never be auto-approved to run `git commit`
    # itself, only to inspect/stage the tree.
    assert "Bash(git commit:*)" not in ALLOWED_RULES


def test_denies_agent_from_committing():
    assert "Bash(git commit:*)" in DENIED_RULES


def test_denies_push_and_force_push():
    assert "Bash(git push:*)" in DENIED_RULES


def test_denies_hard_reset_and_clean():
    assert "Bash(git reset --hard:*)" in DENIED_RULES
    assert "Bash(git clean -fd:*)" in DENIED_RULES
    assert "Bash(git clean -fdx:*)" in DENIED_RULES


def test_denies_deleting_dot_git():
    assert any(".git" in rule and "rm" in rule.lower() for rule in DENIED_RULES)
    assert any(".git" in rule and "remove-item" in rule.lower() for rule in DENIED_RULES)


def test_denies_history_rewriting_commands():
    assert "Bash(git rebase:*)" in DENIED_RULES
    assert "Bash(git filter-branch:*)" in DENIED_RULES
    assert "Bash(git commit --amend:*)" in DENIED_RULES
    assert "Bash(git branch -D:*)" in DENIED_RULES


def test_settings_never_reference_bypass_permissions():
    settings = build_settings()
    dumped = json.dumps(settings)
    assert "bypassPermissions" not in dumped
    assert "--dangerously-skip-permissions" not in dumped
    assert "--allow-dangerously-skip-permissions" not in dumped


def test_ensure_project_claude_settings_creates_file(tmp_path: Path):
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()

    settings_path = ensure_project_claude_settings(project_dir)

    assert settings_path == project_dir / ".claude" / SETTINGS_FILENAME
    assert settings_path.exists()
    content = json.loads(settings_path.read_text(encoding="utf-8"))
    assert content == build_settings()


def test_ensure_project_claude_settings_does_not_overwrite_existing(tmp_path: Path):
    project_dir = tmp_path / "myproj"
    claude_dir = project_dir / ".claude"
    claude_dir.mkdir(parents=True)
    custom = claude_dir / SETTINGS_FILENAME
    custom.write_text('{"permissions": {"allow": ["Bash(npm test:*)"]}}', encoding="utf-8")

    ensure_project_claude_settings(project_dir)

    content = json.loads(custom.read_text(encoding="utf-8"))
    assert content == {"permissions": {"allow": ["Bash(npm test:*)"]}}


def test_ensure_project_claude_settings_creates_missing_project_dir(tmp_path: Path):
    project_dir = tmp_path / "brand-new"
    assert not project_dir.exists()

    settings_path = ensure_project_claude_settings(project_dir)

    assert settings_path.exists()
