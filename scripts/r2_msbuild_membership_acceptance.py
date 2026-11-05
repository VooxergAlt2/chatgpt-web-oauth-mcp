#!/usr/bin/env python3
"""Check C# Compile membership with an offline MSBuild evaluation on Linux.

Requires bubblewrap with user namespaces and an installed .NET SDK 8.0.425.
Run: python3 scripts/r2_msbuild_membership_acceptance.py --dotnet ~/.dotnet/dotnet
The fixture is temporary. Each query uses a fresh sandbox with read-only input,
private scratch space, and no network. No restore or build target is requested.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SDK = "8.0.425"
LINKED = "../shared/External.cs"
FRAMEWORKS = "net8.0;netstandard2.1"
PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFrameworks>net8.0;netstandard2.1</TargetFrameworks></PropertyGroup>
  <ItemGroup>
    <Compile Remove="Omit.cs" />
    <Compile Remove="Net8.cs" Condition="'$(TargetFramework)' != 'net8.0'" />
    <Compile Remove="DebugOnly.cs" Condition="'$(Configuration)' != 'Debug'" />
    <Compile Include="../shared/External.cs" Link="Logical/External.cs" />
  </ItemGroup>
  <Import Project="Absent.props" Condition="'$(ProbeMissing)' == 'true'" />
  <Target Name="DoNotRun" BeforeTargets="Restore;Build;CoreCompile">
    <Error Text="UNEXPECTED_TARGET_EXECUTION" />
  </Target>
</Project>
"""


def sandbox_command(root: Path, fixture: Path) -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("bubblewrap executable is required")
    command = [
        bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
    ]
    if Path("/usr/lib64").exists():
        command.extend(["--symlink", "usr/lib64", "/lib64"])
    for relative in ("dotnet", "host", "shared", "packs", f"sdk/{SDK}"):
        source = root / relative
        if not source.exists():
            raise RuntimeError(f"missing .NET prerequisite: {source}")
        command.extend(["--ro-bind", str(source), "/sdk/" + relative])
    command.extend(["--ro-bind", str(fixture), "/case", "--chdir", "/case/app", "--clearenv"])
    for key, value in {
        "PATH": "/usr/bin:/bin", "HOME": "/tmp/home", "DOTNET_ROOT": "/sdk",
        "DOTNET_CLI_HOME": "/tmp/home", "DOTNET_NOLOGO": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "MSBuildEnableWorkloadResolver": "false",
        "NUGET_PACKAGES": "/tmp/packages",
    }.items():
        command.extend(["--setenv", key, value])
    return command + [
        "--", "/sdk/dotnet", f"/sdk/sdk/{SDK}/MSBuild.dll", "-nologo", "-noAutoResponse", "-nodeReuse:false",
    ]


def run_fixture(dotnet: Path) -> dict:
    dotnet = dotnet.resolve(strict=True)
    if dotnet.name != "dotnet":
        raise ValueError("--dotnet must identify the dotnet host executable")
    with tempfile.TemporaryDirectory(prefix="opss-r2-msbuild-") as directory:
        root = Path(directory).resolve()
        app = root / "app"
        app.mkdir()
        (root / "shared").mkdir()
        (app / "Case.csproj").write_text(PROJECT, encoding="utf-8")
        for source in ("Base.cs", "Omit.cs", "Net8.cs", "DebugOnly.cs", LINKED):
            (app / source).write_text(f"class {Path(source).stem} {{}}\n", encoding="utf-8")

        command = sandbox_command(dotnet.parent, root)
        version = subprocess.run(command + ["-version"], capture_output=True, text=True, timeout=15, check=True)
        report = {"sdk": SDK, "msbuild": version.stdout.strip(), "evaluations": []}
        cases = (
            ("", "Debug", [LINKED]),
            ("net8.0", "Debug", [LINKED, "Base.cs", "DebugOnly.cs", "Net8.cs"]),
            ("net8.0", "Release", [LINKED, "Base.cs", "Net8.cs"]),
            ("netstandard2.1", "Debug", [LINKED, "Base.cs", "DebugOnly.cs"]),
            ("netstandard2.1", "Release", [LINKED, "Base.cs"]),
            ("net8.0", "Debug", None),
        )
        for tfm, configuration, expected in cases:
            args = ["Case.csproj", "-getProperty:TargetFramework,TargetFrameworks,Configuration",
                    "-getItem:Compile", "-p:Configuration=" + configuration]
            if tfm:
                args.append("-p:TargetFramework=" + tfm)
            if expected is None:
                args.append("-p:ProbeMissing=true")
            start = time.monotonic()
            result = subprocess.run(command + args, capture_output=True, text=True, timeout=15)
            label = tfm or "outer"
            row = {"tfm": label, "configuration": configuration,
                   "elapsed_seconds": round(time.monotonic() - start, 3), "exit_code": result.returncode}
            if expected is None:
                diagnostic = result.stdout + result.stderr
                if result.returncode == 0 or "MSB4019" not in diagnostic or "UNEXPECTED_TARGET_EXECUTION" in diagnostic:
                    raise RuntimeError(f"missing import must fail evaluation with MSB4019: {diagnostic}")
                row["missing_import"] = "MSB4019"
            else:
                if result.returncode:
                    raise RuntimeError(f"{label}/{configuration}: {result.stdout}{result.stderr}")
                evaluation = json.loads(result.stdout)
                properties = {"TargetFramework": tfm, "TargetFrameworks": FRAMEWORKS, "Configuration": configuration}
                if evaluation["Properties"] != properties:
                    raise RuntimeError(f"{label}/{configuration}: unexpected properties {evaluation['Properties']}")
                items = evaluation["Items"]["Compile"]
                names = sorted(item["Identity"] for item in items)
                if names != sorted(expected):
                    raise RuntimeError(f"{label}/{configuration}: unexpected Compile items {names}")
                linked = next(item for item in items if item["Identity"] == LINKED)
                if (linked["Link"], linked["FullPath"]) != ("Logical/External.cs", "/case/shared/External.cs"):
                    raise RuntimeError(f"{label}/{configuration}: linked file metadata differs")
                row["compile"] = names
            report["evaluations"].append(row)
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dotnet", type=Path, default=shutil.which("dotnet") or Path.home() / ".dotnet/dotnet",
                        help="installed dotnet executable (defaults to PATH or ~/.dotnet/dotnet)")
    args = parser.parse_args()
    try:
        report = run_fixture(args.dotnet)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit(f"MSBuild membership acceptance failed: {exc}")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
