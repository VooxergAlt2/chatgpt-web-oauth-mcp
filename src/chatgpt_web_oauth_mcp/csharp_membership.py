"""Evaluate MSBuild Compile membership for a tracked file in an approved Git snapshot."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import subprocess
import tempfile
import threading
import time

SDK = "8.0.425"
MAX_FILES = 6000
MAX_DIRECTORIES = 2500
MAX_DEPTH = 32
MAX_SNAPSHOT_BYTES = 48 * 1024 * 1024
MAX_EVALUATION_OUTPUT = 4 * 1024 * 1024
_SLOTS = threading.BoundedSemaphore(2)
_DIMENSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. +\-]{0,79}\Z")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class _Unavailable(RuntimeError):
    def __init__(self, reason: str, diagnostic: str | None = None):
        self.reason = reason
        self.diagnostic = diagnostic


def _relative(value: str) -> Path:
    if (not value or len(value) > 1536 or value.startswith("/") or "\\" in value
            or ":" in value or any(part in ("", ".", "..", ".git") for part in value.split("/"))
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise _Unavailable("invalid_relative_path")
    return Path(value)


def _bounded(argv, *, limit, cwd=None, data=None, env=None):
    # MSBuild uses large anonymous memfds. Limit its pipes while draining both
    # streams rather than restricting all file descriptors with RLIMIT_FSIZE.
    with tempfile.TemporaryFile() as stdin, selectors.DefaultSelector() as selector:
        if data:
            stdin.write(data)
            stdin.seek(0)
        with subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, start_new_session=True) as child:
            streams = {child.stdout: bytearray(), child.stderr: bytearray()}
            try:
                deadline = time.monotonic() + 18
                for stream in streams:
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map():
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        raise _Unavailable("timeout")
                    for key, _ in selector.select(timeout):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output = streams[key.fileobj]
                        if len(output) + len(chunk) > limit:
                            raise _Unavailable("output_limit_exceeded")
                        output.extend(chunk)
                code = child.wait(timeout=max(0.01, deadline - time.monotonic()))
            finally:
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if code:
                match = re.search(rb"\bMSB[0-9]{4}\b", streams[child.stderr]) or re.search(
                    rb"\bMSB[0-9]{4}\b", streams[child.stdout])
                raise _Unavailable("evaluation_failed" if cwd is None else "git_snapshot_unavailable",
                                   match[0].decode() if match else None)
            return streams[child.stdout]


def _git(repo, *args, limit=65536, data=None):
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_NO_REPLACE_OBJECTS": "1",
           "GIT_NO_LAZY_FETCH": "1", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}
    return _bounded(["/usr/bin/prlimit", "--as=536870912", "--cpu=12", "--",
                     "/usr/bin/git", "-c", "protocol.allow=never", *args], cwd=repo,
                    env=env, limit=limit, data=data)


def _export(repo: Path, tree: str, destination: Path) -> None:
    # Raw Git objects preserve export-ignored and export-subst inputs exactly.
    # Disallow symlinks and gitlinks: a partial checkout could misclassify an input.
    listing = _git(repo, "ls-tree", "-rzl", tree, limit=2 * 1024 * 1024)
    entries = []
    directories = set()
    total = 0
    for record in listing.rstrip(b"\0").split(b"\0") if listing else ():
        meta, raw_path = record.split(b"\t", 1)
        mode, kind, oid, raw_size = meta.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise _Unavailable("unsupported_git_entry")
        size = int(raw_size)
        total += size
        if total > MAX_SNAPSHOT_BYTES or len(entries) >= MAX_FILES:
            raise _Unavailable("snapshot_limit_exceeded")
        path = _relative(os.fsdecode(bytes(raw_path)))
        if len(path.parts) > MAX_DEPTH:
            raise _Unavailable("snapshot_limit_exceeded")
        directories.update(path.parents)
        if len(directories) > MAX_DIRECTORIES:
            raise _Unavailable("snapshot_limit_exceeded")
        entries.append((path, oid, size))
    batch = _git(repo, "cat-file", "--batch", data=b"".join(oid + b"\n" for _, oid, _ in entries),
                 limit=MAX_SNAPSHOT_BYTES + MAX_FILES * 100 + 1)
    cursor = 0
    for path, oid, size in entries:
        header = oid + b" blob " + str(size).encode() + b"\n"
        if not batch.startswith(header, cursor):
            raise _Unavailable("invalid_git_object")
        cursor += len(header)
        end = cursor + size
        if batch[end:end + 1] != b"\n":
            raise _Unavailable("invalid_git_object")
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(memoryview(batch)[cursor:end])
        cursor = end + 1


def sandbox_command(root: Path, snapshot: Path, project_dir: Path) -> list[str]:
    """Build the fixed SDK's isolated, resource-limited evaluation command."""
    command = [
        "/usr/bin/systemd-run", "--user", "--scope", "--quiet",
        "-p", "RuntimeMaxSec=15s", "-p", "MemoryMax=768M", "-p", "MemorySwapMax=0",
        "-p", "TasksMax=64", "-p", "CPUQuota=150%",
        # The namespace init is visible through /proc/1/environ. Clear its
        # environment too, after systemd-run has connected to the user bus.
        "/usr/bin/env", "-i",
        "/usr/bin/bwrap", "--unshare-all", "--unshare-user", "--disable-userns",
        "--die-with-parent", "--new-session", "--cap-drop", "ALL",
        "--dir", "/usr", "--ro-bind", "/usr/lib", "/usr/lib", "--symlink", "usr/lib", "/lib",
        "--proc", "/proc", "--dev", "/dev", "--size", "33554432", "--tmpfs", "/tmp",
    ]
    if Path("/usr/lib64").exists():
        command.extend(["--ro-bind", "/usr/lib64", "/usr/lib64", "--symlink", "usr/lib64", "/lib64"])
    for relative in ("dotnet", "host", "shared", "packs", f"sdk/{SDK}"):
        source = root / relative
        if not source.exists():
            raise _Unavailable("sdk_unavailable")
        command.extend(["--ro-bind", str(source), "/sdk/" + relative])
    command.extend(["--ro-bind", str(snapshot), "/case", "--chdir", str(Path("/case") / project_dir),
                    "--remount-ro", "/", "--clearenv"])
    for key, value in {
        "HOME": "/tmp/home", "DOTNET_ROOT": "/sdk", "DOTNET_CLI_HOME": "/tmp/home",
        "DOTNET_NOLOGO": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_PROCESSOR_COUNT": "2",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "MSBuildEnableWorkloadResolver": "false",
        "NUGET_PACKAGES": "/tmp/packages",
    }.items():
        command.extend(["--setenv", key, value])
    return command + ["--", "/sdk/dotnet", f"/sdk/sdk/{SDK}/MSBuild.dll", "-nologo",
                      "-noAutoResponse", "-nodeReuse:false"]


def _evaluate(command, project, configuration, platform, framework=None):
    args = [str(Path("/case") / project), "-getProperty:TargetFramework,TargetFrameworks,TargetFrameworkVersion",
            "-getItem:Compile", "-p:Configuration=" + configuration, "-p:Platform=" + platform]
    if framework:
        args.append("-p:TargetFramework=" + framework)
    try:
        bus_env = {key: os.environ[key] for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
                   if key in os.environ}
        response = json.loads(_bounded(command + args, limit=MAX_EVALUATION_OUTPUT, env=bus_env))
        if not isinstance(response["Properties"], dict) or not isinstance(response["Items"]["Compile"], list):
            raise ValueError
        return response
    except (KeyError, TypeError, ValueError) as exc:
        raise _Unavailable("invalid_evaluation_result") from exc


def csharp_project_membership(*, cwd: Path, ref: str, project: str, configuration: str,
                              platform: str, target_framework: str | None, file: str,
                              dotnet: Path, allowed_roots: tuple[Path, ...]) -> dict:
    result = {"success": False, "status": "unknown", "included": None, "sdk": SDK}
    if not _SLOTS.acquire(blocking=False):
        return dict(result, reason="evaluation_busy")
    try:
        repo = Path(cwd).resolve(strict=True)
        if repo != Path(cwd) or repo not in allowed_roots:
            raise _Unavailable("workspace_not_allowed")
        if (repo / ".git").is_symlink() or not (repo / ".git").is_dir():
            raise _Unavailable("workspace_not_allowed")
        if Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip()) != repo:
            raise _Unavailable("workspace_not_allowed")
        common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if Path(common.decode().strip()).resolve() != repo / ".git":
            raise _Unavailable("workspace_not_allowed")
        proj, source = _relative(project), _relative(file)
        if proj.suffix.lower() != ".csproj" or source.suffix.lower() != ".cs":
            raise _Unavailable("invalid_file_type")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./\-]{0,159}", ref):
            raise _Unavailable("invalid_git_ref")
        for value in (configuration, platform, *(() if target_framework is None else (target_framework,))):
            if not _DIMENSION.fullmatch(value):
                raise _Unavailable("invalid_dimension")
        commit = _git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
        tree = _git(repo, "rev-parse", "--verify", commit + "^{tree}").decode().strip()
        if not _OID.fullmatch(commit) or not _OID.fullmatch(tree):
            raise _Unavailable("invalid_git_object")
        result.update(commit_sha=commit, tree_sha=tree, project=project, file=file,
                      configuration=configuration, platform=platform, target_framework=target_framework)
        if not dotnet or not Path(dotnet).is_absolute() or not Path(dotnet).is_file():
            raise _Unavailable("sdk_unavailable")
        sdk_host = Path(dotnet).resolve(strict=True)
        if sdk_host.name != "dotnet":
            raise _Unavailable("sdk_unavailable")
        with tempfile.TemporaryDirectory(prefix="opss-csharp-membership-") as directory:
            snapshot = Path(directory)
            _export(repo, tree, snapshot)
            if not (snapshot / proj).is_file() or not (snapshot / source).is_file():
                raise _Unavailable("file_missing_from_snapshot")
            command = sandbox_command(sdk_host.parent, snapshot, proj.parent)
            evaluation = _evaluate(command, proj, configuration, platform)
            props = evaluation["Properties"]
            frameworks = (props.get("TargetFrameworks") or props.get("TargetFramework") or "").split(";")
            frameworks = [v.strip() for v in frameworks if v.strip()]
            if len(frameworks) > 32 or any(not _DIMENSION.fullmatch(v) for v in frameworks):
                raise _Unavailable("framework_metadata_limit")
            result["available_frameworks"] = frameworks
            if target_framework and target_framework not in frameworks:
                raise _Unavailable("target_framework_unavailable")
            if props.get("TargetFrameworks"):
                if target_framework is None and len(frameworks) != 1:
                    raise _Unavailable("target_framework_required")
                selected = target_framework or frameworks[0]
                evaluation = _evaluate(command, proj, configuration, platform, selected)
                if evaluation["Properties"].get("TargetFramework") != selected:
                    raise _Unavailable("target_framework_unavailable")
            for prop, field in (("TargetFramework", "target_framework"), ("TargetFrameworkVersion", "target_framework_version")):
                value = evaluation["Properties"].get(prop)
                if value and not _DIMENSION.fullmatch(value):
                    raise _Unavailable("framework_metadata_limit")
                result[field] = value or None
            links = []
            match_count = 0
            links_truncated = False
            for item in evaluation["Items"]["Compile"]:
                full_path = PurePosixPath(item["FullPath"])
                if not full_path.is_relative_to("/case") or ".." in full_path.parts:
                    raise _Unavailable("compile_outside_snapshot")
                if full_path == PurePosixPath("/case") / file:
                    match_count += 1
                    link = item.get("Link")
                    if link and (len(links) >= 8 or len(link) > 256):
                        links_truncated = True
                    elif link:
                        links.append(link)
            result.update(success=True, status="included" if match_count else "excluded", included=bool(match_count),
                          source="MSBuild.Compile", evaluation_host="linux", compile_count=len(evaluation["Items"]["Compile"]),
                          match_count=match_count, logical_links=links, logical_links_truncated=links_truncated)
            return result
    except _Unavailable as exc:
        result["reason"] = exc.reason
        if exc.diagnostic:
            result["diagnostic"] = exc.diagnostic
    except (OSError, UnicodeError, KeyError, ValueError, TypeError, RecursionError, subprocess.TimeoutExpired):
        result["reason"] = "environment_or_evaluation_unavailable"
    finally:
        _SLOTS.release()
    return result
