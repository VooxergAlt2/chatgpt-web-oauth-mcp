"""Immutable profile identity and regular-file snapshot boundaries."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys

import pytest

from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError
from chatgpt_web_oauth_mcp import acp_verification_runtime as runtime_module
from chatgpt_web_oauth_mcp.acp_verification_runtime import VerificationRuntime, validate_runtime_identity


def identity(root="/nonexistent/runtime"):
    return {"id": "pytest-v1", "root": root, "manifest_sha256": "a" * 64, "python": "bin/python"}


def test_identity_validation_is_historical_and_does_not_touch_files(monkeypatch):
    value = identity()
    original = deepcopy(value)
    def unexpected(*args, **kwargs):
        raise AssertionError("Identity validation must not inspect current files")
    monkeypatch.setattr(Path, "resolve", unexpected)
    monkeypatch.setattr(Path, "stat", unexpected)
    result = validate_runtime_identity(value)
    assert result == value and result is not value and value == original


@pytest.mark.parametrize("change", [
    {"id": ""}, {"id": "../runtime"}, {"id": "x" * 65},
    {"root": "/"}, {"root": "relative/runtime"}, {"root": "/tmp/../runtime"},
    {"root": "/tmp/runtime/"}, {"root": "/tmp/runtime\0"}, {"root": "//tmp/runtime"},
    {"root": "/tmp/\ud800"}, {"root": "/" + "x" * 4096},
    {"manifest_sha256": "A" * 64}, {"python": "/usr/bin/python3"}, {"extra": 1},
])
def test_invalid_runtime_identity_rejected(change):
    with pytest.raises(AcpStateError, match="acp_verification_runtime_invalid"):
        validate_runtime_identity({**identity(), **change})


def seal_runtime(root):
    records = {}
    for path in root.rglob("*"):
        if path.is_file() and path.name != "manifest.json":
            mode = 0o555 if path.stat().st_mode & 0o111 else 0o444
            path.chmod(mode)
            data = path.read_bytes()
            records[path.relative_to(root).as_posix()] = {
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": mode,
            }
    manifest = root / "manifest.json"
    if manifest.exists():
        manifest.chmod(0o644)
    raw = json.dumps({"version": 1, "files": records}, sort_keys=True).encode()
    manifest.write_bytes(raw)
    manifest.chmod(0o444)
    return {"id": "pytest-v1", "root": str(root),
            "manifest_sha256": hashlib.sha256(raw).hexdigest(), "python": "bin/python"}


def make_runtime(root, *, real_python=False):
    """Small regular-file venv; real Python supports sandbox/worker integration."""
    root.mkdir(mode=0o700)
    (root / "bin").mkdir()
    (root / "bin/python").write_bytes(
        Path("/usr/bin/python3").read_bytes() if real_python else b"fixture-python")
    (root / "bin/python").chmod(0o755)
    (root / "pyvenv.cfg").write_text("home = /usr/bin\ninclude-system-site-packages = false\n")
    package = root / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    package.mkdir(parents=True)
    (package / "opss_runtime_canary.py").write_text("VALUE = 'snapshot-only'\n")
    return seal_runtime(root)


@pytest.fixture
def profile(tmp_path):
    root = tmp_path / "runtime"
    return VerificationRuntime.load(make_runtime(root))


def test_regular_profile_stage_survives_original_removal_and_is_independent(tmp_path):
    descriptor = make_runtime(tmp_path / "runtime", real_python=True)
    runtime = VerificationRuntime.load(descriptor)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    staged = runtime.stage(private / "runtime")
    assert staged.identity() == descriptor
    assert (staged.root / "manifest.json").is_file()
    assert (staged.root / "bin/python").stat().st_mode & 0o777 == 0o555
    shutil.rmtree(runtime.root)
    staged.validate()
    result = subprocess.run([str(staged.root / "bin/python"), "-I", "-B", "-c",
        "import opss_runtime_canary,sys; print(opss_runtime_canary.VALUE); print(sys.prefix)"],
        capture_output=True, text=True, timeout=5, check=True)
    assert result.stdout.splitlines() == ["snapshot-only", str(staged.root)]


def test_source_tampering_after_load_is_rejected_before_copy(tmp_path, profile):
    config = profile.root / "pyvenv.cfg"
    config.chmod(0o644)
    config.write_text("home = /usr/bin\ninclude-system-site-packages = true\n")
    config.chmod(0o444)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    destination = private / "runtime"
    with pytest.raises(AcpStateError, match="runtime_changed"):
        profile.stage(destination)
    assert not destination.exists()


def test_source_change_after_copy_is_rejected_and_partial_stage_removed(tmp_path, profile, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    original = runtime_module._copy
    touched = []
    def copy_then_change(root, relative, destination, expected):
        original(root, relative, destination, expected)
        if not touched:
            path = root / relative
            path.chmod(0o644)
            path.write_bytes(b"changed after this file was copied")
            path.chmod(0o555 if expected.mode & 0o111 else 0o444)
            touched.append(relative)
    monkeypatch.setattr(runtime_module, "_copy", copy_then_change)
    with pytest.raises(AcpStateError, match="runtime_changed"):
        profile.stage(private / "runtime")
    assert touched and not (private / "runtime").exists()


def test_staged_tampering_is_rejected_without_reading_original(tmp_path, profile):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    staged = profile.stage(private / "runtime")
    shutil.rmtree(profile.root)
    target = staged.root / "bin/python"
    target.chmod(0o755)
    target.write_bytes(b"changed")
    target.chmod(0o555)
    with pytest.raises(AcpStateError, match="runtime_changed"):
        staged.validate()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "socket", "undeclared"])
def test_untrusted_runtime_entries_are_never_copied(tmp_path, profile, kind):
    target = profile.root / "lib/extra"
    outside = tmp_path / "secret"
    outside.write_text("must not enter the runtime")
    sock = None
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, target)
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "socket":
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(target))
    else:
        target.write_text("undeclared")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    try:
        with pytest.raises(AcpStateError, match="acp_verification_runtime_"):
            profile.stage(private / "runtime")
        assert not (private / "runtime").exists()
    finally:
        if sock:
            sock.close()


@pytest.mark.parametrize("forbidden", ["parent", "self", "child"])
def test_profile_cannot_overlap_credentials_state_or_source(tmp_path, profile, forbidden):
    path = {"parent": tmp_path, "self": profile.root, "child": profile.root / "state"}[forbidden]
    with pytest.raises(AcpStateError, match="runtime_overlap"):
        VerificationRuntime.load(profile.identity(), forbidden_roots=[path])


def test_manifest_pin_and_readonly_files_are_enforced(tmp_path, profile):
    manifest = profile.root / "manifest.json"
    manifest.chmod(0o644)
    with pytest.raises(AcpStateError, match="manifest_invalid"):
        profile.validate()
    manifest.chmod(0o444)
    original = profile.identity()
    original["manifest_sha256"] = "f" * 64
    with pytest.raises(AcpStateError, match="runtime_changed"):
        VerificationRuntime.load(original)
    (profile.root / "bin/python").chmod(0o755)
    with pytest.raises(AcpStateError, match="runtime_changed"):
        profile.validate()


@pytest.mark.parametrize("value", ["true", "false\ninclude-system-site-packages = false"])
def test_shared_or_ambiguous_venv_configuration_rejected(tmp_path, value):
    root = tmp_path / "runtime"
    make_runtime(root)
    cfg = root / "pyvenv.cfg"
    cfg.chmod(0o644)
    cfg.write_text(f"home = /usr/bin\ninclude-system-site-packages = {value}\n")
    descriptor = seal_runtime(root)
    with pytest.raises(AcpStateError, match="venv_invalid"):
        VerificationRuntime.load(descriptor)


def test_backend_optional_runtime_configuration_keeps_old_defaults_and_rejects_overlap(tmp_path):
    from test_acp_backend import configuration
    from chatgpt_web_oauth_mcp.acp_backend import AcpBackend
    config, workspace, data = configuration(tmp_path)
    assert AcpBackend.load(config).verification_runtime is None
    descriptor = make_runtime(tmp_path / "runtime")
    data["verification_runtime"] = descriptor
    config.write_text(json.dumps(data))
    assert AcpBackend.load(config).verification_runtime.identity() == descriptor
    data["verification_runtime"] = make_runtime(workspace / "runtime")
    config.write_text(json.dumps(data))
    with pytest.raises(AcpStateError, match="runtime_overlap"):
        AcpBackend.load(config)


def test_runtime_cannot_overlap_an_approved_linked_worktree(tmp_path):
    from test_acp_backend import configuration
    from chatgpt_web_oauth_mcp.acp_backend import AcpBackend
    config, workspace, data = configuration(tmp_path)
    def git(*args):
        subprocess.run(["git", "-C", str(workspace), *args], capture_output=True, check=True)
    git("init", "-q")
    git("add", "sample.py")
    git("-c", "user.name=QA", "-c", "user.email=qa@invalid", "commit", "-qm", "fixture")
    linked = tmp_path / "linked"
    git("worktree", "add", "--detach", "-q", str(linked))
    data["verification_runtime"] = make_runtime(linked / "runtime")
    config.write_text(json.dumps(data))
    backend = AcpBackend.load(config)
    assert backend.workspace_allowed("antigravity", linked)
    with pytest.raises(AcpStateError, match="runtime_overlap"):
        backend.validate(account="antigravity", workspace=linked, kind="code", model=None, timeout=60)
