from pathlib import Path
from types import SimpleNamespace

import pytest

from chatgpt_web_oauth_mcp import delegate_harnesses as harnesses
from chatgpt_web_oauth_mcp.tools_delegate import register_delegate_tools


def catalog_tool(adapters, aggregate=None):
    class MCP:
        def tool(self, **kwargs):
            return lambda fn: fn

    registry = SimpleNamespace(
        harnesses=adapters, harness_info=aggregate or (lambda: {}),
        routing_guidance=aggregate or (lambda: {}), runtime_info=aggregate or (lambda: {}),
    )
    ctx = SimpleNamespace(registry=registry, delegate_default_harness="codex")
    return register_delegate_tools(MCP(), ctx)["delegate_harnesses"]


def test_zero_argument_does_not_probe(monkeypatch):
    def forbidden(*args):
        pytest.fail("unexpected catalog probe")
    monkeypatch.setattr(harnesses, "_agy_models_probe", forbidden)
    tool = catalog_tool({"antigravity": harnesses.AntigravityHarness("agy")})
    assert tool() == {
        "success": True, "default_harness": "codex", "harnesses": {},
        "routing": {}, "runtime": {},
    }


@pytest.mark.parametrize("account", ["antigravity2", "antigravity3"])
def test_unbound_secondary_account_never_probes_ambient(monkeypatch, account):
    def forbidden(*args):
        pytest.fail("secondary account fell back to ambient credentials")
    monkeypatch.setattr(harnesses, "_agy_models_probe", forbidden)
    adapter = harnesses.AntigravityHarness("agy", name=account)
    assert catalog_tool({account: adapter})(agy_account=account)["account_model_catalog"] == {
        "account": account, "status": "unavailable", "models": None,
    }
    assert adapter.model_admission("claude-opus-5-5-medium") == (True, "model_catalog_unverified")


@pytest.mark.parametrize("account", ["antigravity", "antigravity2", "antigravity3"])
def test_catalog_and_admission_share_only_selected_account(monkeypatch, tmp_path, account):
    calls = []
    models = {"antigravity": "claude-opus-4-6-thinking",
              "antigravity2": "claude-opus-5-5-medium",
              "antigravity3": "claude-opus-4-6-thinking"}

    def probe(command, account_home, interval):
        calls.append((command, account_home, interval))
        return frozenset([models[Path(account_home).name]])

    monkeypatch.setattr(harnesses, "_agy_models_probe", probe)
    adapters = {name: harnesses.AntigravityHarness("agy", name=name, home_dir=tmp_path / name)
                for name in models}
    def forbidden_aggregate():
        pytest.fail("targeted catalog queried aggregate harness information")
    payload = catalog_tool(adapters, forbidden_aggregate)(agy_account=account)
    assert set(payload) == {"success", "account_model_catalog"}
    result = payload["account_model_catalog"]
    assert result == {"account": account, "status": "verified", "models": [models[account]]}
    assert adapters[account].model_admission(models[account]) == (True, "model_confirmed")
    assert adapters[account].model_admission("claude-unlisted") == (False, "model_not_available_in_account")
    assert len(calls) == 3
    assert {call[1] for call in calls} == {str(tmp_path / account)}


@pytest.mark.parametrize("present", [False, True])
def test_missing_or_unverified_catalog_is_explicit(monkeypatch, present):
    calls = []
    def failed_probe(*args):
        calls.append(args)
        return None
    monkeypatch.setattr(harnesses, "_agy_models_probe", failed_probe)
    adapters = {"antigravity2": harnesses.AntigravityHarness("agy", home_dir=Path("/isolated/agy2"))} if present else {}
    assert catalog_tool(adapters)(agy_account="antigravity2")["account_model_catalog"] == {
        "account": "antigravity2", "status": "unavailable", "models": None,
    }
    assert len(calls) == int(present)
    if present:
        assert adapters["antigravity2"].model_admission("claude-opus-5-5-medium") == (True, "model_catalog_unverified")
