from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from ..process_env import sanitized_child_env
from ..state_io import ensure_private_directory


GIT_TIMEOUT_SECONDS = 30.0
MAX_GIT_DIAGNOSTIC_CHARS = 8192


class GitSnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitSnapshot:
    repository_id: str
    repository_root: Path
    git_common_dir: Path
    requested_ref: str
    tree_sha: str


def _diagnostic(value: str) -> str:
    return value[-MAX_GIT_DIAGNOSTIC_CHARS:]


def _run_git(cwd: Path, args: list[str], *, timeout: float = GIT_TIMEOUT_SECONDS) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=sanitized_child_env(),
            close_fds=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitSnapshotError(f"git execution failed: {type(exc).__name__}: {exc}") from exc
    if completed.returncode != 0:
        message = _diagnostic((completed.stderr or completed.stdout or "git command failed").strip())
        raise GitSnapshotError(message)
    return completed.stdout.strip()


def _normalized_common_dir(cwd: Path) -> tuple[Path, Path]:
    root_text = _run_git(cwd, ["rev-parse", "--show-toplevel"])
    try:
        common_text = _run_git(
            cwd,
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        )
    except GitSnapshotError:
        common_text = _run_git(cwd, ["rev-parse", "--git-common-dir"])
    root = Path(root_text).expanduser().resolve()
    common = Path(common_text).expanduser()
    if not common.is_absolute():
        common = (cwd / common).resolve()
    else:
        common = common.resolve()
    return root, common


def resolve_git_snapshot(cwd: Path, ref: str = "HEAD") -> GitSnapshot:
    resolved_cwd = Path(cwd).expanduser().resolve()
    normalized_ref = str(ref).strip()
    if not normalized_ref or normalized_ref.startswith("-") or "\x00" in normalized_ref:
        raise GitSnapshotError("ref must be a non-empty Git revision and must not start with '-'.")
    root, common = _normalized_common_dir(resolved_cwd)
    tree_sha = _run_git(root, ["rev-parse", "--verify", f"{normalized_ref}^{{tree}}"])
    if len(tree_sha) != 40 or any(ch not in "0123456789abcdefABCDEF" for ch in tree_sha):
        raise GitSnapshotError("Git returned an invalid tree object id.")
    repository_id = hashlib.sha256(str(common).encode("utf-8")).hexdigest()
    return GitSnapshot(
        repository_id=repository_id,
        repository_root=root,
        git_common_dir=common,
        requested_ref=normalized_ref,
        tree_sha=tree_sha.lower(),
    )


def export_git_snapshot(
    snapshot: GitSnapshot,
    *,
    scratch_root: Path,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> Path:
    """Export exactly snapshot.tree_sha into a private temporary source directory."""
    ensure_private_directory(scratch_root)
    temp_root = Path(tempfile.mkdtemp(prefix="snapshot-", dir=str(scratch_root)))
    temp_root.chmod(0o700)
    archive_path = temp_root / "source.tar"
    source_dir = temp_root / "source"
    source_dir.mkdir(mode=0o700)
    try:
        with archive_path.open("wb") as archive_handle:
            try:
                completed = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(snapshot.repository_root),
                        "archive",
                        "--format=tar",
                        snapshot.tree_sha,
                    ],
                    stdout=archive_handle,
                    stderr=subprocess.PIPE,
                    env=sanitized_child_env(),
                    close_fds=True,
                    timeout=timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise GitSnapshotError(
                    f"git archive failed: {type(exc).__name__}: {exc}"
                ) from exc
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace") if completed.stderr else ""
            raise GitSnapshotError(_diagnostic(stderr.strip() or "git archive failed"))
        if archive_path.stat().st_size == 0:
            raise GitSnapshotError("git archive produced an empty archive.")

        with tarfile.open(archive_path, mode="r:") as archive:
            archive.extractall(source_dir, filter="data")
        archive_path.unlink(missing_ok=True)
        return source_dir
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def cleanup_exported_snapshot(source_dir: Path) -> None:
    """Remove the whole private temporary snapshot root created by export_git_snapshot."""
    source = Path(source_dir)
    parent = source.parent
    if source.name == "source" and parent.name.startswith("snapshot-"):
        shutil.rmtree(parent, ignore_errors=True)
