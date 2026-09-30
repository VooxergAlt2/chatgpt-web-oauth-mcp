from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from chatgpt_web_oauth_mcp.code_graph.backend import (
    BackendStatus,
    JoernBackendConfig,
    JoernDockerBackend,
)
from chatgpt_web_oauth_mcp.code_graph.snapshot import (
    GitSnapshotError,
    cleanup_exported_snapshot,
    export_git_snapshot,
    resolve_git_snapshot,
)


PINNED = (
    "ghcr.io/joernio/joern@"
    "sha256:71a7af77e78d4a84cab0291d2fc1a1490bb27f60fbb64e4f0f1e3191e98b6bc3"
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.py").write_text("VALUE = 'committed'\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _config(**overrides) -> JoernBackendConfig:
    values = {
        "enabled": True,
        "docker_binary": "docker",
        "image": PINNED,
        "version": "4.0.640",
        "memory_mb": 8192,
        "cpus": 4,
        "pids_limit": 512,
        "tmpfs_mb": 2048,
        "build_timeout_seconds": 1800,
    }
    values.update(overrides)
    return JoernBackendConfig(**values)


def test_snapshot_identity_and_export_use_exact_committed_tree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    committed_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    (repo / "tracked.py").write_text("VALUE = 'dirty'\n", encoding="utf-8")
    (repo / "untracked.py").write_text("SHOULD_NOT_APPEAR = True\n", encoding="utf-8")

    snapshot = resolve_git_snapshot(repo, "HEAD")
    assert snapshot.tree_sha == committed_tree
    assert len(snapshot.repository_id) == 64

    source = export_git_snapshot(snapshot, scratch_root=tmp_path / "scratch")
    try:
        assert (source / "tracked.py").read_text(encoding="utf-8") == "VALUE = 'committed'\n"
        assert not (source / "untracked.py").exists()
    finally:
        cleanup_exported_snapshot(source)
    assert list((tmp_path / "scratch").iterdir()) == []


def test_snapshot_rejects_invalid_ref_and_non_repo(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(GitSnapshotError):
        resolve_git_snapshot(repo, "-bad-ref")
    with pytest.raises(GitSnapshotError):
        resolve_git_snapshot(repo, "refs/heads/does-not-exist")
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(GitSnapshotError):
        resolve_git_snapshot(outside, "HEAD")


def test_analyzer_id_contains_version_and_full_digest() -> None:
    config = _config()
    assert config.image_digest == (
        "sha256:71a7af77e78d4a84cab0291d2fc1a1490bb27f60fbb64e4f0f1e3191e98b6bc3"
    )
    assert config.analyzer_id == f"joern:4.0.640@{config.image_digest}"


def test_build_argv_enforces_hardened_offline_container(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    backend = JoernDockerBackend(_config())
    monkeypatch.setattr(backend, "_docker_binary", lambda: "/usr/bin/docker")

    argv = backend.build_argv(
        source_dir=source,
        output_dir=output,
        container_name="opss-codegraph-test",
    )

    assert argv[:3] == ["/usr/bin/docker", "run", "--rm"]
    for expected in (
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "8192m",
        "--cpus",
        "4",
        "--pids-limit",
        "512",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env",
        "HOME=/tmp/joern-home",
        "--tmpfs",
        "/tmp:rw,exec,size=2048m",
    ):
        assert expected in argv
    mounts = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--mount"]
    assert any("dst=/src" in mount and mount.endswith(",readonly") for mount in mounts)
    assert any("dst=/out" in mount and not mount.endswith(",readonly") for mount in mounts)
    assert PINNED in argv
    assert "pull" not in argv


def test_status_never_pulls_missing_image(monkeypatch) -> None:
    backend = JoernDockerBackend(_config())
    monkeypatch.setattr(backend, "_docker_binary", lambda: "/usr/bin/docker")
    calls: list[list[str]] = []

    def fake_capture(argv, *, timeout_seconds=15.0):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="No such image")

    monkeypatch.setattr(backend, "_capture", fake_capture)
    status = backend.status()

    assert status.available is False
    assert status.error_code == "joern_image_unavailable"
    assert len(calls) == 1
    assert calls[0][1:3] == ["image", "inspect"]
    assert all("pull" not in call for call in calls)
