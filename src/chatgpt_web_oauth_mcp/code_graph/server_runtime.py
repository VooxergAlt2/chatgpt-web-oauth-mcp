from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterator, Sequence

from ..process_env import sanitized_child_env
from ..state_io import interprocess_file_lock
from .backend import CodeGraphBackendError, JoernBackendConfig


ROLE_LABEL = "io.opss.code_graph.role"
ROLE_VALUE = "joern-query-server"
GRAPH_LABEL = "io.opss.code_graph.graph_id"
ANALYZER_LABEL = "io.opss.code_graph.analyzer_id"
RUNTIME_SPEC_LABEL = "io.opss.code_graph.runtime_spec"
CPG_FINGERPRINT_LABEL = "io.opss.code_graph.cpg_fingerprint"
RUNTIME_POLICY_VERSION = 1
SERVER_PORT = 8080
MAX_REST_RESPONSE_BYTES = 1024 * 1024


class JoernQueryServerError(CodeGraphBackendError):
    """Base error for the persistent Joern query runtime."""


class JoernQueryTransportError(JoernQueryServerError):
    """Raised when the isolated REST runtime cannot be reached or decoded."""


class JoernQueryExecutionError(JoernQueryServerError):
    """Raised when Joern executes a query but reports a CPGQL failure."""


@dataclass(frozen=True)
class JoernQueryServerConfig:
    backend: JoernBackendConfig
    start_timeout_seconds: int = 30
    max_containers: int = 1
    lifecycle_lock_path: Path = field(
        default_factory=lambda: (
            Path(tempfile.gettempdir()) / "chatgpt-web-oauth-mcp-code-graph-query-runtime.lock"
        )
    )

    def __post_init__(self) -> None:
        if self.start_timeout_seconds <= 0:
            raise ValueError("start_timeout_seconds must be positive.")
        if self.max_containers <= 0:
            raise ValueError("max_containers must be positive.")
        object.__setattr__(
            self,
            "lifecycle_lock_path",
            Path(self.lifecycle_lock_path).expanduser().resolve(),
        )


@dataclass(frozen=True)
class QueryServerResult:
    stdout: str
    duration_seconds: float
    cold_start: bool


class JoernQueryServerRuntime:
    _lifecycle_thread_lock = threading.RLock()

    def __init__(self, config: JoernQueryServerConfig) -> None:
        self.config = config

    @contextmanager
    def _lifecycle_lock(self) -> Iterator[None]:
        with self._lifecycle_thread_lock:
            with interprocess_file_lock(
                self.config.lifecycle_lock_path,
                exclusive=True,
            ):
                yield

    @staticmethod
    def _validate_graph_id(graph_id: str) -> str:
        value = str(graph_id).strip().lower()
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("graph_id must be a lowercase 64-character SHA-256 digest.")
        return value

    def _docker_binary(self) -> str:
        value = self.config.backend.docker_binary
        if os.path.sep in value:
            candidate = Path(value).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            raise JoernQueryTransportError("Configured Docker executable is unavailable.")
        resolved = shutil.which(value)
        if not resolved:
            raise JoernQueryTransportError("Configured Docker executable is unavailable.")
        return resolved

    def container_name(self, graph_id: str) -> str:
        graph_id = self._validate_graph_id(graph_id)
        return f"opss-codegraph-server-{graph_id[:24]}"

    def _runtime_spec_id(self) -> str:
        backend = self.config.backend
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        payload = {
            "policy_version": RUNTIME_POLICY_VERSION,
            "analyzer_id": backend.analyzer_id,
            "image": backend.image,
            "memory_mb": backend.memory_mb,
            "cpus": backend.cpus,
            "pids_limit": backend.pids_limit,
            "tmpfs_mb": backend.tmpfs_mb,
            "user": f"{uid}:{gid}",
            "network": "none",
            "read_only_root": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
            "workdir": "/tmp",
            "environment": ["HOME=/tmp/joern-home"],
            "server_host": "127.0.0.1",
            "server_port": SERVER_PORT,
            "cpg_mount": "/cpg.bin:ro",
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _cpg_fingerprint(cpg_path: Path) -> str:
        cpg = Path(cpg_path).expanduser().resolve()
        if cpg.is_symlink() or not cpg.is_file():
            raise JoernQueryTransportError(f"CPG must be a real file: {cpg}")
        stat = cpg.stat()
        if stat.st_size <= 0:
            raise JoernQueryTransportError("CPG payload must not be empty.")
        raw = (
            f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
        ).encode("ascii")
        return hashlib.sha256(raw).hexdigest()

    def start_argv(self, *, graph_id: str, cpg_path: Path) -> list[str]:
        graph_id = self._validate_graph_id(graph_id)
        cpg = Path(cpg_path).expanduser().resolve()
        backend = self.config.backend
        if not backend.enabled:
            raise JoernQueryServerError("Code Graph is disabled by configuration.")
        cpg_fingerprint = self._cpg_fingerprint(cpg)
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        return [
            self._docker_binary(),
            "run",
            "-d",
            "--rm",
            "--name",
            self.container_name(graph_id),
            "--label",
            f"{ROLE_LABEL}={ROLE_VALUE}",
            "--label",
            f"{GRAPH_LABEL}={graph_id}",
            "--label",
            f"{ANALYZER_LABEL}={backend.analyzer_id}",
            "--label",
            f"{RUNTIME_SPEC_LABEL}={self._runtime_spec_id()}",
            "--label",
            f"{CPG_FINGERPRINT_LABEL}={cpg_fingerprint}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            f"{backend.memory_mb}m",
            "--cpus",
            str(backend.cpus),
            "--pids-limit",
            str(backend.pids_limit),
            "--user",
            f"{uid}:{gid}",
            "--workdir",
            "/tmp",
            "--env",
            "HOME=/tmp/joern-home",
            "--tmpfs",
            f"/tmp:rw,exec,size={backend.tmpfs_mb}m",
            "--mount",
            f"type=bind,src={cpg},dst=/cpg.bin,readonly",
            backend.image,
            "joern",
            "--server",
            "--server-host",
            "127.0.0.1",
            "--server-port",
            str(SERVER_PORT),
            "/cpg.bin",
        ]

    def exec_query_argv(self, *, graph_id: str, max_time_seconds: int | None = None) -> list[str]:
        timeout = max_time_seconds or self.config.backend.query_timeout_seconds
        return [
            self._docker_binary(),
            "exec",
            "-i",
            self.container_name(graph_id),
            "curl",
            "-sS",
            "--max-time",
            str(max(1, int(timeout))),
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
            f"http://127.0.0.1:{SERVER_PORT}/query-sync",
        ]

    def _capture(
        self,
        argv: list[str],
        *,
        timeout_seconds: float,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=sanitized_child_env(),
                close_fds=True,
                timeout=max(0.1, float(timeout_seconds)),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise JoernQueryTransportError(
                f"Joern query server command failed: {type(exc).__name__}: {exc}"
            ) from exc

    def _inspect(self, container_name: str) -> dict[str, Any] | None:
        result = self._capture(
            [self._docker_binary(), "inspect", container_name],
            timeout_seconds=10,
        )
        if result.returncode != 0:
            return None
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise JoernQueryTransportError(f"Unparseable docker inspect JSON: {exc}") from exc
        if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
            raise JoernQueryTransportError("Unexpected docker inspect response shape.")
        return parsed[0]

    def _container_matches(
        self,
        inspected: dict[str, Any],
        *,
        graph_id: str,
        cpg_path: Path,
    ) -> bool:
        config = inspected.get("Config") if isinstance(inspected.get("Config"), dict) else {}
        host = inspected.get("HostConfig") if isinstance(inspected.get("HostConfig"), dict) else {}
        state = inspected.get("State") if isinstance(inspected.get("State"), dict) else {}
        labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
        mounts = inspected.get("Mounts") if isinstance(inspected.get("Mounts"), list) else []
        cpg = str(Path(cpg_path).expanduser().resolve())
        try:
            cpg_fingerprint = self._cpg_fingerprint(cpg_path)
        except JoernQueryServerError:
            return False
        mount_ok = any(
            isinstance(item, dict)
            and item.get("Destination") == "/cpg.bin"
            and str(item.get("Source") or "") == cpg
            and item.get("RW") is False
            for item in mounts
        )
        security_opt = host.get("SecurityOpt") or []
        cap_drop = host.get("CapDrop") or []
        return bool(
            state.get("Running") is True
            and config.get("Image") == self.config.backend.image
            and config.get("WorkingDir") == "/tmp"
            and labels.get(ROLE_LABEL) == ROLE_VALUE
            and labels.get(GRAPH_LABEL) == graph_id
            and labels.get(ANALYZER_LABEL) == self.config.backend.analyzer_id
            and labels.get(RUNTIME_SPEC_LABEL) == self._runtime_spec_id()
            and labels.get(CPG_FINGERPRINT_LABEL) == cpg_fingerprint
            and host.get("NetworkMode") == "none"
            and host.get("ReadonlyRootfs") is True
            and "ALL" in cap_drop
            and any(str(item).startswith("no-new-privileges") for item in security_opt)
            and not host.get("PortBindings")
            and mount_ok
        )

    def _remove_container(self, container_name: str) -> None:
        result = self._capture(
            [self._docker_binary(), "rm", "-f", container_name],
            timeout_seconds=10,
        )
        if result.returncode == 0:
            return
        detail = (result.stderr or result.stdout or "").strip()
        if "No such container" in detail:
            return
        raise JoernQueryTransportError(
            "Failed to remove Joern query server"
            + (f": {detail[-2048:]}" if detail else f" with exit code {result.returncode}")
        )

    def _owned_containers(self) -> list[tuple[str, str]]:
        result = self._capture(
            [
                self._docker_binary(),
                "ps",
                "-a",
                "--filter",
                f"label={ROLE_LABEL}={ROLE_VALUE}",
                "--format",
                "{{.Names}}",
            ],
            timeout_seconds=10,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise JoernQueryTransportError(f"Failed to list owned Joern servers: {detail}")
        entries: list[tuple[str, str]] = []
        for name in sorted({line.strip() for line in result.stdout.splitlines() if line.strip()}):
            inspected = self._inspect(name)
            if inspected is None:
                continue
            created = str(inspected.get("Created") or "")
            entries.append((created, name))
        return sorted(entries)

    def _enforce_capacity(self, *, target_name: str) -> None:
        owned = self._owned_containers()
        others = [(created, name) for created, name in owned if name != target_name]
        total_after_start = 1 + len(others)
        excess = max(0, total_after_start - self.config.max_containers)
        for _created, name in others[:excess]:
            self._remove_container(name)

    def _decode_rest_response(self, result: subprocess.CompletedProcess[str]) -> str:
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise JoernQueryTransportError(
                "Joern REST transport failed"
                + (f": {detail[-2048:]}" if detail else f" with exit code {result.returncode}")
            )
        if len(result.stdout.encode("utf-8")) > MAX_REST_RESPONSE_BYTES:
            raise JoernQueryTransportError("Joern REST response exceeded the bounded response size.")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise JoernQueryTransportError(f"Unparseable Joern REST JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise JoernQueryTransportError("Joern REST response must be a JSON object.")
        if payload.get("success") is not True:
            detail = str(payload.get("stdout") or payload)
            raise JoernQueryExecutionError(f"Joern CPGQL query failed: {detail[-4096:]}")
        stdout = payload.get("stdout")
        if not isinstance(stdout, str):
            raise JoernQueryTransportError("Joern REST success response is missing stdout text.")
        return stdout

    def _post_query(
        self,
        *,
        graph_id: str,
        query: str,
        timeout_seconds: int,
    ) -> str:
        request = json.dumps({"query": query}, ensure_ascii=False, separators=(",", ":"))
        result = self._capture(
            self.exec_query_argv(graph_id=graph_id, max_time_seconds=timeout_seconds),
            timeout_seconds=float(timeout_seconds + 2),
            input_text=request,
        )
        return self._decode_rest_response(result)

    def _probe_ready(self, graph_id: str) -> bool:
        try:
            stdout = self._post_query(
                graph_id=graph_id,
                query="1",
                timeout_seconds=min(2, self.config.backend.query_timeout_seconds),
            )
        except JoernQueryServerError:
            return False
        return bool(stdout)

    def _ensure_server(self, *, graph_id: str, cpg_path: Path) -> bool:
        graph_id = self._validate_graph_id(graph_id)
        name = self.container_name(graph_id)
        inspected = self._inspect(name)
        if inspected is not None and self._container_matches(
            inspected,
            graph_id=graph_id,
            cpg_path=cpg_path,
        ):
            self._enforce_capacity(target_name=name)
            return False
        if inspected is not None:
            self._remove_container(name)

        self._enforce_capacity(target_name=name)
        result = self._capture(
            self.start_argv(graph_id=graph_id, cpg_path=cpg_path),
            timeout_seconds=15,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise JoernQueryTransportError(
                "Failed to start persistent Joern query server"
                + (f": {detail[-2048:]}" if detail else "")
            )

        deadline = time.monotonic() + self.config.start_timeout_seconds
        while time.monotonic() < deadline:
            inspected = self._inspect(name)
            if inspected is None:
                break
            if self._container_matches(inspected, graph_id=graph_id, cpg_path=cpg_path):
                if self._probe_ready(graph_id):
                    return True
            time.sleep(0.25)

        self._remove_container(name)
        raise JoernQueryTransportError(
            f"Persistent Joern query server did not become ready within "
            f"{self.config.start_timeout_seconds}s."
        )

    def query(
        self,
        *,
        graph_id: str,
        cpg_path: Path,
        query: str,
    ) -> QueryServerResult:
        graph_id = self._validate_graph_id(graph_id)
        if not self.config.backend.enabled:
            raise JoernQueryServerError("Code Graph is disabled by configuration.")
        if not query.strip():
            raise ValueError("query must be non-empty.")
        started = time.monotonic()
        with self._lifecycle_lock():
            cold_start = self._ensure_server(graph_id=graph_id, cpg_path=cpg_path)
            try:
                stdout = self._post_query(
                    graph_id=graph_id,
                    query=query,
                    timeout_seconds=self.config.backend.query_timeout_seconds,
                )
            except JoernQueryTransportError:
                self._remove_container(self.container_name(graph_id))
                self._ensure_server(graph_id=graph_id, cpg_path=cpg_path)
                cold_start = True
                stdout = self._post_query(
                    graph_id=graph_id,
                    query=query,
                    timeout_seconds=self.config.backend.query_timeout_seconds,
                )
        return QueryServerResult(
            stdout=stdout,
            duration_seconds=time.monotonic() - started,
            cold_start=cold_start,
        )

    def stop_all_owned(self) -> int:
        with self._lifecycle_lock():
            owned = self._owned_containers()
            removed = 0
            for _created, name in owned:
                self._remove_container(name)
                removed += 1
            return removed

    def stop_graphs(self, graph_ids: Sequence[str]) -> int:
        requested = {self._validate_graph_id(graph_id) for graph_id in graph_ids}
        if not requested:
            return 0
        with self._lifecycle_lock():
            removed = 0
            for _created, name in self._owned_containers():
                inspected = self._inspect(name)
                if inspected is None:
                    continue
                config = (
                    inspected.get("Config")
                    if isinstance(inspected.get("Config"), dict)
                    else {}
                )
                labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
                if labels.get(GRAPH_LABEL) not in requested:
                    continue
                self._remove_container(name)
                removed += 1
            return removed


def cleanup_owned_query_servers(
    docker_binary: str,
    state_dir: Path | str,
    *,
    graph_ids: Sequence[str] | None = None,
    strict: bool = False,
) -> int:
    backend = JoernBackendConfig(
        enabled=True,
        docker_binary=docker_binary,
        image="ghcr.io/joernio/joern@sha256:" + "0" * 64,
        version="cleanup",
        memory_mb=1,
        cpus=1,
        pids_limit=1,
        tmpfs_mb=1,
        build_timeout_seconds=1,
        query_timeout_seconds=1,
    )
    runtime = JoernQueryServerRuntime(
        JoernQueryServerConfig(
            backend=backend,
            lifecycle_lock_path=(
                Path(state_dir).expanduser().resolve()
                / "code-graph"
                / ".query-runtime.lock"
            ),
        )
    )
    try:
        if graph_ids is None:
            return runtime.stop_all_owned()
        return runtime.stop_graphs(graph_ids)
    except JoernQueryServerError:
        if strict:
            raise
        return 0
