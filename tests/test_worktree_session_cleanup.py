from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import subprocess

import pytest

from chatgpt_web_oauth_mcp import server, session, tools_git_shell
from chatgpt_web_oauth_mcp.gitops import git_worktree_create
from chatgpt_web_oauth_mcp.pathing import resolve_cwd
from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore


def _call(tool, **kwargs):
    return (tool.fn if hasattr(tool, "fn") else tool)(**kwargs)


@pytest.fixture
def cleanup_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    for arguments in (
        ["init", "-b", "main"],
        ["config", "user.name", "Test User"],
        ["config", "user.email", "test@example.com"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    worktree = tmp_path / "worktree"
    assert git_worktree_create(cwd=repo, path=str(worktree), mode="detached")["success"]
    store = SessionCheckpointStore(path=tmp_path / "checkpoints.json", ttl_seconds=86400)
    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(session, "registry", session.SessionRegistry())
    token = session.bind_session("openai:cleanup")
    try:
        yield repo, worktree, store
    finally:
        session.reset_session_binding(token)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("explicit_cwd", [False, True])
def test_remove_current_worktree_repairs_cwd_and_resume(cleanup_repo, nested, explicit_cwd):
    repo, worktree, store = cleanup_repo
    current = worktree / "sub" if nested else worktree
    current.mkdir(exist_ok=True)
    session.set_default_cwd(current)
    session.registry.set_default_cwd(current, "openai:foreign")
    saved = store.put(
        session_key="openai:cleanup",
        checkpoint={
            "goal": "cleanup",
            "next_action": "review result",
            "cwd": str(current),
            "runtime": {"cwd": str(current), "last_tool": "job_status"},
        },
    )
    foreign = store.put(
        session_key="openai:foreign", checkpoint={"cwd": str(current), "goal": "other"}
    )

    result = _call(
        server.git_worktree_remove,
        path=str(worktree),
        **({"cwd": str(repo)} if explicit_cwd else {}),
    )

    assert result["success"] is True
    assert result["remaining_cwd"] == str(repo)
    assert result["session_cwd_update"] == {
        "previous_cwd": str(current), "cwd": str(repo), "reason": "worktree_removed",
    }
    assert not worktree.exists()
    assert resolve_cwd(None, repo.parent) == repo
    saved["cwd"] = saved["runtime"]["cwd"] = str(repo)
    assert store.get("openai:cleanup") == saved
    assert store.get("openai:foreign") == foreign
    assert session.registry.get_default_cwd("openai:foreign") == current

    session.set_default_cwd(None)
    assert _call(server.session_resume)["resumable"] is True
    assert session.get_default_cwd() == repo


@pytest.mark.parametrize("current_kind", ["unset", "repo", "sibling", "missing"])
def test_remove_preserves_unrelated_default_cwd(cleanup_repo, current_kind):
    repo, worktree, store = cleanup_repo
    current = {
        "unset": None,
        "repo": repo,
        "sibling": worktree.with_name("worktree-sibling"),
        "missing": repo.parent / "unrelated-missing",
    }[current_kind]
    if current_kind == "sibling":
        current.mkdir()
    session.set_default_cwd(current)
    saved = store.put(session_key="openai:cleanup", checkpoint={"cwd": str(current)})

    result = _call(server.git_worktree_remove, path=str(worktree), cwd=str(repo))

    assert result["success"] is True
    assert "session_cwd_update" not in result
    assert session.get_default_cwd() == current
    assert store.get("openai:cleanup") == saved


def test_failed_remove_and_explicit_missing_cwd_are_not_repaired(cleanup_repo):
    repo, worktree, store = cleanup_repo
    session.set_default_cwd(worktree)
    saved = store.put(session_key="openai:cleanup", checkpoint={"cwd": str(worktree)})
    (worktree / "seed.txt").write_text("dirty\n", encoding="utf-8")
    missing = repo.parent / "missing"
    assert resolve_cwd(str(missing), repo) == missing

    invalid = _call(server.git_worktree_remove, path=str(worktree), cwd=str(missing))
    dirty = _call(server.git_worktree_remove, path=str(worktree), cwd=str(repo))

    assert invalid["success"] is False
    assert dirty["success"] is False
    assert dirty["error"]["code"] == "worktree_dirty"
    assert session.get_default_cwd() == worktree
    assert store.get("openai:cleanup") == saved
    assert worktree.is_dir()


def test_cleanup_does_not_overwrite_concurrent_default_cwd(cleanup_repo, monkeypatch):
    repo, worktree, store = cleanup_repo
    session.set_default_cwd(worktree)
    saved = store.put(session_key="openai:cleanup", checkpoint={"cwd": str(worktree)})
    other = repo.parent / "other"
    other.mkdir()
    remove_impl = tools_git_shell.git_worktree_remove_impl

    def remove_and_switch(**kwargs):
        result = remove_impl(**kwargs)
        session.set_default_cwd(other)
        return result

    monkeypatch.setattr(tools_git_shell, "git_worktree_remove_impl", remove_and_switch)
    result = _call(server.git_worktree_remove, path=str(worktree), cwd=str(repo))
    assert result["success"] is True
    assert "session_cwd_update" not in result
    assert session.get_default_cwd() == other
    assert store.get("openai:cleanup") == saved


def test_checkpoint_write_failure_is_reported_after_successful_remove(cleanup_repo, monkeypatch):
    repo, worktree, store = cleanup_repo
    session.set_default_cwd(worktree)

    def fail(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "relocate_removed_cwd", fail)
    result = _call(server.git_worktree_remove, path=str(worktree), cwd=str(repo))
    assert result["success"] is True
    assert session.get_default_cwd() == repo
    assert "disk full" in result["resume_checkpoint_warning"]


@pytest.mark.parametrize("matching_field", ["cwd", "runtime"])
def test_checkpoint_cleanup_preserves_other_state_and_sessions(cleanup_repo, matching_field):
    repo, worktree, store = cleanup_repo
    checkpoint = {
        "cwd": str(repo),
        "goal": "keep semantic plan",
        "next_action": "keep next action",
        "runtime": {
            "cwd": str(repo),
            "jobs": {"job": {"cwd": str(worktree), "terminal": True}},
            "delegates": {"delegate": {"cwd": str(worktree), "status": "succeeded"}},
            "last_tool": "await_job",
        },
    }
    if matching_field == "cwd":
        checkpoint["cwd"] = str(worktree / "sub")
    else:
        checkpoint["runtime"]["cwd"] = str(worktree / "sub")
    saved = store.put(session_key="openai:cleanup", checkpoint=checkpoint)
    foreign = store.put(session_key="openai:foreign", checkpoint=checkpoint)
    expected = deepcopy(saved)
    expected["cwd"] = expected["runtime"]["cwd"] = str(repo)

    assert store.relocate_removed_cwd(
        session_key="openai:cleanup", removed_cwd=worktree, replacement_cwd=repo,
    ) is True
    assert store.get("openai:cleanup") == expected
    assert store.get("openai:foreign") == foreign
    assert store.relocate_removed_cwd(
        session_key="openai:cleanup", removed_cwd=worktree, replacement_cwd=repo,
    ) is False
    assert store.relocate_removed_cwd(
        session_key="openai:missing", removed_cwd=worktree, replacement_cwd=repo,
    ) is False

