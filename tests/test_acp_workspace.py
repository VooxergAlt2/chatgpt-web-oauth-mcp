import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError
from chatgpt_web_oauth_mcp.acp_workspace import (
    AcpWorkspace, capture_source_snapshot, validate_permitted_paths,
)


def git(cwd, *args):
    return subprocess.check_output(
        ["/usr/bin/git", "-C", str(cwd), *args], stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    ).decode().strip()


@pytest.fixture
def repository(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "config", "user.name", "ACP test")
    git(main, "config", "user.email", "acp-test@example.invalid")
    (main / "source.txt").write_text("before\n")
    (main / "nested").mkdir()
    (main / "nested" / "other.txt").write_text("keep\n")
    (main / ".gitignore").write_text("ignored*\n")
    git(main, "add", ".")
    git(main, "commit", "-qm", "baseline")
    source = tmp_path / "worktree"
    git(main, "worktree", "add", "-qb", "writer-test", str(source))
    return main, source, tmp_path / "state"


def prepare(repository, paths=("source.txt",)):
    _, source, state = repository
    return AcpWorkspace.prepare(source, paths, state)


def test_detached_copy_has_exact_baseline_without_real_git_metadata(repository):
    main, source, _ = repository
    with prepare(repository) as workspace:
        assert workspace.source == source
        assert workspace.baseline_head == git(source, "rev-parse", "HEAD")
        assert not (workspace.root / ".git").exists()
        assert not (workspace.baseline_root / ".git").exists()
        assert workspace.validate_changes() == ()
        assert workspace.result_manifest()["source.txt"]["size"] == 7
        assert not (workspace.baseline_root.stat().st_mode & 0o222)
        assert not ((workspace.baseline_root / "source.txt").stat().st_mode & 0o222)
        assert (workspace.root / "source.txt").stat().st_nlink == 1
        (workspace.root / "source.txt").write_text("candidate\n")
        assert (source / "source.txt").read_text() == "before\n"
        assert (main / "source.txt").read_text() == "before\n"
        assert (workspace.baseline_root / "source.txt").read_text() == "before\n"
        container = workspace.root.parent
    assert not container.exists()


def test_promote_modified_added_deleted_files_only_after_digest_and_cas(repository):
    main, source, _ = repository
    metadata = (source / ".git").read_bytes()
    head = git(source, "rev-parse", "HEAD")
    with prepare(repository, ("source.txt", "nested/other.txt", "new/deep/result.py")) as workspace:
        (workspace.root / "source.txt").write_text("after\n")
        (workspace.root / "nested" / "other.txt").unlink()
        (workspace.root / "new" / "deep").mkdir(parents=True)
        (workspace.root / "new" / "deep" / "result.py").write_text("print(42)\n")
        digest = workspace.snapshot_digest()
        assert workspace.promote(expected_digest=digest) == (
            "nested/other.txt", "new/deep/result.py", "source.txt")
        assert (source / "source.txt").read_text() == "after\n"
        assert not (source / "nested" / "other.txt").exists()
        assert (source / "new" / "deep" / "result.py").read_text() == "print(42)\n"
        assert git(source, "rev-parse", "HEAD") == head
        assert (source / ".git").read_bytes() == metadata
        assert git(main, "status", "--porcelain") == ""
        with pytest.raises(AcpStateError, match="already_promoted"):
            workspace.promote()


def test_noop_promotion_is_valid(repository):
    with prepare(repository) as workspace:
        assert workspace.promote(expected_digest=workspace.snapshot_digest()) == ()


@pytest.mark.parametrize("paths", [
    [], ["."], ["../source.txt"], ["/etc/passwd"], ["nested/../source.txt"],
    ["nested//other.txt"], ["nested/./other.txt"], [".git"], [".git/config"],
    ["nested/.GIT/config"], ["*.py"], ["source?.txt"], ["source[0].txt"],
    [" source.txt"], ["source.txt\n"], ["nested\\other.txt"],
    ["nested", "nested/other.txt"],
])
def test_permitted_paths_require_explicit_canonical_files(paths):
    with pytest.raises(AcpStateError):
        validate_permitted_paths(paths)


def test_directory_scope_rejected(repository):
    with pytest.raises(AcpStateError, match="scope_is_directory"):
        prepare(repository, ("nested",))


def test_main_repository_is_never_a_writer_target(repository):
    main, _, state = repository
    with pytest.raises(AcpStateError, match="linked_worktree_required"):
        AcpWorkspace.prepare(main, ["source.txt"], state)


def test_worktree_subdirectory_is_not_a_writer_root(repository):
    _, source, state = repository
    with pytest.raises(AcpStateError):
        AcpWorkspace.prepare(source / "nested", ["other.txt"], state)


@pytest.mark.parametrize("mutation", ["tracked", "untracked", "ignored", "index"])
def test_admission_requires_clean_source_including_ignored_files(repository, mutation):
    _, source, _ = repository
    if mutation == "tracked":
        (source / "source.txt").write_text("dirty\n")
    elif mutation == "untracked":
        (source / "new.txt").write_text("untracked\n")
    elif mutation == "ignored":
        (source / "ignored-secret").write_text("SYNTHETIC_SECRET")
    else:
        (source / "source.txt").write_text("staged\n")
        git(source, "add", "source.txt")
    with pytest.raises(AcpStateError, match="source_dirty"):
        prepare(repository)


@pytest.mark.parametrize("mutation", ["symlink", "hardlink", "fifo", "socket"])
def test_special_source_files_are_not_copied(repository, mutation, tmp_path):
    _, source, _ = repository
    outside = tmp_path / "outside"
    outside.write_text("SYNTHETIC_ONLY")
    target = source / "source.txt"
    target.unlink()
    listener = None
    try:
        if mutation == "symlink":
            target.symlink_to(outside)
        elif mutation == "hardlink":
            os.link(outside, target)
        elif mutation == "fifo":
            os.mkfifo(target)
        else:
            import socket
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(target))
        with pytest.raises(AcpStateError, match="regular_file_required"):
            prepare(repository)
        assert outside.read_text() == "SYNTHETIC_ONLY"
    finally:
        if listener is not None:
            listener.close()


@pytest.mark.parametrize("mutation", ["symlink", "hardlink", "fifo", "socket", "git", "mode"])
def test_native_invalid_result_cannot_touch_source(repository, mutation, tmp_path):
    _, source, _ = repository
    outside = tmp_path / "outside"
    outside.write_text("SYNTHETIC_ONLY")
    listener = None
    with prepare(repository) as workspace:
        target = workspace.root / "source.txt"
        target.unlink()
        try:
            if mutation == "symlink":
                target.symlink_to(outside)
            elif mutation == "hardlink":
                os.link(outside, target)
            elif mutation == "fifo":
                os.mkfifo(target)
            elif mutation == "socket":
                import socket
                listener = socket.socket(socket.AF_UNIX)
                listener.bind(str(target))
            elif mutation == "git":
                target.write_text("after\n")
                (workspace.root / ".git").write_text("gitdir: /host/metadata\n")
            else:
                target.write_text("after\n")
                target.chmod(0o4755)
            with pytest.raises(AcpStateError):
                workspace.promote()
            assert (source / "source.txt").read_text() == "before\n"
            assert outside.read_text() == "SYNTHETIC_ONLY"
        finally:
            if listener is not None:
                listener.close()


def test_out_of_scope_edit_aborts_entire_promotion(repository):
    _, source, _ = repository
    with prepare(repository) as workspace:
        (workspace.root / "source.txt").write_text("allowed\n")
        (workspace.root / "nested" / "other.txt").write_text("forbidden\n")
        evidence = workspace.inspect_changes_unchecked(max_entries=1)
        assert evidence["outside_scope_count"] == 1
        assert evidence["changed_file_count"] == 2
        assert evidence["truncated"] is True
        assert evidence["changes"][0]["path"] == "nested/other.txt"
        assert evidence["changes"][0]["kind"] == "modified"
        assert evidence["changes"][0]["in_scope"] is False
        with pytest.raises(AcpStateError, match="scope_violation"):
            workspace.promote()
        assert (source / "source.txt").read_text() == "before\n"
        assert (source / "nested" / "other.txt").read_text() == "keep\n"


def test_rejection_diagnostics_distinguish_mode_drift_without_disclosing_content(repository):
    with prepare(repository) as workspace:
        path = workspace.root / "nested" / "other.txt"
        before = stat.S_IMODE(path.stat().st_mode)
        path.chmod(0o600 if before != 0o600 else 0o644)
        evidence = workspace.inspect_changes_unchecked()
        change = evidence["changes"][0]
        assert change["kind"] == "mode_changed" and change["in_scope"] is False
        assert change["before"]["sha256"] == change["after"]["sha256"]
        assert change["before"]["mode"] != change["after"]["mode"]
        assert "keep" not in str(evidence)
        assert str(workspace.source) not in str(evidence)
        with pytest.raises(AcpStateError, match="scope_violation"):
            workspace.promote()


def test_rejection_diagnostics_never_follow_native_symlink(repository, tmp_path):
    with prepare(repository) as workspace:
        secret = tmp_path / "secret"
        secret.write_text("SYNTHETIC_SECRET")
        (workspace.root / "source.txt").unlink()
        (workspace.root / "source.txt").symlink_to(secret)
        evidence = workspace.inspect_changes_unchecked()
        assert evidence == {"available": False, "error_code": "acp_writer_diagnostics_unavailable"}
        assert "SYNTHETIC_SECRET" not in str(evidence) and str(secret) not in str(evidence)
        with pytest.raises(AcpStateError):
            workspace.promote()


def test_source_cas_preserves_external_user_edit(repository):
    _, source, _ = repository
    with prepare(repository) as workspace:
        (workspace.root / "source.txt").write_text("model\n")
        (source / "nested" / "other.txt").write_text("concurrent user\n")
        with pytest.raises(AcpStateError, match="source_changed"):
            workspace.promote()
        assert (source / "source.txt").read_text() == "before\n"
        assert (source / "nested" / "other.txt").read_text() == "concurrent user\n"


def test_result_digest_requires_exact_independently_verified_content(repository):
    _, source, _ = repository
    with prepare(repository) as workspace:
        (workspace.root / "source.txt").write_text("verified\n")
        digest = workspace.snapshot_digest()
        (workspace.root / "source.txt").write_text("later mutation\n")
        with pytest.raises(AcpStateError, match="result_changed"):
            workspace.promote(expected_digest=digest)
        assert (source / "source.txt").read_text() == "before\n"


def test_partial_promotion_failure_rolls_back_prior_atomic_replacement(repository, monkeypatch):
    import chatgpt_web_oauth_mcp.acp_workspace as module
    _, source, _ = repository
    with prepare(repository, ("source.txt", "nested/other.txt")) as workspace:
        (workspace.root / "source.txt").write_text("after\n")
        (workspace.root / "nested" / "other.txt").write_text("after other\n")
        original = module._replace_checked
        calls = []
        def fail_second(*args, **kwargs):
            calls.append(args[1])
            if len(calls) == 2:
                raise OSError("injected export failure")
            return original(*args, **kwargs)
        monkeypatch.setattr(module, "_replace_checked", fail_second)
        with pytest.raises(OSError, match="injected"):
            workspace.promote()
        assert len(calls) == 3
        assert (source / "source.txt").read_text() == "before\n"
        assert (source / "nested" / "other.txt").read_text() == "keep\n"
        assert git(source, "status", "--porcelain") == ""


def test_preparation_never_runs_git_filter_or_hooks(repository, tmp_path):
    _, source, _ = repository
    marker = tmp_path / "filter-ran"
    git(source, "config", "filter.evil.clean", f"touch {marker}; cat")
    (source / ".gitattributes").write_text("source.txt filter=evil\n")
    git(source, "add", ".gitattributes")
    git(source, "commit", "-qm", "attribute config")
    marker.unlink(missing_ok=True)
    with prepare(repository) as workspace:
        assert workspace.validate_changes() == ()
    assert not marker.exists()


def test_snapshot_size_bounds_are_fail_closed(repository, monkeypatch):
    import chatgpt_web_oauth_mcp.acp_workspace as module
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 2)
    with pytest.raises(AcpStateError, match="snapshot_limit"):
        prepare(repository)


def test_cleanup_does_not_follow_native_symlinks(repository, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("keep\n")
    workspace = prepare(repository)
    (workspace.root / "out-link").symlink_to(outside, target_is_directory=True)
    container = workspace.root.parent
    workspace.close()
    assert not container.exists()
    assert (outside / "sentinel").read_text() == "keep\n"
    with pytest.raises(AcpStateError, match="closed"):
        workspace.snapshot_digest()


def test_safe_source_snapshot_validates_promoted_result_without_git_filters(repository):
    _, source, _ = repository
    before = capture_source_snapshot(source)
    with prepare(repository) as workspace:
        assert workspace.baseline_manifest() == before["manifest"]
        (workspace.root / "source.txt").write_text("verified after\n")
        expected = workspace.result_manifest()
        digest = workspace.snapshot_digest()
        workspace.promote(expected_digest=digest)
        after = capture_source_snapshot(source, require_clean=False)
        assert after == {"head": before["head"], "manifest": expected, "digest": digest}
        with pytest.raises(AcpStateError, match="source_dirty"):
            capture_source_snapshot(source)


def test_new_private_file_permissions_are_preserved(repository):
    _, source, _ = repository
    with prepare(repository, ("new.txt",)) as workspace:
        (workspace.root / "new.txt").write_text("private new file\n")
        (workspace.root / "new.txt").chmod(0o600)
        workspace.promote()
        assert stat.S_IMODE((source / "new.txt").stat().st_mode) == 0o600


def test_cleanup_handles_native_directory_permission_changes(repository):
    workspace = prepare(repository)
    container = workspace.root.parent
    (workspace.root / "nested").chmod(0)
    workspace.root.chmod(0)
    workspace.close()
    assert not container.exists()


def test_directory_fsync_failure_after_replace_rolls_back_changed_file(repository, monkeypatch):
    import chatgpt_web_oauth_mcp.acp_workspace as module
    _, source, _ = repository
    with prepare(repository) as workspace:
        (workspace.root / "source.txt").write_text("after\n")
        source_identity = (source.stat().st_dev, source.stat().st_ino)
        original = module.os.fsync
        injected = []
        def fail_source_directory_once(fd):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == source_identity and not injected:
                injected.append(True)
                raise OSError("injected directory fsync failure")
            return original(fd)
        monkeypatch.setattr(module.os, "fsync", fail_source_directory_once)
        with pytest.raises(OSError, match="injected directory fsync"):
            workspace.promote()
        assert injected
        assert (source / "source.txt").read_text() == "before\n"
        assert git(source, "status", "--porcelain") == ""
