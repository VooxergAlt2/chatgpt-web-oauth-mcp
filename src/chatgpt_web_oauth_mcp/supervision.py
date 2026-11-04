"""Provider-neutral, durable supervision of one owned execution.

Adapters supply a trusted binding and observations. Models can publish reports,
but cannot select accounts, change permissions or authorize their own decisions.
An issued decision is not proof of provider delivery or acceptance of code.
"""
from __future__ import annotations

import asyncio
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .state_io import atomic_write_bytes

CHECKPOINT_TOOL = "supervisor_checkpoint"
PHASES = ("research", "plan", "implementation", "verification", "review")
DECISIONS = ("continue", "steer", "review", "escalate", "stop")
_WAITING = {"paused_for_supervisor", "review_requested", "escalated"}
_STATES = _WAITING | {"running", "checkpoint_requested", "decision_ready", "closed"}
_MAX_STATE_BYTES = 262144


class SupervisionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise SupervisionError("supervisor_invalid_identifier")
    return value


def _text(value: str, limit: int = 8192, *, empty: bool = False) -> str:
    if (not isinstance(value, str) or (not empty and not value.strip())
            or "\x00" in value or len(value.encode("utf-8")) > limit):
        raise SupervisionError("supervisor_invalid_text")
    return value


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class AgentEndpoint:
    """Identity, not credentials. Account and transport are independent axes.

    No provider, model or account-name allowlist lives in the supervision core.
    Execution adapters still validate configured credentials and capabilities.
    """

    provider: str
    transport: str
    account_id: str
    model: str
    reasoning: str

    def __post_init__(self):
        for value in (self.provider, self.transport, self.account_id):
            _identifier(value)
        _text(self.model, 256)
        _text(self.reasoning, 128)


@dataclass(frozen=True)
class SupervisorBinding:
    owner: str
    execution_id: str
    session_ref: str
    endpoint: AgentEndpoint
    workspace: str
    scope_digest: str

    def __post_init__(self):
        for value in (self.owner, self.scope_digest):
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise SupervisionError("supervisor_invalid_binding")
        _identifier(self.execution_id)
        _text(self.session_ref, 256)
        if not isinstance(self.endpoint, AgentEndpoint) or not Path(self.workspace).is_absolute():
            raise SupervisionError("supervisor_invalid_binding")

    @property
    def key(self) -> str:
        # Reusing an execution with another endpoint must hit the binding check,
        # not silently create a second record under a new account or scope.
        return hashlib.sha256((self.owner + "\0" + self.execution_id).encode()).hexdigest()


class SupervisorStore:
    """Short OS transactions shared by workers and controller/API adapters."""

    def __init__(self, root: Path):
        self.root = Path(root)
        if self.root.is_symlink():
            raise SupervisionError("supervisor_state_not_private")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SupervisionError("supervisor_state_not_private")

    @contextmanager
    def _lease(self, binding: SupervisorBinding):
        fd = os.open(self.root / (binding.key + ".lock"),
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise SupervisionError("supervisor_state_not_private")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self, binding: SupervisorBinding) -> dict:
        try:
            fd = os.open(self.root / (binding.key + ".json"),
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except FileNotFoundError:
            raise SupervisionError("supervisor_not_started") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise SupervisionError("supervisor_state_not_private")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(_MAX_STATE_BYTES + 1)
            if len(raw) > _MAX_STATE_BYTES:
                raise SupervisionError("supervisor_state_too_large")
            state = json.loads(raw)
        finally:
            os.close(fd)
        if (not isinstance(state, dict) or state.get("version") != 1
                or state.get("state") not in _STATES or type(state.get("revision")) is not int
                or state["revision"] < 0 or not isinstance(state.get("commands"), dict)
                or type(state.get("expires_at")) not in {int, float}
                or not math.isfinite(state["expires_at"])):
            raise SupervisionError("supervisor_state_invalid")
        if state.get("binding") != asdict(binding):
            raise SupervisionError("supervisor_binding_mismatch")
        return state

    def _save(self, binding, state):
        raw = _json(state)
        if len(raw) > _MAX_STATE_BYTES:
            raise SupervisionError("supervisor_state_too_large")
        path = self.root / (binding.key + ".json")
        if path.is_symlink():
            raise SupervisionError("supervisor_state_not_private")
        atomic_write_bytes(path, raw, sync_directory=True)

    @staticmethod
    def _view(state):
        result = copy.deepcopy({k: v for k, v in state.items() if k != "commands"})
        result["available"] = True
        result["expired"] = time.time() >= state["expires_at"]
        result["decision_required"] = state["state"] in _WAITING and not result["expired"]
        result["delivery_mode"] = "cooperative_tool_checkpoint"
        return result

    @staticmethod
    def _live(state):
        if state["state"] == "closed" or time.time() >= state["expires_at"]:
            raise SupervisionError("supervisor_execution_closed")

    def open(self, binding: SupervisorBinding, *, expires_at: float) -> dict:
        if type(expires_at) not in {int, float} or not math.isfinite(expires_at) or expires_at <= time.time():
            raise SupervisionError("supervisor_invalid_deadline")
        with self._lease(binding):
            try:
                self._load(binding)
            except SupervisionError as exc:
                if exc.code != "supervisor_not_started":
                    raise
            else:
                raise SupervisionError("supervisor_execution_already_started")
            state = {"version": 1, "binding": asdict(binding), "revision": 0,
                     "state": "running", "expires_at": expires_at, "checkpoint": None,
                     "request": None, "decision": None, "commands": {}}
            self._save(binding, state)
            return self._view(state)

    def snapshot(self, binding: SupervisorBinding) -> dict:
        with self._lease(binding):
            return self._view(self._load(binding))

    def publish(self, binding: SupervisorBinding, *, report: dict, observations: dict) -> dict:
        if (not isinstance(report, dict) or set(report) != {"phase", "summary", "evidence"}
                or report["phase"] not in PHASES or not isinstance(report["evidence"], list)
                or len(report["evidence"]) > 24):
            raise SupervisionError("supervisor_invalid_report")
        _text(report["summary"])
        for evidence in report["evidence"]:
            _text(evidence, 1024)
        if not isinstance(observations, dict) or len(_json(observations)) > 16384:
            raise SupervisionError("supervisor_invalid_observations")
        with self._lease(binding):
            state = self._load(binding)
            self._live(state)
            if state["state"] not in {"running", "checkpoint_requested"}:
                raise SupervisionError("supervisor_checkpoint_pending")
            state["revision"] += 1
            state["state"] = "paused_for_supervisor"
            state["checkpoint"] = {
                "id": secrets.token_hex(16), "created_at": time.time(),
                "agent_report": copy.deepcopy(report), "report_trust": "unverified_agent_claims",
                "observations": copy.deepcopy(observations),
                "observation_digest": hashlib.sha256(_json(observations)).hexdigest(),
            }
            state["decision"] = None
            self._save(binding, state)
            return self._view(state)

    def command(self, binding: SupervisorBinding, *, action: str, command_id: str,
                expected_revision: int, checkpoint_id: str | None = None,
                instruction: str = "") -> dict:
        _identifier(command_id)
        _text(instruction, empty=True)
        if action not in (*DECISIONS, "request_checkpoint") or type(expected_revision) is not int:
            raise SupervisionError("supervisor_invalid_command")
        if action == "steer" and not instruction.strip():
            raise SupervisionError("supervisor_steer_requires_instruction")
        payload = {"action": action, "command_id": command_id, "revision": expected_revision,
                   "checkpoint_id": checkpoint_id, "instruction": instruction}
        digest = hashlib.sha256(_json(payload)).hexdigest()
        with self._lease(binding):
            state = self._load(binding)
            previous = state["commands"].get(command_id)
            if previous:
                if previous != digest:
                    raise SupervisionError("supervisor_command_conflict")
                return {**self._view(state), "already_recorded": True}
            self._live(state)
            if expected_revision != state["revision"]:
                raise SupervisionError("supervisor_stale_revision")
            if action == "request_checkpoint":
                if checkpoint_id is not None or state["state"] != "running":
                    raise SupervisionError("supervisor_checkpoint_pending")
                state["state"] = "checkpoint_requested"
                state["request"] = payload
            else:
                if (state["state"] not in _WAITING or not state["checkpoint"]
                        or checkpoint_id != state["checkpoint"]["id"]):
                    raise SupervisionError("supervisor_checkpoint_mismatch")
                state["state"] = {"review": "review_requested", "escalate": "escalated"}.get(action, "decision_ready")
                state["decision"] = {**payload, "delivery": "recorded", "recorded_at": time.time()}
            state["revision"] += 1
            state["commands"][command_id] = digest
            # Old retries still fail their revision check after receipt eviction.
            while len(state["commands"]) > 32:
                del state["commands"][next(iter(state["commands"]))]
            self._save(binding, state)
            return {**self._view(state), "already_recorded": False}

    def claim_decision(self, binding: SupervisorBinding, *, checkpoint_id: str,
                       observations: dict) -> dict | None:
        with self._lease(binding):
            state = self._load(binding)
            self._live(state)
            if not state["checkpoint"] or state["checkpoint"]["id"] != checkpoint_id:
                raise SupervisionError("supervisor_checkpoint_mismatch")
            if state["state"] != "decision_ready":
                return None
            if hashlib.sha256(_json(observations)).hexdigest() != state["checkpoint"]["observation_digest"]:
                raise SupervisionError("supervisor_snapshot_changed")
            state["decision"]["delivery"] = "issued_to_worker"
            state["state"] = "running"
            state["request"] = None
            state["revision"] += 1
            self._save(binding, state)
            return copy.deepcopy(state["decision"])

    def assert_released(self, binding: SupervisorBinding) -> None:
        state = self.snapshot(binding)
        if state["state"] != "running" or state["expired"]:
            raise SupervisionError("supervisor_checkpoint_unresolved")

    def close(self, binding: SupervisorBinding, *, reason: str) -> None:
        _identifier(reason)
        with self._lease(binding):
            state = self._load(binding)
            if state["state"] != "closed":
                state.update(state="closed", close_reason=reason, closed_at=time.time(),
                             revision=state["revision"] + 1)
                self._save(binding, state)


class SupervisorChannel:
    """Cooperative tool boundary, reusable by ACP and future API execution loops.

    No automatic retries, account switching, provider calls or code promotion.
    Review/escalate keep the tool waiting; only continue/steer/stop release it.
    """

    def __init__(self, store: SupervisorStore, binding: SupervisorBinding, *,
                 deadline: float, cancel: asyncio.Event, observe: Callable[[], dict]):
        self.store, self.binding = store, binding
        self.deadline, self.cancel, self.observe = deadline, cancel, observe

    def requested(self) -> bool:
        return self.store.snapshot(self.binding)["state"] == "checkpoint_requested"

    async def checkpoint(self, *, phase: str, summary: str, evidence: list[str] | None = None) -> dict:
        if self.cancel.is_set():
            raise SupervisionError("supervisor_cancelled")
        state = self.store.publish(self.binding,
                                   report={"phase": phase, "summary": summary, "evidence": [] if evidence is None else evidence},
                                   observations=self.observe())
        checkpoint_id = state["checkpoint"]["id"]
        try:
            while not self.cancel.is_set() and time.monotonic() < self.deadline:
                current = self.store.snapshot(self.binding)
                if current["state"] == "decision_ready":
                    decision = self.store.claim_decision(self.binding, checkpoint_id=checkpoint_id,
                                                         observations=self.observe())
                    if decision is not None:
                        if decision["action"] == "stop":
                            self.cancel.set()
                            raise SupervisionError("supervisor_stopped")
                        return {"checkpoint_id": checkpoint_id, "action": decision["action"],
                                "instruction": decision["instruction"], "scope_changed": False,
                                "accepted": False, "delivery": "issued_to_worker"}
                elif current["state"] == "closed":
                    raise SupervisionError("supervisor_execution_closed")
                await asyncio.sleep(0.2)
            raise SupervisionError("supervisor_cancelled" if self.cancel.is_set() else "supervisor_wait_expired")
        except BaseException:
            self.cancel.set()
            self.store.close(self.binding, reason="checkpoint_interrupted")
            raise
