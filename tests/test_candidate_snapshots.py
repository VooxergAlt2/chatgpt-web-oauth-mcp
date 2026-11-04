"""Private draft capture/restore without treating saved bytes as accepted code."""
import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.acp_workspace import AcpWorkspace
from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError
from chatgpt_web_oauth_mcp.candidate_snapshots import CandidateSnapshotStore
from chatgpt_web_oauth_mcp.supervision import (
    AgentEndpoint, SupervisorBinding, SupervisorChannel, SupervisorStore, SupervisionError,
)
from test_acp_workspace import repository, git  # reuse the linked-worktree fixture


def context(repository, *, paths=None):
    _, source, state = repository
    scope = {"files_in_scope": paths or ["source.txt"], "out_of_scope": []}
    binding = SupervisorBinding("a" * 64, "writer-task", "b" * 32,
        AgentEndpoint("google", "acp", "account-4", "flash", "high"), str(source), "c" * 64)
    return CandidateSnapshotStore(state / "drafts"), binding, scope


def capture(store, binding, scope, writer):
    return store.capture(binding, export=lambda path: writer.capture_draft(path, scope=scope))


def test_delta_survives_worker_copy_cleanup_and_requires_explicit_promotion(repository):
    _, source, state = repository
    store, binding, scope = context(repository, paths=["source.txt", "nested/other.txt", "new.txt"])
    with AcpWorkspace.prepare(source, scope["files_in_scope"], state / "workspaces") as writer:
        (writer.root / "source.txt").write_text("changed\n")
        (writer.root / "nested/other.txt").unlink()
        (writer.root / "new.txt").write_text("new\n")
        receipt = capture(store, binding, scope, writer)
        assert capture(store, binding, scope, writer) == receipt  # no duplicate artifact
        assert not receipt["accepted"] and receipt["changed_file_count"] == 3
        assert receipt["stored_bytes"] == len(b"changed\nnew\n")
    assert (source / "source.txt").read_text() == "before\n"
    assert not (source / "new.txt").exists()
    reopened = CandidateSnapshotStore(state / "drafts")
    with AcpWorkspace.prepare(source, scope["files_in_scope"], state / "workspaces") as writer:
        reopened.restore(binding, receipt["snapshot_id"], apply=lambda p, m: writer.restore_draft(p, m, scope=scope))
        assert (writer.root / "source.txt").read_text() == "changed\n"
        assert not (writer.root / "nested/other.txt").exists()
        assert writer.snapshot_digest() == receipt["candidate_digest"]
        assert (source / "source.txt").read_text() == "before\n"
        # Only this explicit acceptance operation changes the source.
        writer.promote(expected_digest=receipt["candidate_digest"])
    assert (source / "new.txt").read_text() == "new\n"
    assert reopened.discard(binding, receipt["snapshot_id"])["discarded"]
    assert reopened.discard(binding, receipt["snapshot_id"])["already_discarded"]


@pytest.mark.parametrize("change", ["account", "scope", "baseline", "bytes", "symlink"])
def test_draft_restore_refuses_changed_identity_or_bytes(repository, change):
    _, source, state = repository
    store, binding, scope = context(repository)
    with AcpWorkspace.prepare(source, scope["files_in_scope"], state / "workspaces") as writer:
        (writer.root / "source.txt").write_text("draft\n")
        receipt = capture(store, binding, scope, writer)
    if change == "account":
        binding = replace(binding, endpoint=replace(binding.endpoint, account_id="other"))
    elif change == "scope":
        scope = {**scope, "out_of_scope": ["source.txt"]}
    elif change == "baseline":
        (source / "source.txt").write_text("new baseline\n")
        git(source, "add", ".")
        git(source, "commit", "-qm", "changed baseline")
    else:
        saved = store.root / binding.key / receipt["snapshot_id"] / "files/source.txt"
        if change == "bytes":
            saved.write_text("tampered\n")
        else:
            saved.unlink()
            saved.symlink_to(source / "source.txt")
    with AcpWorkspace.prepare(source, scope["files_in_scope"], state / "workspaces") as writer:
        with pytest.raises((AcpStateError, SupervisionError)):
            store.restore(binding, receipt["snapshot_id"], apply=lambda p, m: writer.restore_draft(p, m, scope=scope))
    assert git(source, "status", "--porcelain") == ""


def test_published_snapshot_survives_sigkill_of_its_creator(repository):
    _, source, state = repository
    store, binding, scope = context(repository)
    child = '''
import json, os, signal, sys
from pathlib import Path
from chatgpt_web_oauth_mcp.acp_workspace import AcpWorkspace
from chatgpt_web_oauth_mcp.candidate_snapshots import CandidateSnapshotStore
from chatgpt_web_oauth_mcp.supervision import SupervisorBinding, AgentEndpoint
args=json.loads(sys.stdin.read())
b=args['binding']; b['endpoint']=AgentEndpoint(**b['endpoint']); binding=SupervisorBinding(**b)
workspace=AcpWorkspace.prepare(Path(binding.workspace), args['scope']['files_in_scope'], Path(args['state'])/'workspaces')
(workspace.root/'source.txt').write_text('survives-kill\\n')
store=CandidateSnapshotStore(Path(args['state'])/'drafts')
receipt=store.capture(binding, export=lambda p: workspace.capture_draft(p, scope=args['scope']))
print(json.dumps(receipt),flush=True)
os.kill(os.getpid(),signal.SIGKILL)
'''
    result = subprocess.run([sys.executable, "-B", "-c", child], input=json.dumps({
        "binding": asdict(binding), "scope": scope, "state": str(state)}), capture_output=True, text=True, timeout=10)
    assert result.returncode == -signal.SIGKILL, result.stderr
    receipt = json.loads(result.stdout)
    assert store.list(binding) == [receipt]
    with AcpWorkspace.prepare(source, scope["files_in_scope"], state / "workspaces") as writer:
        store.restore(binding, receipt["snapshot_id"], apply=lambda p, m: writer.restore_draft(p, m, scope=scope))
        assert (writer.root / "source.txt").read_text() == "survives-kill\n"
    assert (source / "source.txt").read_text() == "before\n"


def test_suspend_requires_retained_draft_and_leaves_it_after_cancellation(repository):
    _, source, state = repository
    drafts, binding, scope = context(repository)
    with AcpWorkspace.prepare(source, scope["files_in_scope"], state / "workspaces") as writer:
        (writer.root / "source.txt").write_text("paused draft\n")
        supervisor = SupervisorStore(state / "supervision")
        supervisor.open(binding, expires_at=time.time() + 10)
        receipt = None
        def save():
            nonlocal receipt
            receipt = capture(drafts, binding, scope, writer)
        async def run():
            cancel = asyncio.Event()
            channel = SupervisorChannel(supervisor, binding, deadline=time.monotonic() + 5, cancel=cancel,
                observe=lambda: {"retained_draft": receipt, "workspace_digest": writer.snapshot_digest()}, capture=save)
            task = asyncio.create_task(channel.checkpoint(phase="implementation", summary="Draft saved"))
            await asyncio.sleep(0)
            view = supervisor.snapshot(binding)
            assert view["checkpoint"]["observations"]["retained_draft"] == receipt
            supervisor.command(binding, action="suspend", command_id="pause-1", expected_revision=view["revision"],
                               checkpoint_id=view["checkpoint"]["id"])
            with pytest.raises(SupervisionError, match="supervisor_suspended"):
                await task
            assert cancel.is_set()
        asyncio.run(run())
    assert drafts.list(binding) == [receipt]
    assert (source / "source.txt").read_text() == "before\n"


def test_storage_limit_does_not_remove_previous_snapshot(repository, monkeypatch):
    import chatgpt_web_oauth_mcp.candidate_snapshots as module
    _, source, state = repository
    store, binding, scope = context(repository)
    with AcpWorkspace.prepare(source, scope["files_in_scope"], state / "workspaces") as writer:
        (writer.root / "source.txt").write_text("first\n")
        receipt = capture(store, binding, scope, writer)
        monkeypatch.setattr(module, "MAX_SNAPSHOTS", 1)
        (writer.root / "source.txt").write_text("second\n")
        with pytest.raises(SupervisionError, match="draft_retention_limit"):
            capture(store, binding, scope, writer)
    assert store.list(binding) == [receipt]


@pytest.mark.parametrize("damage", [
    "missing_baseline", "missing_candidate", "missing_scope", "missing_created_at",
    "not_a_changes_mapping", "bad_change_size", "bad_change_mode", "bad_change_digest",
    "stored_bytes_mismatch", "bad_json", "bad_payload_checksum",
])
def test_malformed_published_draft_fails_closed_without_raw_exceptions(repository, damage):
    """A syntactically valid, correctly checksummed manifest can still be corrupt."""
    _, source, state = repository
    store, binding, scope = context(repository)
    with AcpWorkspace.prepare(source, scope["files_in_scope"], state / "workspaces") as writer:
        (writer.root / "source.txt").write_text("draft\n")
        receipt = capture(store, binding, scope, writer)
    path = store.root / binding.key / receipt["snapshot_id"] / "manifest.json"
    record = json.loads(path.read_text())
    payload = record["payload"]
    if damage == "missing_baseline":
        del payload["baseline_head"]
    elif damage == "missing_candidate":
        del payload["candidate_digest"]
    elif damage == "missing_scope":
        del payload["scope"]
    elif damage == "missing_created_at":
        del record["created_at"]
    elif damage == "not_a_changes_mapping":
        payload["changes"] = ["source.txt"]
    elif damage == "bad_change_size":
        payload["changes"]["source.txt"]["size"] = -3
    elif damage == "bad_change_mode":
        payload["changes"]["source.txt"]["mode"] = "644"
    elif damage == "bad_change_digest":
        payload["changes"]["source.txt"]["sha256"] = None
    elif damage == "stored_bytes_mismatch":
        record["stored_bytes"] += 1
    elif damage == "bad_json":
        path.write_text("{")
    elif damage == "bad_payload_checksum":
        record["payload_sha256"] = "0" * 64
    if damage not in {"bad_json", "bad_payload_checksum"}:
        record["payload_sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
        ).hexdigest()
    if damage != "bad_json":
        path.write_text(json.dumps(record))
    reason = "draft_manifest_changed" if damage == "bad_payload_checksum" else "draft_manifest_invalid"
    for action in (
        lambda: store.list(binding),
        lambda: store.restore(binding, receipt["snapshot_id"], apply=lambda *_: pytest.fail("unsafe restore")),
        lambda: store.discard(binding, receipt["snapshot_id"]),
    ):
        with pytest.raises(SupervisionError, match=reason):
            action()
    assert (source / "source.txt").read_text() == "before\n"
