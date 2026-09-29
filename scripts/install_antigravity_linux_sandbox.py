#!/usr/bin/env python3
"""Install the Linux AppArmor policy required by sandboxed Antigravity delegates."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


DEFAULT_UNIT_PATH = (
    Path.home() / ".config/systemd/user/chatgpt-web-oauth-mcp.service"
)
APPARMOR_PROFILE_PATH = Path("/etc/apparmor.d/agy-userns-restrict")
APPARMOR_LOCAL_USERNS_PATH = Path("/etc/apparmor.d/local/unprivileged_userns")
APPARMOR_USERNS_PROFILE_PATH = Path("/etc/apparmor.d/unprivileged_userns")
APPARMOR_BWRAP_PROFILE_PATH = Path("/etc/apparmor.d/bwrap-userns-restrict")


def build_agy_profile(agy_path: Path) -> str:
    agy = str(agy_path.resolve())
    return f"""abi <abi/4.0>,

include <tunables/global>

profile agy {agy} flags=(attach_disconnected) {{
  allow capability,
  allow file rwlkm /{{**,}},
  allow network,
  allow unix,
  allow ptrace,
  allow signal,
  allow mqueue,
  allow io_uring,
  allow userns,
  allow mount,
  allow umount,
  allow pivot_root,
  allow dbus,

  allow px /** -> agy//&unpriv_agy,
}}

profile unpriv_agy flags=(attach_disconnected) {{
  allow file rwlkm /{{**,}},
  allow network,
  allow unix,
  allow ptrace,
  allow signal,
  allow mqueue,
  allow io_uring,
  allow userns,
  allow mount,
  allow umount,
  allow pivot_root,
  allow dbus,

  # AGY configures mounts and networking inside its private namespaces.
  allow capability sys_admin,
  allow capability net_admin,

  # AGY probes SETPCAP while bootstrapping the sandbox. It is not required
  # for successful command execution, so keep it denied without audit noise.
  deny capability setpcap,

  allow pix /** -> &unpriv_agy,
}}
"""


def merge_userns_transitions(existing: str, agy_path: Path) -> str:
    required = [
        "/usr/bin/bwrap px -> bwrap,",
        f"{agy_path.resolve()} px -> agy,",
    ]
    lines = [
        line
        for line in existing.splitlines()
        if not line.strip().endswith(" px -> agy,")
    ]
    present = {line.strip() for line in lines}
    for rule in required:
        if rule not in present:
            lines.append(rule)
    return "\n".join(lines).rstrip() + "\n"


def patch_server_unit(unit: str) -> str:
    lines = unit.splitlines()
    try:
        service_start = lines.index("[Service]")
    except ValueError as exc:
        raise ValueError("systemd unit has no [Service] section") from exc

    service_end = next(
        (
            index
            for index in range(service_start + 1, len(lines))
            if lines[index].startswith("[") and lines[index].endswith("]")
        ),
        len(lines),
    )

    found = False
    for index in range(service_start + 1, service_end):
        if lines[index].startswith("NoNewPrivileges="):
            lines[index] = "NoNewPrivileges=false"
            found = True

    if not found:
        insert_at = next(
            (
                index
                for index in range(service_start + 1, service_end)
                if lines[index].startswith("PrivateTmp=")
            ),
            service_end,
        )
        lines.insert(insert_at, "NoNewPrivileges=false")

    return "\n".join(lines).rstrip() + "\n"


def _sudo_install(source: Path, destination: Path) -> None:
    subprocess.run(
        ["sudo", "install", "-m", "0644", str(source), str(destination)],
        check=True,
    )


def validate_profile(profile: str) -> None:
    with tempfile.TemporaryDirectory(prefix="agy-apparmor-validate-") as temp_dir:
        profile_source = Path(temp_dir) / "agy-userns-restrict"
        profile_source.write_text(profile, encoding="utf-8")
        subprocess.run(
            ["/usr/sbin/apparmor_parser", "-Q", "-T", str(profile_source)],
            check=True,
        )


def install(
    *,
    agy_path: Path,
    unit_path: Path,
    restart_service: bool,
    apply: bool,
) -> dict[str, object]:
    if not agy_path.is_file():
        raise RuntimeError(f"Antigravity executable not found: {agy_path}")
    if not APPARMOR_USERNS_PROFILE_PATH.is_file():
        raise RuntimeError(f"Missing AppArmor profile: {APPARMOR_USERNS_PROFILE_PATH}")
    if not APPARMOR_BWRAP_PROFILE_PATH.is_file():
        raise RuntimeError(f"Missing AppArmor bwrap profile: {APPARMOR_BWRAP_PROFILE_PATH}")
    if not unit_path.is_file():
        raise RuntimeError(f"Missing MCP systemd user unit: {unit_path}")

    profile = build_agy_profile(agy_path)
    existing_local = (
        APPARMOR_LOCAL_USERNS_PATH.read_text(encoding="utf-8")
        if APPARMOR_LOCAL_USERNS_PATH.exists()
        else ""
    )
    transitions = merge_userns_transitions(existing_local, agy_path)
    patched_unit = patch_server_unit(unit_path.read_text(encoding="utf-8"))
    validate_profile(profile)

    result: dict[str, object] = {
        "apply": apply,
        "agy_path": str(agy_path.resolve()),
        "unit_path": str(unit_path),
        "profile_path": str(APPARMOR_PROFILE_PATH),
        "local_userns_path": str(APPARMOR_LOCAL_USERNS_PATH),
        "restart_service": restart_service,
    }
    if not apply:
        return result

    with tempfile.TemporaryDirectory(prefix="agy-apparmor-") as temp_dir:
        temp = Path(temp_dir)
        profile_source = temp / "agy-userns-restrict"
        transitions_source = temp / "unprivileged_userns"
        profile_source.write_text(profile, encoding="utf-8")
        transitions_source.write_text(transitions, encoding="utf-8")

        _sudo_install(profile_source, APPARMOR_PROFILE_PATH)
        _sudo_install(transitions_source, APPARMOR_LOCAL_USERNS_PATH)

    subprocess.run(
        ["sudo", "/usr/sbin/apparmor_parser", "-r", str(APPARMOR_PROFILE_PATH)],
        check=True,
    )
    subprocess.run(
        ["sudo", "/usr/sbin/apparmor_parser", "-r", str(APPARMOR_USERNS_PROFILE_PATH)],
        check=True,
    )
    subprocess.run(
        ["sudo", "/usr/sbin/apparmor_parser", "-r", str(APPARMOR_BWRAP_PROFILE_PATH)],
        check=True,
    )

    if patched_unit != unit_path.read_text(encoding="utf-8"):
        unit_path.write_text(patched_unit, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if restart_service:
        subprocess.run(
            ["systemctl", "--user", "restart", "chatgpt-web-oauth-mcp.service"],
            check=True,
        )

    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    default_agy = shutil.which("agy")
    parser.add_argument(
        "--agy-path",
        type=Path,
        default=Path(default_agy) if default_agy else Path.home() / ".local/bin/agy",
    )
    parser.add_argument("--unit-path", type=Path, default=DEFAULT_UNIT_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()

    result = install(
        agy_path=args.agy_path.expanduser(),
        unit_path=args.unit_path.expanduser(),
        restart_service=not args.no_restart,
        apply=args.apply,
    )
    for key, value in result.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
