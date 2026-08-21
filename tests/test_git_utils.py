import pytest

from orchestrator import git_utils


def test_is_git_repo_true(git_repo):
    assert git_utils.is_git_repo(git_repo) is True


def test_is_git_repo_false(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert git_utils.is_git_repo(not_a_repo) is False


def test_no_changes_initially(git_repo):
    assert git_utils.has_uncommitted_changes(git_repo) is False


def test_commit_after_change(git_repo):
    (git_repo / "new_file.txt").write_text("hello\n", encoding="utf-8")
    assert git_utils.has_uncommitted_changes(git_repo) is True

    commit_hash = git_utils.commit(git_repo, "add new_file.txt")
    assert len(commit_hash) == 40
    assert git_utils.has_uncommitted_changes(git_repo) is False


def test_commit_with_nothing_to_commit_raises(git_repo):
    with pytest.raises(git_utils.GitError):
        git_utils.commit(git_repo, "should fail")
