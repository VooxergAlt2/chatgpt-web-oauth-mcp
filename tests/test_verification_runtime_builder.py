"""Provisioning preserves publication and filesystem trust boundaries."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

SPEC = importlib.util.spec_from_file_location(
    "runtime_builder", Path(__file__).parents[1] / "scripts/build_verification_runtime.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux runtime provisioning")


def test_atomic_publication_never_replaces_existing_runtime(tmp_path):
    first, second, destination = (tmp_path / name for name in ("first", "second", "published"))
    first.mkdir()
    second.mkdir()
    (first / "value").write_text("accepted")
    (second / "value").write_text("other")
    builder.publish_no_replace(first, destination)
    with pytest.raises(FileExistsError):
        builder.publish_no_replace(second, destination)
    assert (destination / "value").read_text() == "accepted"
    assert (second / "value").read_text() == "other"


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_manifest_refuses_files_linked_outside_runtime(tmp_path, kind):
    root = tmp_path / "runtime"
    (root / "lib").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("must-not-be-copied")
    target = root / "lib/linked"
    if kind == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)
    with pytest.raises(ValueError, match="regular, unlinked"):
        builder.freeze_manifest(root)
    assert not (root / "manifest.json").exists()
    assert outside.read_text() == "must-not-be-copied"


def test_freeze_manifest_binds_contents_and_readonly_modes(tmp_path):
    root = tmp_path / "runtime"
    (root / "bin").mkdir(parents=True)
    (root / "bin/python").write_bytes(b"executable")
    (root / "bin/python").chmod(0o755)
    (root / "pyvenv.cfg").write_text("home = /usr/bin\ninclude-system-site-packages = false\n")
    digest, count, size = builder.freeze_manifest(root)
    manifest = root / "manifest.json"
    assert digest == builder.hashlib.sha256(manifest.read_bytes()).hexdigest()
    records = json.loads(manifest.read_bytes())["files"]
    assert count == 2 and size == sum(row["size"] for row in records.values())
    assert records["bin/python"]["mode"] == 0o555
    assert records["pyvenv.cfg"]["mode"] == 0o444
    assert manifest.stat().st_mode & 0o777 == 0o444
    for relative, row in records.items():
        path = root / relative
        assert path.stat().st_mode & 0o777 == row["mode"]
        assert builder.hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]


def test_failed_download_does_not_publish_or_leave_build_directory(tmp_path, monkeypatch):
    root = tmp_path / "published"
    def fail(*args):
        raise ValueError("artifact download failed")
    monkeypatch.setattr(builder, "download_wheel", fail)
    with pytest.raises(ValueError, match="artifact download failed"):
        builder.build_runtime(root, "pytest-fixture", Path("/usr/bin/python3"))
    assert not root.exists()
    assert not list(tmp_path.glob(".runtime-build-*"))


def test_existing_runtime_is_not_modified_by_builder(tmp_path):
    root = tmp_path / "published"
    root.mkdir()
    (root / "sentinel").write_text("existing")
    with pytest.raises(FileExistsError):
        builder.build_runtime(root, "pytest-fixture", Path("/usr/bin/python3"))
    assert (root / "sentinel").read_text() == "existing"
