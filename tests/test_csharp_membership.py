import subprocess
import sys

import pytest

from chatgpt_web_oauth_mcp import csharp_membership as engine


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def case(tmp_path):
    repo = tmp_path / "approved"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / ".gitattributes").write_text("Example.cs export-ignore\n")
    (repo / "Project.csproj").write_text("<Project/>")
    (repo / "Example.cs").write_text("original")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=test", "-c", "user.email=test@local", "commit", "-qm", "base")
    dotnet = tmp_path / "dotnet"
    dotnet.touch()
    return dict(cwd=repo, ref="HEAD", project="Project.csproj", file="Example.cs", platform="AnyCPU",
                configuration="Debug", target_framework=None, dotnet=dotnet,
                allowed_roots=(repo, tmp_path / "removed-project"))


def evaluation(frameworks="", framework="", items=None):
    return {"Properties": {"TargetFrameworks": frameworks, "TargetFramework": framework},
            "Items": {"Compile": [] if items is None else items}}


def test_committed_inputs_ignore_dirty_tree_and_export_attributes(case, monkeypatch):
    (case["cwd"] / "Example.cs").write_text("modified")
    def sandbox(root, snapshot, project_dir):
        assert (snapshot / "Example.cs").read_text() == "original"
        assert not (snapshot / ".git").exists()
        return []
    monkeypatch.setattr(engine, "sandbox_command", sandbox)
    monkeypatch.setattr(engine, "_evaluate", lambda *args: evaluation(
        items=[{"FullPath": "/case/Example.cs", "Link": "Shared/Example.cs"}]))
    result = engine.csharp_project_membership(**case)
    assert result["included"] is True and result["logical_links"] == ["Shared/Example.cs"]
    assert result["commit_sha"] == git(case["cwd"], "rev-parse", "HEAD")


def test_solution_mapping_from_same_commit_drives_project_evaluation(case, monkeypatch):
    repo = case["cwd"]
    solution = repo / "Demo.sln"
    guid = "{00000000-0000-0000-0000-000000000001}"
    template = f'''Microsoft Visual Studio Solution File, Format Version 12.00
Project("{{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}}") = "Project", "Project.csproj", "{guid}"
EndProject
Global
    GlobalSection(SolutionConfigurationPlatforms) = preSolution
        Debug|Any CPU = Debug|Any CPU
    EndGlobalSection
    GlobalSection(ProjectConfigurationPlatforms) = postSolution
        {guid}.Debug|Any CPU.ActiveCfg = Release|x64
        {guid}.Debug|Any CPU.Build.0 = Release|x64
    EndGlobalSection
EndGlobal
'''
    solution.write_text(template, encoding="utf-8-sig")
    git(repo, "add", "Demo.sln")
    git(repo, "-c", "user.name=test", "-c", "user.email=test@local", "commit", "-qm", "solution")
    solution.write_text(template.replace("Release|x64", "Debug|x86"))
    def sandbox(root, snapshot, project_dir, *, solution_query=False):
        assert "Release|x64" in (snapshot / "Demo.sln").read_text()
        return ["solution" if solution_query else "project"]

    monkeypatch.setattr(engine, "sandbox_command", sandbox)
    def resolve(argv, limit):
        assert argv == ["solution", "/case/Demo.sln", "/case/Project.csproj", "Debug", "Any CPU"]
        return {"status": "mapped", "configuration": "Release", "platform": "x64"}

    monkeypatch.setattr(engine, "_sandbox_json", resolve)
    observed = []

    def evaluate(command, project, configuration, platform):
        observed.append((configuration, platform))
        return evaluation(items=[{"FullPath": "/case/Example.cs"}])

    monkeypatch.setattr(engine, "_evaluate", evaluate)
    result = engine.csharp_project_membership(**(case | {"solution": "Demo.sln", "platform": "Any CPU"}))
    assert result["included"] is True
    assert (result["solution_configuration"], result["solution_platform"]) == ("Debug", "Any CPU")
    assert observed == [("Release", "x64")]
    assert (result["configuration"], result["platform"]) == observed[0]


def test_solution_build_disabled(case, monkeypatch):
    repo = case["cwd"]
    (repo / "Demo.sln").write_text("Microsoft Visual Studio Solution File, Format Version 12.00\n")
    git(repo, "add", "Demo.sln")
    git(repo, "-c", "user.name=test", "-c", "user.email=test@local", "commit", "-qm", "solution")
    monkeypatch.setattr(engine, "sandbox_command", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "_sandbox_json", lambda *args, **kwargs: {"status": "build_disabled"})
    monkeypatch.setattr(engine, "_evaluate", lambda *args, **kwargs: pytest.fail("unexpected evaluate"))
    result = engine.csharp_project_membership(**(case | {"solution": "Demo.sln"}))
    assert result["included"] is False
    assert result["reason"] == "build_disabled"
    assert result["source"] == "Solution.ProjectConfigurationPlatforms"


@pytest.mark.parametrize(("change", "reason"), [
    ({"configuration": "Debug;Danger=1"}, "invalid_dimension"),
    ({"file": "../outside.cs"}, "invalid_relative_path"),
    ({"ref": "--config=unexpected"}, "invalid_git_ref"),
    ({"allowed_roots": ()}, "workspace_not_allowed"),
])
def test_invalid_requests_fail_before_export(case, monkeypatch, change, reason):
    monkeypatch.setattr(engine, "_export", lambda *args: pytest.fail("unexpected export"))
    result = engine.csharp_project_membership(**(case | change))
    assert result["included"] is None and result["reason"] == reason


def test_unsupported_entry_and_size_limits_fail_before_evaluation(case, monkeypatch):
    repo = case["cwd"]
    monkeypatch.setattr(engine, "MAX_SNAPSHOT_BYTES", 4)
    assert engine.csharp_project_membership(**case)["reason"] == "snapshot_limit_exceeded"
    monkeypatch.setattr(engine, "MAX_SNAPSHOT_BYTES", 65536)
    (repo / "Alias.cs").symlink_to("/outside.cs")
    git(repo, "add", "Alias.cs")
    git(repo, "-c", "user.name=test", "-c", "user.email=test@local", "commit", "-qm", "link")
    assert engine.csharp_project_membership(**case)["reason"] == "unsupported_git_entry"


@pytest.mark.parametrize(("tfm", "expected"), [(None, "target_framework_required"),
                                               ("net7.0", "target_framework_unavailable"),
                                               ("net8.0", None)])
def test_frameworks_require_valid_inner_evaluation(case, monkeypatch, tfm, expected):
    monkeypatch.setattr(engine, "sandbox_command", lambda *args: [])
    calls = []
    def evaluate(*args):
        calls.append(args)
        return evaluation("net8.0;net9.0", args[4] if len(args) == 5 else "")
    monkeypatch.setattr(engine, "_evaluate", evaluate)
    result = engine.csharp_project_membership(**(case | {"target_framework": tfm}))
    assert result.get("reason") == expected
    assert result["included"] is (None if expected else False)
    assert len(calls) == (1 if expected else 2)


def test_unknown_external_item_and_bounded_link_receipt(case, monkeypatch):
    monkeypatch.setattr(engine, "sandbox_command", lambda *args: [])
    items = [{"FullPath": "/case/Example.cs", "Link": "A" * 2048}]
    monkeypatch.setattr(engine, "_evaluate", lambda *args: evaluation(items=items))
    result = engine.csharp_project_membership(**case)
    assert result["included"] is True and result["match_count"] == 1
    assert result["logical_links"] == [] and result["logical_links_truncated"] is True
    items.append({"FullPath": "/sdk/Unknown.cs"})
    unknown = engine.csharp_project_membership(**case)
    assert unknown["included"] is None and unknown["reason"] == "compile_outside_snapshot"


@pytest.mark.parametrize("fd", [1, 2])
def test_draining_limits_each_output_pipe(fd):
    with pytest.raises(engine._Unavailable) as err:
        engine._bounded([sys.executable, "-c", f"import os; os.write({fd}, b'x' * 65536)"], limit=256)
    assert err.value.reason == "output_limit_exceeded"


@pytest.mark.parametrize("path,limit", [("a/b/c/d/e.cs", "MAX_DEPTH"), ("a/b/c.cs", "MAX_DIRECTORIES")])
def test_directory_limits_apply_before_any_file_is_created(monkeypatch, tmp_path, path, limit):
    listing = ("100644 blob " + "a" * 40 + " 1\t" + path + "\0").encode()
    def listing_only(repo, *args, **kwargs):
        assert args[0] == "ls-tree"
        return listing
    monkeypatch.setattr(engine, "_git", listing_only)
    monkeypatch.setattr(engine, limit, 2)
    with pytest.raises(engine._Unavailable) as err:
        engine._export(tmp_path, "a" * 40, tmp_path)
    assert err.value.reason == "snapshot_limit_exceeded"
    assert list(tmp_path.iterdir()) == []
