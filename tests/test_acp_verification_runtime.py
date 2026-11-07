"""Immutable profile identity and regular-file snapshot boundaries."""
from copy import deepcopy
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError
from chatgpt_web_oauth_mcp.acp_verification_runtime import validate_runtime_identity


def identity(root="/nonexistent/runtime"):
    return {"id": "pytest-v1", "root": root, "manifest_sha256": "a" * 64, "python": "bin/python"}


def test_identity_validation_is_historical_and_does_not_touch_files(monkeypatch):
    value = identity()
    original = deepcopy(value)
    def unexpected(*args, **kwargs):
        raise AssertionError("Identity validation must not inspect current files")
    monkeypatch.setattr(Path, "resolve", unexpected)
    monkeypatch.setattr(Path, "stat", unexpected)
    result = validate_runtime_identity(value)
    assert result == value and result is not value and value == original


@pytest.mark.parametrize("change", [
    {"id": ""}, {"id": "../runtime"}, {"id": "x" * 65},
    {"root": "/"}, {"root": "relative/runtime"}, {"root": "/tmp/../runtime"},
    {"root": "/tmp/runtime/"}, {"root": "/tmp/runtime\0"}, {"root": "//tmp/runtime"},
    {"root": "/tmp/\ud800"}, {"root": "/" + "x" * 4096},
    {"manifest_sha256": "A" * 64}, {"python": "/usr/bin/python3"}, {"extra": 1},
])
def test_invalid_runtime_identity_rejected(change):
    with pytest.raises(AcpStateError, match="acp_verification_runtime_invalid"):
        validate_runtime_identity({**identity(), **change})
