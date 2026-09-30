from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Sequence

from ..process_env import sanitized_child_env


MAX_DIAGNOSTIC_BYTES = 32768


class CodeGraphBackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class JoernBackendConfig:
    enabled: bool
    docker_binary: str
    image: str
    version: str
    memory_mb: int
    cpus: int
    pids_limit: int
    tmpfs_mb: int
    build_timeout_seconds: int
    query_timeout_seconds: int = 20

    @property
    def image_digest(self) -> str:
        marker = "@sha256:"
        if marker not in self.image:
            raise CodeGraphBackendError("Joern image must be pinned by sha256 digest.")
        digest = self.image.rsplit(marker, 1)[-1].lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise CodeGraphBackendError("Joern image contains an invalid sha256 digest.")
        return f"sha256:{digest}"

    @property
    def analyzer_id(self) -> str:
        return f"joern:{self.version}@{self.image_digest}"


@dataclass(frozen=True)
class BackendStatus:
    enabled: bool
    available: bool
    analyzer_id: str
    image: str
    version: str
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class BuildResult:
    payload_path: Path
    duration_seconds: float
    stdout_tail: str
    stderr_tail: str


class JoernDockerBackend:
    def __init__(self, config: JoernBackendConfig) -> None:
        self.config = config

    def _docker_binary(self) -> str | None:
        value = self.config.docker_binary
        if os.path.sep in value:
            candidate = Path(value).expanduser()
            return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
        return shutil.which(value)

    def _capture(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float = 15.0,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(argv),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=sanitized_child_env(),
                close_fds=True,
                timeout=max(0.1, float(timeout_seconds)),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodeGraphBackendError(
                f"Backend command failed: {type(exc).__name__}: {exc}"
            ) from exc

    def status(self) -> BackendStatus:
        analyzer_id = self.config.analyzer_id
        if not self.config.enabled:
            return BackendStatus(
                enabled=False,
                available=False,
                analyzer_id=analyzer_id,
                image=self.config.image,
                version=self.config.version,
                error_code="code_graph_disabled",
                error_message="Code Graph is disabled by configuration.",
            )
        docker = self._docker_binary()
        if not docker:
            return BackendStatus(
                enabled=True,
                available=False,
                analyzer_id=analyzer_id,
                image=self.config.image,
                version=self.config.version,
                error_code="docker_unavailable",
                error_message="Configured Docker executable is unavailable.",
            )
        try:
            result = self._capture(
                [docker, "image", "inspect", self.config.image, "--format", "{{.Id}}"],
                timeout_seconds=15,
            )
        except CodeGraphBackendError as exc:
            return BackendStatus(
                enabled=True,
                available=False,
                analyzer_id=analyzer_id,
                image=self.config.image,
                version=self.config.version,
                error_code="docker_probe_failed",
                error_message=str(exc),
            )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            return BackendStatus(
                enabled=True,
                available=False,
                analyzer_id=analyzer_id,
                image=self.config.image,
                version=self.config.version,
                error_code="joern_image_unavailable",
                error_message=(
                    "Pinned Joern image is not available locally; runtime image pulls are disabled."
                    + (f" Detail: {detail[-1024:]}" if detail else "")
                ),
            )
        return BackendStatus(
            enabled=True,
            available=True,
            analyzer_id=analyzer_id,
            image=self.config.image,
            version=self.config.version,
        )

    def build_argv(
        self,
        *,
        source_dir: Path,
        output_dir: Path,
        container_name: str,
    ) -> list[str]:
        docker = self._docker_binary()
        if not docker:
            raise CodeGraphBackendError("Configured Docker executable is unavailable.")
        source = Path(source_dir).expanduser().resolve()
        output = Path(output_dir).expanduser().resolve()
        if not source.is_dir():
            raise CodeGraphBackendError(f"Source snapshot is not a directory: {source}")
        if output.is_symlink() or not output.is_dir():
            raise CodeGraphBackendError(f"Output directory must be a real directory: {output}")
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        return [
            docker,
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            f"{self.config.memory_mb}m",
            "--cpus",
            str(self.config.cpus),
            "--pids-limit",
            str(self.config.pids_limit),
            "--user",
            f"{uid}:{gid}",
            "--env",
            "HOME=/tmp/joern-home",
            "--tmpfs",
            f"/tmp:rw,exec,size={self.config.tmpfs_mb}m",
            "--mount",
            f"type=bind,src={source},dst=/src,readonly",
            "--mount",
            f"type=bind,src={output},dst=/out",
            self.config.image,
            "joern-parse",
            "/src",
            "--language",
            "PYTHONSRC",
            "--output",
            "/out/cpg.bin",
        ]

    @staticmethod
    def _tail_binary(handle, max_bytes: int = MAX_DIAGNOSTIC_BYTES) -> str:
        handle.flush()
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        return handle.read().decode("utf-8", errors="replace")

    def _force_remove_container(self, container_name: str) -> None:
        docker = self._docker_binary()
        if not docker:
            return
        try:
            subprocess.run(
                [docker, "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=sanitized_child_env(),
                close_fds=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def build(self, *, source_dir: Path, output_dir: Path) -> BuildResult:
        status = self.status()
        if not status.available:
            raise CodeGraphBackendError(status.error_message or "Joern backend is unavailable.")

        container_name = f"opss-codegraph-{os.getpid()}-{secrets.token_hex(5)}"
        argv = self.build_argv(
            source_dir=source_dir,
            output_dir=output_dir,
            container_name=container_name,
        )
        payload_path = Path(output_dir) / "cpg.bin"
        if payload_path.exists():
            if payload_path.is_symlink() or not payload_path.is_file():
                raise CodeGraphBackendError("Existing CPG output path is unsafe.")
            payload_path.unlink()

        started = time.monotonic()
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            popen_kwargs: dict[str, object] = {
                "stdout": stdout_file,
                "stderr": stderr_file,
                "stdin": subprocess.DEVNULL,
                "env": sanitized_child_env(),
                "close_fds": True,
            }
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            try:
                process = subprocess.Popen(argv, **popen_kwargs)
            except OSError as exc:
                raise CodeGraphBackendError(
                    f"Failed to start Joern container: {type(exc).__name__}: {exc}"
                ) from exc
            try:
                return_code = process.wait(timeout=self.config.build_timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                self._force_remove_container(container_name)
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except (OSError, ProcessLookupError):
                        pass
                else:
                    try:
                        process.terminate()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                raise CodeGraphBackendError(
                    f"Joern build timed out after {self.config.build_timeout_seconds}s."
                ) from exc

            stdout_tail = self._tail_binary(stdout_file)
            stderr_tail = self._tail_binary(stderr_file)

        duration = time.monotonic() - started
        if return_code != 0:
            detail = stderr_tail or stdout_tail or f"exit code {return_code}"
            raise CodeGraphBackendError(f"Joern build failed: {detail}")
        if payload_path.is_symlink() or not payload_path.is_file():
            raise CodeGraphBackendError("Joern reported success but did not create cpg.bin.")
        if payload_path.stat().st_size <= 0:
            raise CodeGraphBackendError("Joern created an empty cpg.bin.")
        return BuildResult(
            payload_path=payload_path,
            duration_seconds=duration,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
