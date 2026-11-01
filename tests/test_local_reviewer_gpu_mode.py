from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = ROOT / "scripts" / "dem_gpu_mode_control.py"
INSTALLER_PATH = ROOT / "scripts" / "install_local_reviewer_gpu_mode.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _isolate_speakr_job_ledger(monkeypatch, tmp_path: Path) -> None:
    # Controller tests must never depend on the production Speakr job queue.
    monkeypatch.setenv("DEM_GPU_SPEAKR_JOB_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("DEM_GPU_SPEAKR_LEASE_PATH", str(tmp_path / "gpu-lease.lock"))


def test_reviewer_unit_is_two_slot_qwen38_q8q4_profile() -> None:
    installer = _load(INSTALLER_PATH, "install_local_reviewer_gpu_mode_test")
    unit = installer._llama_unit("vooxerg")

    assert installer.REVIEWER_TOTAL_CONTEXT == 137_216
    assert installer.REVIEWER_PARALLEL_SLOTS == 2
    assert "llama-server" in unit
    assert str(installer.MODEL_PATH) in unit
    assert "--host 127.0.0.1" in unit
    assert "--host 0.0.0.0" not in unit
    assert "--port 8083" in unit
    assert "--alias qwen38-27b" in unit
    assert "-c 137216" in unit
    assert "-ctk q8_0" in unit
    assert "-ctv q4_0" in unit
    assert "-np 2" in unit
    assert "-ub 128" in unit
    assert "-fit off" in unit
    assert "ConditionPathIsMountPoint=/mnt/rag-reference" in unit
    assert f"ConditionPathExists={installer.MODEL_PATH}" in unit
    assert "User=" not in unit
    assert "Group=" not in unit
    assert "WantedBy=default.target" in unit


def test_gpu_mode_path_unit_watches_only_bounded_request_file() -> None:
    installer = _load(INSTALLER_PATH, "install_local_reviewer_gpu_mode_path_test")
    unit = installer._path_unit("vooxerg")

    assert f"PathChanged={installer._request_path('vooxerg')}" in unit
    assert "Unit=dem-gpu-mode.service" in unit
    assert "WantedBy=default.target" in unit


def test_gpu_mode_control_unit_reads_canonical_rag_storage_env() -> None:
    installer = _load(INSTALLER_PATH, "install_local_reviewer_gpu_mode_control_unit_test")
    unit = installer._control_unit("vooxerg")

    assert "EnvironmentFile=/etc/" not in unit
    assert f"ExecStart={installer._controller_path('vooxerg')} apply" in unit


def test_installer_prepends_local2_without_duplicating_existing_fallbacks(tmp_path: Path) -> None:
    installer = _load(INSTALLER_PATH, "install_local_reviewer_gpu_mode_env_test")
    env_path = tmp_path / "env"
    env_path.write_text(
        "CHATGPT_MCP_DELEGATE_FALLBACK_HARNESSES=antigravity,local2,codex\n",
        encoding="utf-8",
    )

    installer._prepend_csv_env_value(
        env_path,
        "CHATGPT_MCP_DELEGATE_FALLBACK_HARNESSES",
        "local2",
    )

    assert env_path.read_text(encoding="utf-8") == (
        "CHATGPT_MCP_DELEGATE_FALLBACK_HARNESSES=local2,antigravity,codex\n"
    )


def test_installer_migrates_legacy_devstral_request_to_reviewer(tmp_path: Path) -> None:
    installer = _load(INSTALLER_PATH, "install_local_reviewer_gpu_mode_request_migration_test")
    request_path = tmp_path / "gpu-mode.request"
    request_path.write_text("devstral\n", encoding="utf-8")

    installer._migrate_request(request_path)

    assert request_path.read_text(encoding="utf-8") == "reviewer\n"


def test_installer_preserves_canonical_request(tmp_path: Path) -> None:
    installer = _load(INSTALLER_PATH, "install_local_reviewer_gpu_mode_request_preserve_test")
    request_path = tmp_path / "gpu-mode.request"
    request_path.write_text("rag\n", encoding="utf-8")

    installer._migrate_request(request_path)

    assert request_path.read_text(encoding="utf-8") == "rag\n"


def test_installer_removes_legacy_devstral_env_keys(tmp_path: Path) -> None:
    installer = _load(INSTALLER_PATH, "install_local_reviewer_gpu_mode_env_cleanup_test")
    env_path = tmp_path / "ha-monitor.env"
    env_path.write_text(
        "KEEP=value\n"
        "OPS_MCP_GPU_MODE_DEVSTRAL_HEALTH_URL=http://127.0.0.1:8083/health\n"
        "OPS_MCP_GPU_MODE_DEVSTRAL_UNIT=llama-devstral-gip.service\n",
        encoding="utf-8",
    )

    installer._remove_env_keys(
        env_path,
        {
            "OPS_MCP_GPU_MODE_DEVSTRAL_HEALTH_URL",
            "OPS_MCP_GPU_MODE_DEVSTRAL_UNIT",
        },
    )

    assert env_path.read_text(encoding="utf-8") == "KEEP=value\n"


def test_installer_fails_when_initial_gpu_mode_apply_fails(monkeypatch, tmp_path: Path) -> None:
    installer = _load(INSTALLER_PATH, "install_local_reviewer_gpu_mode_apply_failure_test")
    controller_path = tmp_path / "dem-gpu-mode-control"

    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            2,
            stdout="",
            stderr="unsafe transition rejected",
        ),
    )

    with pytest.raises(
        RuntimeError,
        match=r"GPU mode apply failed \(2\): unsafe transition rejected",
    ):
        installer._apply_gpu_mode(controller_path)


def test_controller_rejects_reviewer_when_model_is_not_ready(monkeypatch) -> None:
    controller = _load(CONTROLLER_PATH, "dem_gpu_mode_missing_model_test")
    states: list[dict[str, object]] = []
    monkeypatch.setattr(controller, "_model_ready", lambda: False)
    monkeypatch.setattr(
        controller,
        "_write_state",
        lambda **kwargs: states.append(kwargs),
    )

    rc = controller._switch_to_reviewer()

    assert rc == 2
    assert states[-1]["status"] == "rejected"
    assert states[-1]["error"] == "reviewer_model_missing_or_wrong_size"


def test_controller_switches_rag_to_reviewer_in_fixed_order(monkeypatch) -> None:
    controller = _load(CONTROLLER_PATH, "dem_gpu_mode_switch_test")
    commands: list[tuple[str, ...]] = []
    posts: list[str] = []
    states: list[dict[str, object]] = []
    monkeypatch.setattr(controller, "_model_ready", lambda: True)
    monkeypatch.setattr(controller, "_mode", lambda: "rag")
    monkeypatch.setattr(controller, "_maintenance_available", lambda: True)
    monkeypatch.setattr(controller, "_wait_rag_state", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_wait_user_unit", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_wait_http", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_inflight_source_runs", lambda: [])
    monkeypatch.setattr(controller, "_active_rag_background_services", lambda: [])
    monkeypatch.setattr(
        controller,
        "_write_state",
        lambda **kwargs: states.append(kwargs),
    )

    def fake_systemctl(*args: str, check: bool = False):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_post(url: str, **_kwargs):
        posts.append(url)
        return {"ok": True, "state": "suspended"}

    monkeypatch.setattr(controller, "_run_user_systemctl", fake_systemctl)
    monkeypatch.setattr(controller, "_post_json", fake_post)

    rc = controller._switch_to_reviewer()

    assert rc == 0
    assert posts == [controller.RAG_SUSPEND]
    assert commands == [("start", controller.REVIEWER_UNIT)]
    assert states[0]["status"] == "switching"
    assert states[-1]["status"] == "ready"


def test_controller_rejects_old_rag_without_maintenance_api(monkeypatch) -> None:
    controller = _load(CONTROLLER_PATH, "dem_gpu_mode_no_maintenance_test")
    states: list[dict[str, object]] = []
    posts: list[str] = []
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(controller, "_model_ready", lambda: True)
    monkeypatch.setattr(controller, "_mode", lambda: "rag")
    monkeypatch.setattr(controller, "_maintenance_available", lambda: False)
    monkeypatch.setattr(controller, "_write_state", lambda **kwargs: states.append(kwargs))
    monkeypatch.setattr(
        controller,
        "_post_json",
        lambda url, **_kwargs: posts.append(url) or {"ok": True},
    )
    monkeypatch.setattr(
        controller,
        "_run_user_systemctl",
        lambda *args, **_kwargs: commands.append(args)
        or subprocess.CompletedProcess(args, 0, "", ""),
    )

    rc = controller._switch_to_reviewer()

    assert rc == 2
    assert posts == []
    assert commands == []
    assert states[-1]["status"] == "rejected"
    assert states[-1]["error"] == "rag_maintenance_control_unavailable"


def test_controller_resumes_rag_if_background_work_races_suspend(monkeypatch) -> None:
    controller = _load(CONTROLLER_PATH, "dem_gpu_mode_race_test")
    states: list[dict[str, object]] = []
    posts: list[str] = []
    source_checks = iter(
        [
            [],
            [
                {
                    "run_id": "run-raced",
                    "scope": "project:test",
                    "source_id": "files",
                    "state": "running",
                    "active_slot": 1,
                }
            ],
        ]
    )
    monkeypatch.setattr(controller, "_model_ready", lambda: True)
    monkeypatch.setattr(controller, "_mode", lambda: "rag")
    monkeypatch.setattr(controller, "_maintenance_available", lambda: True)
    monkeypatch.setattr(controller, "_inflight_source_runs", lambda: next(source_checks))
    monkeypatch.setattr(controller, "_active_rag_background_services", lambda: [])
    monkeypatch.setattr(controller, "_wait_rag_state", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_wait_http", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_write_state", lambda **kwargs: states.append(kwargs))
    monkeypatch.setattr(
        controller,
        "_post_json",
        lambda url, **_kwargs: posts.append(url) or {"ok": True},
    )

    rc = controller._switch_to_reviewer()

    assert rc == 2
    assert posts == [controller.RAG_SUSPEND, controller.RAG_RESUME]
    assert states[-1]["status"] == "rejected"
    assert states[-1]["error"] == "rag_background_work_raced_suspend"


def test_controller_switches_reviewer_back_to_rag_in_fixed_order(monkeypatch) -> None:
    controller = _load(CONTROLLER_PATH, "dem_gpu_mode_to_rag_test")
    commands: list[tuple[str, ...]] = []
    posts: list[str] = []
    states: list[dict[str, object]] = []
    monkeypatch.setattr(controller, "_mode", lambda: "reviewer")
    monkeypatch.setattr(controller, "_wait_user_unit", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_wait_http", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_write_state", lambda **kwargs: states.append(kwargs))
    monkeypatch.setattr(
        controller,
        "_run_user_systemctl",
        lambda *args, **_kwargs: commands.append(args)
        or subprocess.CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(
        controller,
        "_post_json",
        lambda url, **_kwargs: posts.append(url) or {"ok": True, "state": "ready"},
    )

    rc = controller._switch_to_rag()

    assert rc == 0
    assert commands == [("stop", controller.REVIEWER_UNIT)]
    assert posts == [controller.RAG_RESUME]
    assert states[0]["status"] == "switching"
    assert states[-1]["status"] == "ready"


def test_controller_does_not_restart_reviewer_when_rollback_suspend_fails(monkeypatch) -> None:
    controller = _load(CONTROLLER_PATH, "dem_gpu_mode_rollback_suspend_failure_test")
    commands: list[tuple[str, ...]] = []
    posts: list[str] = []
    states: list[dict[str, object]] = []
    monkeypatch.setattr(controller, "_mode", lambda: "reviewer")
    monkeypatch.setattr(controller, "_model_ready", lambda: True)
    monkeypatch.setattr(controller, "_wait_user_unit", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_wait_http", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_write_state", lambda **kwargs: states.append(kwargs))

    def fake_systemctl(*args: str, check: bool = False):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_post(url: str, **_kwargs):
        posts.append(url)
        if len(posts) == 1:
            raise RuntimeError("initial_resume_failed")
        if url == controller.RAG_SUSPEND:
            raise RuntimeError("rollback_suspend_failed")
        return {"ok": True, "state": "ready"}

    monkeypatch.setattr(controller, "_run_user_systemctl", fake_systemctl)
    monkeypatch.setattr(controller, "_post_json", fake_post)

    rc = controller._switch_to_rag()

    assert rc == 1
    assert commands == [("stop", controller.REVIEWER_UNIT)]
    assert posts == [controller.RAG_RESUME, controller.RAG_SUSPEND, controller.RAG_RESUME]
    assert states[-1]["status"] == "rolled_back"
    assert "rag_suspend_failed:rollback_suspend_failed" in states[-1]["error"]
    assert "recovered_to_rag" in states[-1]["error"]


def test_controller_does_not_restart_reviewer_until_rag_suspend_is_confirmed(monkeypatch) -> None:
    controller = _load(CONTROLLER_PATH, "dem_gpu_mode_rollback_suspend_state_test")
    commands: list[tuple[str, ...]] = []
    posts: list[str] = []
    states: list[dict[str, object]] = []
    monkeypatch.setattr(controller, "_mode", lambda: "reviewer")
    monkeypatch.setattr(controller, "_model_ready", lambda: True)
    monkeypatch.setattr(controller, "_wait_user_unit", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_wait_rag_state", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(controller, "_wait_http", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_write_state", lambda **kwargs: states.append(kwargs))

    def fake_systemctl(*args: str, check: bool = False):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_post(url: str, **_kwargs):
        posts.append(url)
        if len(posts) == 1:
            raise RuntimeError("initial_resume_failed")
        return {"ok": True, "state": "suspended" if url == controller.RAG_SUSPEND else "ready"}

    monkeypatch.setattr(controller, "_run_user_systemctl", fake_systemctl)
    monkeypatch.setattr(controller, "_post_json", fake_post)

    rc = controller._switch_to_rag()

    assert rc == 1
    assert commands == [("stop", controller.REVIEWER_UNIT)]
    assert posts == [controller.RAG_RESUME, controller.RAG_SUSPEND, controller.RAG_RESUME]
    assert states[-1]["status"] == "rolled_back"
    assert "rag_suspend_state_timeout" in states[-1]["error"]
    assert "recovered_to_rag" in states[-1]["error"]


def test_controller_stops_partial_reviewer_rollback_before_final_rag_recovery(monkeypatch) -> None:
    controller = _load(CONTROLLER_PATH, "dem_gpu_mode_partial_reviewer_rollback_test")
    commands: list[tuple[str, ...]] = []
    posts: list[str] = []
    states: list[dict[str, object]] = []
    monkeypatch.setattr(controller, "_mode", lambda: "reviewer")
    monkeypatch.setattr(controller, "_model_ready", lambda: True)
    monkeypatch.setattr(controller, "_wait_user_unit", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_wait_rag_state", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        controller,
        "_wait_http",
        lambda url, *_args, **_kwargs: url == controller.RAG_READY,
    )
    monkeypatch.setattr(controller, "_write_state", lambda **kwargs: states.append(kwargs))

    def fake_systemctl(*args: str, check: bool = False):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_post(url: str, **_kwargs):
        posts.append(url)
        if len(posts) == 1:
            raise RuntimeError("initial_resume_failed")
        return {"ok": True}

    monkeypatch.setattr(controller, "_run_user_systemctl", fake_systemctl)
    monkeypatch.setattr(controller, "_post_json", fake_post)

    rc = controller._switch_to_rag()

    assert rc == 1
    assert commands == [
        ("stop", controller.REVIEWER_UNIT),
        ("start", controller.REVIEWER_UNIT),
        ("stop", controller.REVIEWER_UNIT),
    ]
    assert posts == [controller.RAG_RESUME, controller.RAG_SUSPEND, controller.RAG_RESUME]
    assert states[-1]["status"] == "rolled_back"
    assert "reviewer_health_timeout" in states[-1]["error"]
    assert "recovered_to_rag" in states[-1]["error"]


def test_controller_rejects_reviewer_while_rag_source_run_is_inflight(monkeypatch) -> None:
    controller = _load(CONTROLLER_PATH, "dem_gpu_mode_inflight_test")
    states: list[dict[str, object]] = []
    commands: list[tuple[str, ...]] = []
    posts: list[str] = []
    monkeypatch.setattr(controller, "_model_ready", lambda: True)
    monkeypatch.setattr(controller, "_mode", lambda: "rag")
    monkeypatch.setattr(controller, "_maintenance_available", lambda: True)
    monkeypatch.setattr(
        controller,
        "_inflight_source_runs",
        lambda: [
            {
                "run_id": "run-live",
                "scope": "project:test",
                "source_id": "files",
                "state": "running",
                "active_slot": 1,
            }
        ],
    )
    monkeypatch.setattr(controller, "_active_rag_background_services", lambda: [])
    monkeypatch.setattr(controller, "_write_state", lambda **kwargs: states.append(kwargs))

    def fake_systemctl(*args: str, check: bool = False):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(controller, "_run_user_systemctl", fake_systemctl)
    monkeypatch.setattr(
        controller,
        "_post_json",
        lambda url, **_kwargs: posts.append(url) or {"ok": True},
    )

    rc = controller._switch_to_reviewer()

    assert rc == 2
    assert commands == []
    assert posts == []
    assert states[-1]["status"] == "rejected"
    assert states[-1]["error"] == "rag_background_work_inflight"
    assert states[-1]["details"]["source_runs"][0]["run_id"] == "run-live"


def test_controller_inflight_ledger_uses_only_canonical_nonterminal_states(
    monkeypatch,
    tmp_path: Path,
) -> None:
    controller = _load(CONTROLLER_PATH, "dem_gpu_mode_ledger_test")
    db_path = tmp_path / "source-runs.sqlite3"
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            """
            create table source_sync_runs (
                run_id text primary key,
                scope text not null,
                source_id text not null,
                state text not null,
                active_slot integer,
                created_at integer not null
            )
            """
        )
        con.executemany(
            "insert into source_sync_runs values (?,?,?,?,?,?)",
            [
                ("queued", "project:a", "files", "queued", None, 1),
                ("waiting", "project:b", "mail", "waiting_for_slot", None, 2),
                ("running", "project:c", "files", "running", 1, 3),
                ("partial", "project:d", "mail", "completed_with_errors", None, 4),
                ("done", "project:e", "files", "succeeded", None, 5),
            ],
        )
        con.commit()
    finally:
        con.close()
    monkeypatch.setattr(controller, "SOURCE_RUN_DB", db_path)

    rows = controller._inflight_source_runs()

    assert [row["run_id"] for row in rows] == ["queued", "waiting", "running"]
