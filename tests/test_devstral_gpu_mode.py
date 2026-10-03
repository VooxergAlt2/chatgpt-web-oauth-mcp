from __future__ import annotations

import importlib.util
from pathlib import Path
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
