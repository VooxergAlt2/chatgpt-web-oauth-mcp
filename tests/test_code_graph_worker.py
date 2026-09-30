from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import subprocess

from chatgpt_web_oauth_mcp.code_graph.backend import BuildResult, JoernDockerBackend
from chatgpt_web_oauth_mcp.code_graph.cache import CodeGraphCache
from chatgpt_web_oauth_mcp.code_graph import worker as worker_module
from chatgpt_web_oauth_mcp.code_graph.identity import create_graph_identity
from chatgpt_web_oauth_mcp.code_graph.models import GCSummary
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


def test_worker_cleans_query_servers_for_gc_evictions(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    args = _args(repo, state)
    evicted = "a" * 64

    def fake_build(self, *, source_dir: Path, output_dir: Path) -> BuildResult:
        payload = output_dir / "cpg.bin"
        payload.write_bytes(b"fake-cpg")
        return BuildResult(
            payload_path=payload,
            duration_seconds=0.1,
            stdout_tail="ok",
            stderr_tail="",
        )

    monkeypatch.setattr(JoernDockerBackend, "build", fake_build)
    monkeypatch.setattr(
        CodeGraphCache,
        "gc",
        lambda self, *args, **kwargs: GCSummary(
            deleted_graph_ids=(evicted,),
            bytes_freed=123,
            retained_graph_count=1,
            retained_total_bytes=456,
        ),
    )
    calls: list[dict[str, object]] = []

    def fake_cleanup(
        docker_binary,
        state_dir,
        *,
        graph_ids=None,
        orphaned_only=False,
        strict=False,
    ):
        calls.append(
            {
                "docker_binary": docker_binary,
                "state_dir": state_dir,
                "graph_ids": tuple(graph_ids or ()),
                "orphaned_only": orphaned_only,
                "strict": strict,
            }
        )
        return len(tuple(graph_ids or ()))

    monkeypatch.setattr(worker_module, "cleanup_owned_query_servers", fake_cleanup)

    assert run_worker(args) == 0
    assert calls == [
        {
            "docker_binary": "docker",
            "state_dir": state,
            "graph_ids": (),
            "orphaned_only": True,
            "strict": True,
        },
        {
            "docker_binary": "docker",
            "state_dir": state,
            "graph_ids": (evicted,),
            "orphaned_only": False,
            "strict": True,
        },
    ]

    # Repeating the same worker is an idempotent cache hit and does not call backend again.
    def should_not_build(*args, **kwargs):
        raise AssertionError("backend build must not run on cache hit")

    monkeypatch.setattr(JoernDockerBackend, "build", should_not_build)
    assert run_worker(args) == 0
    assert calls[-1] == {
        "docker_binary": "docker",
        "state_dir": state,
        "graph_ids": (),
        "orphaned_only": True,
        "strict": True,
    }


def test_worker_retry_reconciles_orphan_after_post_gc_cleanup_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    args = _args(repo, state)
    evicted = "d" * 64

    def fake_build(self, *, source_dir: Path, output_dir: Path) -> BuildResult:
        payload = output_dir / "cpg.bin"
        payload.write_bytes(b"fake-cpg")
        return BuildResult(
            payload_path=payload,
            duration_seconds=0.1,
            stdout_tail="ok",
            stderr_tail="",
        )

    monkeypatch.setattr(JoernDockerBackend, "build", fake_build)
    monkeypatch.setattr(
        CodeGraphCache,
        "gc",
        lambda self, *args, **kwargs: GCSummary(
            deleted_graph_ids=(evicted,),
            bytes_freed=123,
            retained_graph_count=1,
            retained_total_bytes=456,
        ),
    )

    phase = {"post_gc_failed": False, "orphan_reconciled": False}

    def flaky_cleanup(
        docker_binary,
        state_dir,
        *,
        graph_ids=None,
        orphaned_only=False,
        strict=False,
    ):
        if graph_ids:
            phase["post_gc_failed"] = True
            raise RuntimeError("synthetic docker cleanup failure")
        if orphaned_only and phase["post_gc_failed"]:
            phase["orphan_reconciled"] = True
        return 0

    monkeypatch.setattr(worker_module, "cleanup_owned_query_servers", flaky_cleanup)

    assert run_worker(args) == 1
    assert phase["post_gc_failed"] is True

    def should_not_build(*args, **kwargs):
        raise AssertionError("cache-hit retry must not rebuild the graph")

    monkeypatch.setattr(JoernDockerBackend, "build", should_not_build)
    assert run_worker(args) == 0
    assert phase["orphan_reconciled"] is True


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
