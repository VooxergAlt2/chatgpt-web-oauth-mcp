from __future__ import annotations

import base64
import hashlib
import json

import pytest

from chatgpt_web_oauth_mcp import delegate_packets as packets


def proposal_source(text: object):
    return {
        "delegate_id": "proposal-id", "cwd": "/tmp/repo", "harness": "acp",
        "execution_backend": "acp", "work_role": "proposal", "status": "succeeded",
        "completed": True, "structured_output": {"status": "succeeded", "summary": text, "findings": [],
                                                  "blockers": [], "next_execution_prompt": None},
    }


def test_review_proposal_is_exact_bounded_and_unaccepted():
    submitted = " \n```diff\n+я\n```\n "
    packet = packets.build_delegate_packet(proposal_source(submitted), purpose="review",
                                           freshness={"state": "unknown"})
    proposal = packet["proposal"]
    encoded = submitted.encode("utf-8")
    assert proposal == {"available": True, "text": submitted,
                        "sha256": hashlib.sha256(encoded).hexdigest(),
                        "bytes": len(encoded), "accepted": False}
    assert packet["agent_claims"]["summary"] == ""
    assert packet["agent_claims"]["findings"] == []


def test_oversize_proposal_is_explicitly_unavailable():
    packet = packets.build_delegate_packet(proposal_source("я" * (packets.MAX_PROPOSAL_BYTES // 2 + 1)),
                                           purpose="review", freshness={"state": "unknown"})
    assert packet["proposal"] == {"available": False, "reason": "oversize", "accepted": False}
    assert packet["agent_claims"]["summary"] == ""


def test_invalid_proposal_and_non_proposal_are_not_exposed():
    packet = packets.build_delegate_packet(proposal_source(None), purpose="review",
                                           freshness={"state": "unknown"})
    assert packet["proposal"] == {"available": False, "reason": "invalid_text", "accepted": False}
    source = proposal_source("proposal text")
    source["work_role"] = "review"
    assert "proposal" not in packets.build_delegate_packet(source, purpose="review",
                                                            freshness={"state": "unknown"})


def test_handoff_does_not_inject_proposal_text():
    source = proposal_source("code proposal should stay out of prompt")
    packet = packets.build_delegate_packet(source, purpose="handoff",
                                           freshness={"state": "unknown"})
    assert "proposal" not in packet
    assert packet["agent_claims"]["summary"] == ""
    assert "code proposal should stay out of prompt" not in str(packet)


@pytest.mark.parametrize("field,value", [
    ("status", "failed"), ("completed", False), ("outcome_uncertain", True),
    ("harness_metadata", {"outcome_uncertain": True}), ("execution_backend", "cli"),
    ("error", {"code": "acp_tool_budget_exceeded"}),
])
def test_only_successful_certain_acp_run_can_deliver_source(field, value):
    source = proposal_source("def candidate(): pass")
    source[field] = value
    candidate = packets.build_delegate_packet(source)["proposal"]
    assert candidate == {"available": False, "accepted": False, "reason": "unsuccessful_run"}


@pytest.mark.parametrize("status", ["partial", "blocked"])
def test_incomplete_manifests_preserve_blocker_without_offering_source(status):
    source = proposal_source("contract lacks type")
    source["structured_output"].update(status=status, blockers=["Specify the return type"])
    packet = packets.build_delegate_packet(source)
    assert packet["proposal"]["available"] is False and packet["proposal"]["reason"] == "incomplete_manifest"
    assert packet["agent_claims"]["blockers"] == ["Specify the return type"]


def test_candidate_recovery_requires_the_original_private_artifact(tmp_path):
    from chatgpt_web_oauth_mcp.executors import ExecutorRegistry

    source = proposal_source("def f():\n    return 3\n")
    source["delegate_id"] = "c" * 12
    state = tmp_path / "state"
    log = state / "record"
    log.mkdir(parents=True)
    structured = source.pop("structured_output")
    raw = json.dumps({"delegate_id": source["delegate_id"], "structured_output": structured}).encode()
    artifact = log / "structured-result.json"
    artifact.write_bytes(raw)
    artifact.chmod(0o600)
    source["structured_result_ref"] = {"name": artifact.name, "bytes": len(raw),
                                       "sha256": hashlib.sha256(raw).hexdigest()}
    (log / "metadata.json").write_text(json.dumps(source))
    registry = ExecutorRegistry(codex_command="true", delegate_state_root=state)
    registry.recover_persisted_delegates(roots=[state])
    def candidate():
        return packets.build_delegate_packet(registry.delegate_evidence_snapshot(source["delegate_id"]))["proposal"]
    assert candidate()["text"] == structured["summary"]
    artifact.write_bytes(raw.replace(b"return 3", b"return 4"))
    assert candidate() == {"available": False, "accepted": False, "reason": "artifact_unavailable"}


def test_maximum_multibyte_source_roundtrips_and_fits_budget():
    value = "😀" * (packets.MAX_PROPOSAL_BYTES // 4)
    source = proposal_source(value)
    packet = packets.build_delegate_packet(source)
    candidate = packet["proposal"]
    assert base64.b64decode(candidate["text_base64"]).decode("utf-8") == value
    assert candidate["bytes"] == 8192 and packets._size(packet) <= packets.MAX_PACKET_BYTES
    assert value[:8] not in str(packets.build_delegate_packet(source, purpose="triage"))
