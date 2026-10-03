from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = ROOT / "scripts" / "dem_gpu_mode_control.py"
INSTALLER_PATH = ROOT / "scripts" / "install_devstral_gpu_mode.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_devstral_unit_is_single_slot_q5_profile() -> None:
    installer = _load(INSTALLER_PATH, "install_devstral_gpu_mode_test")
    unit = installer._llama_unit("vooxerg")

    assert "llama-server" in unit
    assert str(installer.MODEL_PATH) in unit
    assert "--port 8083" in unit
    assert "--alias devstral-gip" in unit
    assert "-c 32768" in unit
    assert "-ctk q8_0" in unit
    assert "-ctv q8_0" in unit
    assert "-np 1" in unit
    assert "Conflicts=rag-gpu-inference.service llama-qwen35-gip.service" in unit


def test_gpu_mode_path_unit_watches_only_bounded_request_file() -> None:
    installer = _load(INSTALLER_PATH, "install_devstral_gpu_mode_path_test")
    unit = installer._path_unit()

    assert f"PathChanged={installer.REQUEST_PATH}" in unit
    assert "Unit=dem-gpu-mode.service" in unit


def test_gpu_mode_control_unit_reads_canonical_rag_storage_env() -> None:
    installer = _load(INSTALLER_PATH, "install_devstral_gpu_mode_control_unit_test")
    unit = installer._control_unit()

    assert "EnvironmentFile=-/etc/rag-safe-wrapper-storage.env" in unit
    assert f"ExecStart={installer.CONTROLLER_PATH} apply" in unit


def test_installer_prepends_local2_without_duplicating_existing_fallbacks(tmp_path: Path) -> None:
    installer = _load(INSTALLER_PATH, "install_devstral_gpu_mode_env_test")
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


def test_controller_rejects_devstral_when_model_is_not_ready(monkeypatch) -> None:
    controller = _load(CONTROLLER_PATH, "dem_gpu_mode_missing_model_test")
    states: list[dict[str, object]] = []
    monkeypatch.setattr(controller, "_model_ready", lambda: False)
    monkeypatch.setattr(
        controller,
        "_write_state",
        lambda **kwargs: states.append(kwargs),
    )

    rc = controller._switch_to_devstral()

    assert rc == 2
    assert states[-1]["status"] == "rejected"
    assert states[-1]["error"] == "devstral_model_missing_or_wrong_size"


def test_controller_switches_rag_to_devstral_in_fixed_order(monkeypatch) -> None:
    controller = _load(CONTROLLER_PATH, "dem_gpu_mode_switch_test")
    commands: list[tuple[str, ...]] = []
    states: list[dict[str, object]] = []
    monkeypatch.setattr(controller, "_model_ready", lambda: True)
    monkeypatch.setattr(controller, "_mode", lambda: "rag")
    monkeypatch.setattr(controller, "_wait_unit", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_wait_http", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_pause_rag_background_timers", lambda: [])
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

    monkeypatch.setattr(controller, "_run_systemctl", fake_systemctl)

    rc = controller._switch_to_devstral()

    assert rc == 0
    assert commands[:3] == [
        ("stop", controller.LEGACY_QWEN_UNIT),
        ("stop", controller.RAG_UNIT),
        ("start", controller.DEVSTRAL_UNIT),
    ]
    assert states[0]["status"] == "switching"
    assert states[-1]["status"] == "ready"


def test_controller_rejects_devstral_while_rag_source_run_is_inflight(monkeypatch) -> None:
    controller = _load(CONTROLLER_PATH, "dem_gpu_mode_inflight_test")
    states: list[dict[str, object]] = []
    restored: list[bool] = []
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(controller, "_model_ready", lambda: True)
    monkeypatch.setattr(controller, "_mode", lambda: "rag")
    monkeypatch.setattr(controller, "_pause_rag_background_timers", lambda: ["rag-project-autoindex.timer"])
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
    monkeypatch.setattr(
        controller,
        "_restore_rag_background_timers",
        lambda: restored.append(True) or [],
    )
    monkeypatch.setattr(controller, "_write_state", lambda **kwargs: states.append(kwargs))

    def fake_systemctl(*args: str, check: bool = False):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(controller, "_run_systemctl", fake_systemctl)

    rc = controller._switch_to_devstral()

    assert rc == 2
    assert restored == [True]
    assert commands == []
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
