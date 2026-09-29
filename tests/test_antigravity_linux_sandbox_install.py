from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "scripts" / "install_antigravity_linux_sandbox.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "install_antigravity_linux_sandbox_test",
        INSTALLER_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_agy_profile_keeps_nested_capabilities_minimal() -> None:
    installer = _load_installer()
    profile = installer.build_agy_profile(Path("/home/test/.local/bin/agy"))

    assert "profile agy /home/test/.local/bin/agy" in profile
    assert "profile unpriv_agy" in profile
    assert "allow capability sys_admin," in profile
    assert "allow capability net_admin," in profile
    assert "deny capability setpcap," in profile
    assert "allow capability setpcap," not in profile
    assert "allow pix /** -> &unpriv_agy," in profile


def test_userns_transition_merge_is_idempotent_and_preserves_existing_rules() -> None:
    installer = _load_installer()
    agy_path = Path("/home/test/.local/bin/agy")
    existing = "/custom/tool px -> custom,\n/usr/bin/bwrap px -> bwrap,\n"

    merged = installer.merge_userns_transitions(existing, agy_path)
    second = installer.merge_userns_transitions(merged, agy_path)

    assert merged == second
    assert "/custom/tool px -> custom," in merged
    assert merged.count("/usr/bin/bwrap px -> bwrap,") == 1
    assert merged.count("/home/test/.local/bin/agy px -> agy,") == 1


def test_userns_transition_merge_replaces_stale_agy_path() -> None:
    installer = _load_installer()
    existing = (
        "/usr/bin/bwrap px -> bwrap,\n"
        "/home/old/.local/bin/agy px -> agy,\n"
    )

    merged = installer.merge_userns_transitions(
        existing,
        Path("/home/new/.local/bin/agy"),
    )

    assert "/home/old/.local/bin/agy px -> agy," not in merged
    assert "/home/new/.local/bin/agy px -> agy," in merged


def test_server_unit_disables_no_new_privileges_for_apparmor_transition() -> None:
    installer = _load_installer()
    unit = """[Unit]
Description=Ops MCP

[Service]
ExecStart=/opt/app
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""

    patched = installer.patch_server_unit(unit)

    assert "NoNewPrivileges=true" not in patched
    assert patched.count("NoNewPrivileges=false") == 1
    assert installer.patch_server_unit(patched) == patched


def test_server_unit_adds_no_new_privileges_when_missing() -> None:
    installer = _load_installer()
    unit = """[Service]
ExecStart=/opt/app
PrivateTmp=true
"""

    patched = installer.patch_server_unit(unit)

    assert "ExecStart=/opt/app\nNoNewPrivileges=false\nPrivateTmp=true" in patched
