from pathlib import Path

import pytest

from poc.hermes_agent.security import (
    SecurityBoundaryError,
    assert_command_is_safe,
    assert_prompt_is_safe,
    ensure_within_workspace,
    is_localhost_host,
)


@pytest.mark.parametrize(
    "command",
    [
        "claude --dangerously-skip-permissions",
        "codex --yolo",
        "git push --force origin main",
        "git reset --hard HEAD~1",
        "git rebase -i HEAD~3",
        "rm -rf /",
        "git commit --amend -m oops",
        "Remove-Item -Recurse -Force .\\workspace",
        "powershell -Command \"rmdir /s /q .\\workspace\"",
        "ri -Recurse -Force .\\workspace",
        "[System.IO.Directory]::Delete('.\\workspace', $true)",
        "python -c \"import shutil; shutil.rmtree('workspace')\"",
        "python -c \"import os; os.remove('workspace')\"",
        "python -c \"import subprocess; subprocess.run(['rmdir', 'workspace'])\"",
        "powershell -Command Invoke-Expression 'Remove-Item workspace'",
        "node -e \"require('fs').rmSync('workspace', {recursive:true})\"",
        "node -e \"const fs = require('node:fs'); fs.unlinkSync('workspace')\"",
        "python -c \"__import__('os').remove('workspace')\"",
        "node -e \"eval('require(\\\"fs\\\").rmSync(\\\"workspace\\\")')\"",
        "powershell -EncodedCommand JABwID0gUmVtb3ZlLUl0ZW0gLUNvbmZpcm0=",
        "git   commit -m forbidden",
        "perl -e \"unlink 'file'\"",
        "busybox rm -rf workspace",
        "ruby --eval \"File.delete('workspace')\"",
        "unknown-tool --execute destructive-payload",
        "busybox dd if=/dev/zero of=file",
        "robocopy C:\\empty C:\\workspace /MIR",
        "cp source target",
        "rsync source target",
        "echo hacked > target.txt",
        "cat source.txt >> target.txt",
        "printf payload | tee target.txt",
        "git diff > patch.txt",
        "Set-Content target.txt hacked",
        "Out-File target.txt",
        "[System.IO.File]::WriteAllText('target.txt','hacked')",
        "sc target.txt hacked",
        "ac target.txt hacked",
        "ri target.txt",
        "rni target.txt hacked",
        "ni target.txt",
        "cpi source.txt target.txt",
        "ci source.txt target.txt",
        "clc target.txt",
        "si target.txt hacked",
        "sp target.txt hacked",
        "rp target.txt",
        "echo safe; ac target.txt hacked",
        "echo safe | ri target.txt",
    ],
)
def test_assert_command_is_safe_rejects_destructive_commands(command):
    with pytest.raises(SecurityBoundaryError):
        assert_command_is_safe(command)


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff",
        "python -m pytest -q",
        "git log --oneline -5",
    ],
)
def test_assert_command_is_safe_allows_safe_commands(command):
    assert_command_is_safe(command)  # must not raise


@pytest.mark.parametrize("command", ["echo hello", "whoami", "unknown-tool payload"])
def test_assert_command_is_safe_rejects_unknown_commands_by_default(command):
    with pytest.raises(SecurityBoundaryError, match="allowlist"):
        assert_command_is_safe(command)


def test_assert_prompt_is_safe_allows_natural_language_prompt():
    assert_prompt_is_safe("hello, inspect the existing PoC and summarize the result")


def test_ensure_within_workspace_accepts_nested_path(tmp_path: Path):
    workspace = tmp_path / "workspace"
    project = workspace / "some-project"
    project.mkdir(parents=True)

    resolved = ensure_within_workspace(project, workspace)
    assert resolved == project.resolve()


def test_ensure_within_workspace_rejects_outside_path(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    with pytest.raises(SecurityBoundaryError):
        ensure_within_workspace(outside, workspace)


def test_is_localhost_host():
    assert is_localhost_host("127.0.0.1") is True
    assert is_localhost_host("localhost") is True
    assert is_localhost_host("::1") is True
    assert is_localhost_host("0.0.0.0") is False
