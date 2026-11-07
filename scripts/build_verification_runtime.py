#!/usr/bin/env python3
"""Build a pinned, credential-free pytest runtime for the ACP verifier (Linux)."""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, build_opener

PINS = ("pytest==9.1.1", "iniconfig==2.3.0", "packaging==26.3", "pluggy==1.6.0", "Pygments==2.21.0")
MAX_WHEEL_BYTES = 16 * 1024 * 1024
MOUNT = "/opt/opss-verification"
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8",
             "PYTHONDONTWRITEBYTECODE": "1", "PIP_CONFIG_FILE": "/dev/null"}


def download_wheel(pin: str, directory: Path) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.+_-]*", pin):
        raise ValueError("Requirements must be exact package==version pins")
    name, version = pin.split("==")
    # No environment proxy or credential handler is used for public artifacts.
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"https://pypi.org/pypi/{quote(name)}/{quote(version)}/json", timeout=30) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Package metadata exceeded its limit")
    metadata = json.loads(raw)
    candidates = [item for item in metadata["urls"]
                  if item["packagetype"] == "bdist_wheel" and item["filename"].endswith("-py3-none-any.whl")]
    if len(candidates) != 1:
        raise ValueError("Exactly one universal pure-Python wheel is required")
    wheel = candidates[0]
    filename = wheel["filename"]
    parsed = urlsplit(wheel["url"])
    if (Path(filename).name != filename or parsed.scheme != "https"
            or parsed.hostname != "files.pythonhosted.org" or parsed.username or parsed.password
            or type(wheel["size"]) is not int or not 0 < wheel["size"] <= MAX_WHEEL_BYTES
            or not re.fullmatch(r"[a-f0-9]{64}", wheel["digests"]["sha256"])):
        raise ValueError("Invalid wheel metadata")
    destination = directory / filename
    digest = hashlib.sha256()
    total = 0
    with opener.open(wheel["url"], timeout=30) as response, destination.open("xb") as output:
        final = urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname != "files.pythonhosted.org":
            raise ValueError("Wheel redirect left the public artifact host")
        while chunk := response.read(65536):
            total += len(chunk)
            if total > wheel["size"]:
                raise ValueError("Wheel download exceeded its declared size")
            digest.update(chunk)
            output.write(chunk)
    if total != wheel["size"] or digest.hexdigest() != wheel["digests"]["sha256"]:
        raise ValueError("Wheel download did not match its SHA256 and size")
    return destination


def normalize_venv(root: Path) -> None:
    """Keep only the configured interpreter; remove host-path console launchers."""
    binary = root / "bin/python"
    if binary.is_symlink():
        target = binary.resolve(strict=True)
        binary.unlink()
        shutil.copyfile(target, binary)
        binary.chmod(0o755)
    for path in (root / "bin").iterdir():
        if path != binary:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    alias = root / "lib64"
    if alias.is_symlink():
        if alias.resolve(strict=True) != root / "lib":
            raise ValueError("Unexpected virtualenv lib64 link")
        alias.unlink()
    for path in root.rglob("__pycache__"):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)


def freeze_manifest(root: Path) -> tuple[str, int, int]:
    files = {}
    total = 0
    for folder, directories, names in os.walk(root, followlinks=False):
        current = Path(folder)
        current.chmod(0o700)
        for name in directories:
            path = current / name
            if path.is_symlink():
                raise ValueError("Runtime directories cannot be symlinks")
        for name in names:
            path = current / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Runtime files must be regular, unlinked files")
            relative = path.relative_to(root).as_posix()
            if not (relative == "pyvenv.cfg" or relative.startswith(("bin/", "lib/", "include/"))):
                raise ValueError("Unexpected runtime path")
            if info.st_size > 16 * 1024 * 1024:
                raise ValueError("Runtime file exceeded 16 MiB")
            mode = 0o555 if info.st_mode & 0o111 else 0o444
            path.chmod(mode)
            data = path.read_bytes()
            total += len(data)
            files[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": mode}
            if len(files) > 10000 or total > 256 * 1024 * 1024:
                raise ValueError("Runtime tree exceeded its limits")
    payload = json.dumps({"version": 1, "files": files}, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > 4 * 1024 * 1024:
        raise ValueError("Runtime manifest exceeded 4 MiB")
    manifest = root / "manifest.json"
    manifest.write_bytes(payload)
    manifest.chmod(0o444)
    return hashlib.sha256(payload).hexdigest(), len(files), total


def publish_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish a new Linux runtime, including collision protection."""
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise RuntimeError("Linux renameat2 is required for atomic publication")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError(destination)
        raise OSError(code, os.strerror(code), str(destination))


def build_runtime(root: Path, runtime_id: str, python: Path, pins: tuple[str, ...] = PINS) -> dict:
    if sys.platform != "linux":
        raise RuntimeError("ACP verification runtime provisioning requires Linux")
    if (not root.is_absolute() or root != root.resolve(strict=False)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", runtime_id)):
        raise ValueError("Use a canonical absolute runtime root and a bounded id")
    if root.exists() or root.is_symlink():
        raise FileExistsError(root)
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = root.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise ValueError("Runtime parent must be owned and not group/world writable")
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("An executable absolute Python path is required")
    from chatgpt_web_oauth_mcp.acp_verification_runtime import VerificationRuntime
    with tempfile.TemporaryDirectory(prefix=".runtime-build-", dir=root.parent) as temporary:
        temporary_root = Path(temporary)
        environment = temporary_root / "runtime"
        wheels = temporary_root / "wheels"
        wheels.mkdir(mode=0o700)
        for pin in pins:
            download_wheel(pin, wheels)
        subprocess.run([str(python), "-I", "-m", "venv", "--copies", "--without-pip", str(environment)],
                       check=True, stdin=subprocess.DEVNULL, env=CLEAN_ENV)
        subprocess.run([sys.executable, "-I", "-m", "pip", "--isolated", "--no-cache-dir",
                        "--disable-pip-version-check", "--python", str(environment / "bin/python"),
                        "install", "--no-index", "--find-links", str(wheels), "--no-deps", "--no-compile", *pins],
                       check=True, stdin=subprocess.DEVNULL, env=CLEAN_ENV)
        normalize_venv(environment)
        probe = temporary_root / "probe"
        probe.mkdir(mode=0o700)
        (probe / "test_runtime.py").write_text("import pytest\ndef test_runtime():\n    assert pytest.__version__ == '" + pins[0].split("==")[1] + "'\n")
        subprocess.run([str(environment / "bin/python"), "-I", "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", str(probe)],
                       cwd=probe, check=True, stdin=subprocess.DEVNULL, env=CLEAN_ENV)
        digest, count, size = freeze_manifest(environment)
        staging_profile = {"id": runtime_id, "root": str(environment), "manifest_sha256": digest, "python": "bin/python"}
        VerificationRuntime.load(staging_profile).validate()
        publish_no_replace(environment, root)
    profile = {"id": runtime_id, "root": str(root), "manifest_sha256": digest, "python": "bin/python"}
    VerificationRuntime.load(profile).validate()
    return {"verification_runtime": profile, "pins": list(pins), "files": count, "bytes": size}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--id", required=True, dest="runtime_id")
    parser.add_argument("--python", type=Path, default=Path("/usr/bin/python3"))
    args = parser.parse_args()
    previous = os.umask(0o077)
    try:
        print(json.dumps(build_runtime(args.root, args.runtime_id, args.python), sort_keys=True))
    finally:
        os.umask(previous)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    main()
