from __future__ import annotations

import os
from pathlib import Path
import tempfile

import pytest


# The developer workstation may have a real .env / launchd environment for the
# live ChatGPT connector. Unit tests must start from deterministic defaults and
# should opt into auth modes explicitly via monkeypatch/module attributes.
for key in list(os.environ):
    if key.startswith("CHATGPT_MCP_"):
        os.environ.pop(key, None)

# Modules such as server.py create their registry during test collection, before
# per-test fixtures run. Give that collection-time registry a suite-private
# namespace so it can never be confused with production delegate metadata.
_SUITE_DELEGATE_STATE_DIR = Path(
    tempfile.mkdtemp(prefix="chatgpt-web-oauth-mcp-test-delegates-")
)
os.environ["CHATGPT_MCP_DELEGATE_STATE_DIR"] = str(_SUITE_DELEGATE_STATE_DIR)


@pytest.fixture(autouse=True)
def _isolate_delegate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from chatgpt_web_oauth_mcp import executors

    state_root = tmp_path / "delegate-state"
    monkeypatch.setattr(executors, "_default_delegate_state_root", lambda: state_root)
    yield
