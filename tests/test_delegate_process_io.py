from __future__ import annotations

import json
from pathlib import Path
import stat

from chatgpt_web_oauth_mcp.delegate_process import (
    write_private_json,
    write_private_text,
)


def test_write_private_text_replaces_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    path = tmp_path / "prompt.txt"
    path.symlink_to(target)

    write_private_text(path, "secret prompt")

    assert not path.is_symlink()
    assert path.read_text(encoding="utf-8") == "secret prompt"
    assert target.read_text(encoding="utf-8") == "outside"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_private_json_is_atomic_private_and_valid(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"

    write_private_json(path, {"status": "running", "count": 2})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "count": 2,
        "status": "running",
    }
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
