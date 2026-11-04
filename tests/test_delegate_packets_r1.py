from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp import delegate_packets as packets
from chatgpt_web_oauth_mcp import server, session_continuation


def source(tmp_path):
    return {"delegate_id": "a" * 12, "cwd": str(tmp_path), "harness": "antigravity3",
            "execution_backend": "acp", "status": "succeeded", "completed": True,
            "harness_metadata": {"owner_digest": hashlib.sha256(b"owner").hexdigest()},
            "acp_scope": {"files_in_scope": ["a.py"], "out_of_scope": []},
            "structured_output": {"summary": "claim"},
            "code_verification": {"verified": True, "baseline": {"head": "head"},
                "post_verification_result_fingerprint": "result",
                "verification_commands": [{"command": "pytest", "passed": True, "exit_code": 0}]}}


def test_packet_preserves_historical_pass_but_unknown_environment_prevents_reuse(tmp_path, monkeypatch):
    s = source(tmp_path)
    monkeypatch.setattr(packets, "capture_source_snapshot",
                        lambda *a, **k: {"digest": "result", "head": "head"})
    p = packets.build_delegate_packet(s)
    assert p["server_observations"]["source_freshness"]["state"] == "current"
    assert p["server_observations"]["verification"]["historical_verified"] is True
    assert p["agent_claims"]["trust"] == "unverified"
    assert p["reuse_eligible"] is False
    assert p["server_observations"]["review_evidence"]["available"] is False


@pytest.mark.parametrize("current", [
    {"digest": "drift", "head": "head"}, {"digest": "result", "head": "new-head"}])
def test_source_or_head_drift_invalidates_freshness(tmp_path, monkeypatch, current):
    monkeypatch.setattr(packets, "capture_source_snapshot", lambda *a, **k: current)
    p = packets.build_delegate_packet(source(tmp_path))
    assert p["server_observations"]["source_freshness"]["state"] == "stale"
    assert p["server_observations"]["verification"]["checks"][0]["passed"] is True


def test_missing_source_and_legacy_receipt_fail_closed(tmp_path, monkeypatch):
    def absent(*a, **k):
        raise OSError("absent")
    monkeypatch.setattr(packets, "capture_source_snapshot", absent)
    assert packets.source_freshness(source(tmp_path))["state"] == "unknown"
    s = source(tmp_path)
    s["execution_backend"] = "cli"
    assert packets.source_freshness(s)["reason"] == "no_verified_acp_source_receipt"


def test_packet_bound_preserves_json_and_marks_omissions(tmp_path):
    s = source(tmp_path)
    s["structured_output"] = {"summary": "я" * 100000, "findings": ["я" * 10000] * 30}
    s["code_verification"]["changed_files"] = ["я" * 1000] * 100
    s["code_verification"]["review_evidence"] = {"diff": "я" * 100000, "truncated": True}
    s["acp_scope"]["files_in_scope"] = ["я" * 1000] * 100
    p = packets.build_delegate_packet(s, freshness={"state": "unknown"})
    assert len(json.dumps(p, ensure_ascii=True, sort_keys=True).encode()) <= packets.MAX_PACKET_BYTES
    assert p["truncated"] is True
    assert p["reuse_eligible"] is False


def test_handoff_binding_and_current_task_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(packets, "capture_source_snapshot",
                        lambda *a, **k: {"digest": "result", "head": "head"})
    s = source(tmp_path)
    kwargs = dict(owner="owner", harness="antigravity3", cwd=tmp_path,
                  files_in_scope=["a.py"], out_of_scope=[])
    h = packets.handoff_context(s, **kwargs)
    assert "current task and scope take precedence" in h
    assert '"trust": "unverified"' in h
    for key, value in [("owner", "foreign"), ("harness", "antigravity"),
                       ("cwd", tmp_path / "elsewhere"), ("files_in_scope", []),
                       ("out_of_scope", ["a.py"])]:
        assert packets.handoff_context(s, **{**kwargs, key: value}) == ""


@pytest.mark.parametrize("ownership", ["unscoped", "unowned", "owned_elsewhere", "conflict"])
def test_packet_tool_denies_before_reading_source(monkeypatch, ownership):
    from chatgpt_web_oauth_mcp import tools_delegate
    monkeypatch.setattr(tools_delegate, "result_access_scope", lambda *a, **k: ownership)
    class Registry:
        def delegate_evidence_snapshot(self, delegate_id):
            pytest.fail("denied caller must not read source")
    monkeypatch.setattr(server, "registry", Registry())
    fn = getattr(server.delegate_packet, "fn", server.delegate_packet)
    assert fn(delegate_id="a" * 12)["success"] is False


def test_packet_tool_owned_snapshot(monkeypatch, tmp_path):
    from chatgpt_web_oauth_mcp import tools_delegate
    monkeypatch.setattr(tools_delegate, "result_access_scope", lambda *a, **k: "owned_here")
    monkeypatch.setattr(packets, "capture_source_snapshot",
                        lambda *a, **k: {"digest": "result", "head": "head"})
    class Registry:
        def delegate_evidence_snapshot(self, delegate_id):
            return source(tmp_path)
    monkeypatch.setattr(server, "registry", Registry())
    fn = getattr(server.delegate_packet, "fn", server.delegate_packet)
    assert fn(delegate_id="a" * 12)["packet"]["delegate_id"] == "a" * 12


def artifact_source(tmp_path, *, delegate_id=None):
    s = source(tmp_path)
    output = s.pop('structured_output')
    data = json.dumps({'delegate_id': delegate_id or s['delegate_id'], 'structured_output': output}).encode()
    artifact = tmp_path / 'structured-result.json'
    artifact.write_bytes(data)
    artifact.chmod(0o600)
    s['structured_result_ref'] = {'name': artifact.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    return s, artifact


def test_packet_claims_survive_real_registry_recovery(tmp_path):
    from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
    state = tmp_path / 'state'
    log = state / 'record'
    log.mkdir(parents=True)
    s, artifact = artifact_source(log)
    metadata = log / 'metadata.json'
    metadata.write_text(json.dumps(s))
    r = ExecutorRegistry(codex_command='true', delegate_state_root=state)
    r.recover_persisted_delegates(roots=[state])
    recovered = r.delegate_evidence_snapshot(s['delegate_id'])
    assert recovered['structured_output'] == {'summary': 'claim'}
    assert 'structured_output' not in r._resume_source_snapshot(s['delegate_id'])


@pytest.mark.parametrize('fault', ['digest', 'foreign', 'symlink', 'public', 'oversized', 'path'])
def test_claim_artifact_cannot_substitute_or_escape(tmp_path, fault):
    s, artifact = artifact_source(tmp_path, delegate_id='foreign' if fault == 'foreign' else None)
    if fault == 'digest':
        s['structured_result_ref']['sha256'] = '0' * 64
    elif fault == 'symlink':
        target = tmp_path / 'target'
        artifact.rename(target)
        artifact.symlink_to(target)
    elif fault == 'public':
        artifact.chmod(0o644)
    elif fault == 'oversized':
        s['structured_result_ref']['bytes'] = packets.MAX_CLAIM_ARTIFACT_BYTES + 1
    elif fault == 'path':
        s['structured_result_ref']['name'] = '../structured-result.json'
    recovered = packets.restore_packet_claims(s, tmp_path / 'metadata.json')
    assert 'structured_output' not in recovered
