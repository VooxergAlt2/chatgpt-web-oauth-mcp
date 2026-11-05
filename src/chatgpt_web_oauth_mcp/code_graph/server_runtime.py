from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Iterator, Sequence

from ..process_env import sanitized_child_env
from ..state_io import interprocess_file_lock
from .backend import CodeGraphBackendError, JoernBackendConfig
from .cache import DEFAULT_PAYLOAD_FILENAME, graph_cache_root, sha256_file


ROLE_LABEL = "io.opss.code_graph.role"
ROLE_VALUE = "joern-query-server"
OWNER_LABEL = "io.opss.code_graph.owner"
GRAPH_LABEL = "io.opss.code_graph.graph_id"
ANALYZER_LABEL = "io.opss.code_graph.analyzer_id"
RUNTIME_SPEC_LABEL = "io.opss.code_graph.runtime_spec"
CPG_FINGERPRINT_LABEL = "io.opss.code_graph.cpg_fingerprint"
RUNTIME_POLICY_VERSION = 1
SERVER_PORT = 8080
MAX_REST_RESPONSE_BYTES = 1024 * 1024
_DOCKER_DIGEST = re.compile(r"[0-9a-f]{64}")


class JoernQueryServerError(CodeGraphBackendError):
    """Base error for the persistent Joern query runtime."""


class JoernQueryTransportError(JoernQueryServerError):
    """Raised when the isolated REST runtime cannot be reached or decoded."""


class JoernQueryExecutionError(JoernQueryServerError):
    """Raised when Joern executes a query but reports a CPGQL failure."""


class JoernQueryRuntimeNotReady(JoernQueryTransportError):
    """Raised when a semantic query requires a prewarmed Joern runtime."""


@dataclass(frozen=True)
class JoernQueryServerConfig:
    backend: JoernBackendConfig
    state_dir: Path
    start_timeout_seconds: int = 120
    max_containers: int = 1

    def __post_init__(self) -> None:
        if self.start_timeout_seconds <= 0:
            raise ValueError("start_timeout_seconds must be positive.")
        if self.max_containers <= 0:
            raise ValueError("max_containers must be positive.")
        object.__setattr__(self, "state_dir", Path(self.state_dir).expanduser().resolve())

    @property
    def lifecycle_lock_path(self) -> Path:
        return graph_cache_root(self.state_dir) / ".query-runtime.lock"


@dataclass(frozen=True)
class QueryServerResult:
    stdout: str
    duration_seconds: float
    cold_start: bool


@dataclass(frozen=True)
class QueryServerWarmResult:
    duration_seconds: float
    cold_start: bool


class JoernQueryServerRuntime:
    _lifecycle_thread_lock = threading.RLock()

    def __init__(self, config: JoernQueryServerConfig) -> None:
        self.config = config
        self.cache_root = graph_cache_root(config.state_dir)
        self.owner_id = hashlib.sha256(os.fsencode(self.cache_root)).hexdigest()

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
        return f"opss-codegraph-server-{self.owner_id[:24]}-{graph_id[:24]}"

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
    def _validate_cpg_sha256(cpg_sha256: str) -> str:
        value = str(cpg_sha256).strip().lower()
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("cpg_sha256 must be a lowercase 64-character SHA-256 digest.")
        return value

    @staticmethod
    def _validate_cpg_path(cpg_path: Path) -> Path:
        cpg = Path(cpg_path).expanduser().resolve()
        if cpg.is_symlink() or not cpg.is_file():
            raise JoernQueryTransportError(f"CPG must be a real file: {cpg}")
        stat = cpg.stat()
        if stat.st_size <= 0:
            raise JoernQueryTransportError("CPG payload must not be empty.")
        return cpg

    def start_argv(
        self,
        *,
        graph_id: str,
        cpg_path: Path,
        cpg_sha256: str,
    ) -> list[str]:
        graph_id = self._validate_graph_id(graph_id)
        cpg = self._validate_cpg_path(cpg_path)
        cpg_sha256 = self._validate_cpg_sha256(cpg_sha256)
        backend = self.config.backend
        if not backend.enabled:
            raise JoernQueryServerError("Code Graph is disabled by configuration.")
        actual_sha256 = sha256_file(cpg)
        if actual_sha256 != cpg_sha256:
            raise JoernQueryTransportError(
                f"CPG SHA-256 changed before server start: expected {cpg_sha256}, "
                f"found {actual_sha256}"
            )
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
            f"{OWNER_LABEL}={self.owner_id}",
            "--label",
            f"{GRAPH_LABEL}={graph_id}",
            "--label",
            f"{ANALYZER_LABEL}={backend.analyzer_id}",
            "--label",
            f"{RUNTIME_SPEC_LABEL}={self._runtime_spec_id()}",
            "--label",
            f"{CPG_FINGERPRINT_LABEL}={cpg_sha256}",
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

    def exec_query_argv(self, *, container_id: str, max_time_seconds: int | None = None) -> list[str]:
        if not _DOCKER_DIGEST.fullmatch(container_id):
            raise ValueError("Docker exec requires a full container ID.")
        timeout = max_time_seconds or self.config.backend.query_timeout_seconds
        return [
            self._docker_binary(),
            "exec",
            "-i",
            container_id,
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
        cpg_sha256: str,
    ) -> bool:
        config = inspected.get("Config") if isinstance(inspected.get("Config"), dict) else {}
        host = inspected.get("HostConfig") if isinstance(inspected.get("HostConfig"), dict) else {}
        state = inspected.get("State") if isinstance(inspected.get("State"), dict) else {}
        labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
        mounts = inspected.get("Mounts") if isinstance(inspected.get("Mounts"), list) else []
        try:
            cpg = str(self._validate_cpg_path(cpg_path))
            cpg_sha256 = self._validate_cpg_sha256(cpg_sha256)
        except (JoernQueryServerError, ValueError):
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
        backend = self.config.backend
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        expected_cmd = [
            "joern",
            "--server",
            "--server-host",
            "127.0.0.1",
            "--server-port",
            str(SERVER_PORT),
            "/cpg.bin",
        ]
        expected_tmpfs = {"/tmp": f"rw,exec,size={backend.tmpfs_mb}m"}
        env = config.get("Env") if isinstance(config.get("Env"), list) else []
        return bool(
            state.get("Running") is True
            and self._owns_container(inspected)
            and config.get("Image") == backend.image
            and config.get("WorkingDir") == "/tmp"
            and config.get("User") == f"{uid}:{gid}"
            and config.get("Cmd") == expected_cmd
            and config.get("Entrypoint") in (None, [])
            and "HOME=/tmp/joern-home" in env
            and labels.get(ROLE_LABEL) == ROLE_VALUE
            and labels.get(GRAPH_LABEL) == graph_id
            and labels.get(ANALYZER_LABEL) == backend.analyzer_id
            and labels.get(RUNTIME_SPEC_LABEL) == self._runtime_spec_id()
            and labels.get(CPG_FINGERPRINT_LABEL) == cpg_sha256
            and host.get("NetworkMode") == "none"
            and host.get("ReadonlyRootfs") is True
            and host.get("Memory") == backend.memory_mb * 1024 * 1024
            and host.get("NanoCpus") == backend.cpus * 1_000_000_000
            and host.get("PidsLimit") == backend.pids_limit
            and host.get("Tmpfs") == expected_tmpfs
            and host.get("Binds") in (None, [])
            and "ALL" in cap_drop
            and any(str(item).startswith("no-new-privileges") for item in security_opt)
            and not host.get("PortBindings")
            and len(mounts) == 1
            and mount_ok
        )

    @staticmethod
    def _cpg_mount(inspected: dict[str, Any]) -> Path | None:
        mounts = inspected.get("Mounts")
        if not isinstance(mounts, list) or len(mounts) != 1:
            return None
        mount = mounts[0]
        if not isinstance(mount, dict) or (
            mount.get("Type") != "bind"
            or mount.get("Destination") != "/cpg.bin"
            or mount.get("RW") is not False
        ):
            return None
        raw_path = mount.get("Source")
        return Path(raw_path) if isinstance(raw_path, str) and os.path.isabs(raw_path) else None

    def _owns_container(self, inspected: dict[str, Any]) -> bool:
        config = inspected.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict) or labels.get(ROLE_LABEL) != ROLE_VALUE:
            return False
        graph_id = labels.get(GRAPH_LABEL)
        if not isinstance(graph_id, str) or not _DOCKER_DIGEST.fullmatch(graph_id):
            return False
        container_id = inspected.get("Id")
        if not isinstance(container_id, str) or not _DOCKER_DIGEST.fullmatch(container_id):
            return False
        name = inspected.get("Name")
        if OWNER_LABEL in labels:
            return labels[OWNER_LABEL] == self.owner_id and name == "/" + self.container_name(graph_id)

        # Old servers have a daemon-wide graph-only name. Only adopt a former
        # runtime whose read-only bind proves the canonical local cache layout.
        if name != "/opss-codegraph-server-" + graph_id[:24]:
            return False
        source = self._cpg_mount(inspected)
        repositories = self.cache_root / "repositories"
        if source is None or not source.is_relative_to(repositories):
            return False
        parts = source.relative_to(repositories).parts
        if (
            len(parts) != 4
            or not _DOCKER_DIGEST.fullmatch(parts[0])
            or parts[1:] != ("graphs", graph_id, DEFAULT_PAYLOAD_FILENAME)
        ):
            return False
        try:
            return source == source.resolve()
        except (OSError, RuntimeError):
            # An inaccessible or looping path cannot establish legacy ownership.
            return False

    def _remove_container(self, inspected: dict[str, Any]) -> None:
        if not self._owns_container(inspected):
            raise JoernQueryTransportError("Refusing to remove a Joern server without local ownership.")
        # Docker may reuse a name between inspect and rm. IDs never transfer.
        result = self._capture(
            [self._docker_binary(), "rm", "-f", inspected["Id"]],
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

    def _owned_containers(self) -> list[dict[str, Any]]:
        result = self._capture(
            [
                self._docker_binary(),
                "ps",
                "-a",
                "--no-trunc",
                "--filter",
                f"label={ROLE_LABEL}={ROLE_VALUE}",
                "--format",
                "{{.ID}}",
            ],
            timeout_seconds=10,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise JoernQueryTransportError(f"Failed to list owned Joern servers: {detail}")
        entries = []
        for container_id in result.stdout.splitlines():
            if not _DOCKER_DIGEST.fullmatch(container_id):
                continue
            inspected = self._inspect(container_id)
            if inspected and inspected.get("Id") == container_id and self._owns_container(inspected):
                entries.append(inspected)
        return sorted(entries, key=lambda item: (str(item.get("Created") or ""), item["Id"]))

    def _enforce_capacity(self, *, target_name: str) -> None:
        owned = self._owned_containers()
        others = [item for item in owned if item["Name"] != "/" + target_name]
        total_after_start = 1 + len(others)
        excess = max(0, total_after_start - self.config.max_containers)
        for item in others[:excess]:
            self._remove_container(item)

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
        container_id: str,
        query: str,
        timeout_seconds: int,
    ) -> str:
        request = json.dumps({"query": query}, ensure_ascii=False, separators=(",", ":"))
        result = self._capture(
            self.exec_query_argv(container_id=container_id, max_time_seconds=timeout_seconds),
            timeout_seconds=float(timeout_seconds + 2),
            input_text=request,
        )
        return self._decode_rest_response(result)

    def _probe_ready(self, container_id: str) -> bool:
        try:
            stdout = self._post_query(
                container_id=container_id,
                query="1",
                timeout_seconds=min(2, self.config.backend.query_timeout_seconds),
            )
        except JoernQueryServerError:
            return False
        return bool(stdout)

    def _matching_server_ready(
        self,
        *,
        graph_id: str,
        cpg_path: Path,
        cpg_sha256: str,
    ) -> dict[str, Any] | None:
        inspected = self._inspect(self.container_name(graph_id))
        if inspected is None:
            return None
        if not self._container_matches(
            inspected,
            graph_id=graph_id,
            cpg_path=cpg_path,
            cpg_sha256=cpg_sha256,
        ):
            return None
        return inspected if self._probe_ready(inspected["Id"]) else None

    def is_ready(
        self,
        *,
        graph_id: str,
        cpg_path: Path,
        cpg_sha256: str,
    ) -> bool:
        graph_id = self._validate_graph_id(graph_id)
        cpg_sha256 = self._validate_cpg_sha256(cpg_sha256)
        if not self.config.backend.enabled:
            return False
        with self._lifecycle_lock():
            return self._matching_server_ready(
                graph_id=graph_id,
                cpg_path=cpg_path,
                cpg_sha256=cpg_sha256,
            ) is not None

    def _ensure_server(
        self,
        *,
        graph_id: str,
        cpg_path: Path,
        cpg_sha256: str,
    ) -> tuple[dict[str, Any], bool]:
        graph_id = self._validate_graph_id(graph_id)
        name = self.container_name(graph_id)
        inspected = self._inspect(name)
        if inspected is not None and not self._container_matches(
            inspected, graph_id=graph_id, cpg_path=cpg_path, cpg_sha256=cpg_sha256,
        ):
            self._remove_container(inspected)
            inspected = None

        cold_start = inspected is None
        if inspected is not None:
            container_id = inspected["Id"]
        else:
            self._enforce_capacity(target_name=name)
            result = self._capture(
                self.start_argv(
                    graph_id=graph_id,
                    cpg_path=cpg_path,
                    cpg_sha256=cpg_sha256,
                ),
                timeout_seconds=15,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise JoernQueryTransportError(
                    "Failed to start persistent Joern query server"
                    + (f": {detail[-2048:]}" if detail else "")
                )
            container_id = result.stdout.strip()
            if not _DOCKER_DIGEST.fullmatch(container_id):
                raise JoernQueryTransportError("Docker run did not return a full container ID.")

        deadline = time.monotonic() + self.config.start_timeout_seconds
        while time.monotonic() < deadline:
            inspected = self._inspect(container_id)
            if inspected is None:
                break
            if inspected.get("Id") != container_id:
                raise JoernQueryTransportError("Docker inspect returned a different container ID.")
            if self._container_matches(
                inspected,
                graph_id=graph_id,
                cpg_path=cpg_path,
                cpg_sha256=cpg_sha256,
            ):
                if self._probe_ready(container_id):
                    self._enforce_capacity(target_name=name)
                    return inspected, cold_start
            time.sleep(0.25)

        if inspected is not None:
            self._remove_container(inspected)
        raise JoernQueryTransportError(
            f"Persistent Joern query server did not become ready within "
            f"{self.config.start_timeout_seconds}s."
        )

    def warm(
        self,
        *,
        graph_id: str,
        cpg_path: Path,
        cpg_sha256: str,
    ) -> QueryServerWarmResult:
        graph_id = self._validate_graph_id(graph_id)
        cpg_sha256 = self._validate_cpg_sha256(cpg_sha256)
        if not self.config.backend.enabled:
            raise JoernQueryServerError("Code Graph is disabled by configuration.")
        started = time.monotonic()
        with self._lifecycle_lock():
            _, cold_start = self._ensure_server(
                graph_id=graph_id,
                cpg_path=cpg_path,
                cpg_sha256=cpg_sha256,
            )
        return QueryServerWarmResult(
            duration_seconds=time.monotonic() - started,
            cold_start=cold_start,
        )

    def query(
        self,
        *,
        graph_id: str,
        cpg_path: Path,
        cpg_sha256: str,
        query: str,
        allow_cold_start: bool = True,
    ) -> QueryServerResult:
        graph_id = self._validate_graph_id(graph_id)
        cpg_sha256 = self._validate_cpg_sha256(cpg_sha256)
        if not self.config.backend.enabled:
            raise JoernQueryServerError("Code Graph is disabled by configuration.")
        if not query.strip():
            raise ValueError("query must be non-empty.")
        started = time.monotonic()
        with self._lifecycle_lock():
            if allow_cold_start:
                inspected, cold_start = self._ensure_server(
                    graph_id=graph_id,
                    cpg_path=cpg_path,
                    cpg_sha256=cpg_sha256,
                )
            else:
                inspected = self._matching_server_ready(
                    graph_id=graph_id,
                    cpg_path=cpg_path,
                    cpg_sha256=cpg_sha256,
                )
                if inspected is None:
                    raise JoernQueryRuntimeNotReady(
                        "Persistent Joern query runtime is not ready; run code_graph_prepare."
                    )
                cold_start = False
            try:
                stdout = self._post_query(
                    container_id=inspected["Id"],
                    query=query,
                    timeout_seconds=self.config.backend.query_timeout_seconds,
                )
            except JoernQueryTransportError:
                self._remove_container(inspected)
                if not allow_cold_start:
                    raise JoernQueryRuntimeNotReady(
                        "Persistent Joern query runtime became unavailable; run code_graph_prepare."
                    )
                inspected, _ = self._ensure_server(
                    graph_id=graph_id,
                    cpg_path=cpg_path,
                    cpg_sha256=cpg_sha256,
                )
                cold_start = True
                stdout = self._post_query(
                    container_id=inspected["Id"],
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
            for inspected in owned:
                self._remove_container(inspected)
            return len(owned)

    def stop_graphs(self, graph_ids: Sequence[str]) -> int:
        requested = {self._validate_graph_id(graph_id) for graph_id in graph_ids}
        if not requested:
            return 0
        with self._lifecycle_lock():
            removed = 0
            for inspected in self._owned_containers():
                if inspected["Config"]["Labels"][GRAPH_LABEL] not in requested:
                    continue
                self._remove_container(inspected)
                removed += 1
            return removed

    def stop_orphaned(self) -> int:
        with self._lifecycle_lock():
            removed = 0
            for inspected in self._owned_containers():
                source = self._cpg_mount(inspected)
                if source is not None and source.is_file() and not source.is_symlink():
                    continue
                self._remove_container(inspected)
                removed += 1
            return removed


def cleanup_owned_query_servers(
    docker_binary: str,
    state_dir: Path | str,
    *,
    graph_ids: Sequence[str] | None = None,
    orphaned_only: bool = False,
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
            state_dir=Path(state_dir),
        )
    )
    try:
        if orphaned_only:
            return runtime.stop_orphaned()
        if graph_ids is None:
            return runtime.stop_all_owned()
        return runtime.stop_graphs(graph_ids)
    except JoernQueryServerError:
        if strict:
            raise
        return 0
