from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import subprocess

from chatgpt_web_oauth_mcp.code_graph.backend import BuildResult, JoernDockerBackend
from chatgpt_web_oauth_mcp.code_graph.cache import CodeGraphCache
from chatgpt_web_oauth_mcp.code_graph.identity import create_graph_identity
from chatgpt_web_oauth_mcp.code_graph.snapshot import resolve_git_snapshot
from chatgpt_web_oauth_mcp.code_graph.worker import (
    ANALYSIS_OPTIONS,
    GRAPH_SCHEMA_VERSION,
    run_worker,
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
    (repo / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _args(repo: Path, state: Path) -> Namespace:
    snapshot = resolve_git_snapshot(repo, "HEAD")
    return Namespace(
        repository_root=str(repo),
        repository_id=snapshot.repository_id,
        tree_sha=snapshot.tree_sha,
        state_dir=str(state),
        docker_binary="docker",
        joern_image=PINNED,
        joern_version="4.0.640",
        memory_mb=8192,
        cpus=4,
        pids_limit=512,
        tmpfs_mb=2048,
        build_timeout_seconds=1800,
        cache_max_bytes=1024 * 1024 * 1024,
        cache_max_graphs=4,
        staging_ttl_seconds=3600,
    )


def test_worker_publishes_fake_backend_and_cleans_snapshot(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    args = _args(repo, state)

    def fake_build(self, *, source_dir: Path, output_dir: Path) -> BuildResult:
        assert (source_dir / "app.py").is_file()
        assert source_dir != repo
        payload = output_dir / "cpg.bin"
        payload.write_bytes(b"fake-cpg")
        return BuildResult(
            payload_path=payload,
            duration_seconds=0.25,
            stdout_tail="ok",
            stderr_tail="",
        )

    monkeypatch.setattr(JoernDockerBackend, "build", fake_build)
    assert run_worker(args) == 0

    snapshot = resolve_git_snapshot(repo, "HEAD")
    identity = create_graph_identity(
        repository_id=snapshot.repository_id,
        git_tree_sha=snapshot.tree_sha,
        analyzer_id=(
            "joern:4.0.640@"
            "sha256:71a7af77e78d4a84cab0291d2fc1a1490bb27f60fbb64e4f0f1e3191e98b6bc3"
        ),
        schema_version=GRAPH_SCHEMA_VERSION,
        options=ANALYSIS_OPTIONS,
    )
    cache = CodeGraphCache(state)
    entry = cache.status(snapshot.repository_id, identity.graph_id, expected_identity=identity)
    assert entry.status.value == "ready"
    assert entry.manifest is not None
    assert entry.manifest.payload_size_bytes == len(b"fake-cpg")
    scratch = cache.base_dir / "scratch"
    assert not scratch.exists() or list(scratch.iterdir()) == []

    # Repeating the same worker is an idempotent cache hit and does not call backend again.
    def should_not_build(*args, **kwargs):
        raise AssertionError("backend build must not run on cache hit")

    monkeypatch.setattr(JoernDockerBackend, "build", should_not_build)
    assert run_worker(args) == 0


def test_worker_failure_does_not_publish_and_cleans_snapshot(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    args = _args(repo, state)

    def fail_build(self, *, source_dir: Path, output_dir: Path):
        raise RuntimeError("synthetic build failure")

    monkeypatch.setattr(JoernDockerBackend, "build", fail_build)
    assert run_worker(args) == 1

    snapshot = resolve_git_snapshot(repo, "HEAD")
    cache = CodeGraphCache(state)
    graphs = list(cache.graphs_dir(snapshot.repository_id).iterdir())
    assert graphs == []
    scratch = cache.base_dir / "scratch"
    assert not scratch.exists() or list(scratch.iterdir()) == []
