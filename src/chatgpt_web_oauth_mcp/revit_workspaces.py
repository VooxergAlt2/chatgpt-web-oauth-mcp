"""Owner-bound disposable Revit workspaces and conservative desktop fencing.

This is coordination, not a C# sandbox. Native v1 must atomically select and
verify document context and serialize its UI queue. Reservations precede I/O;
uncertain I/O never clears a fence. One state file covers the gateway desktop,
including all Revit processes. All Opss processes for that desktop must share it.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any, Callable, Iterator
import uuid

from mcp.types import Tool, ToolAnnotations
from jsonschema import Draft202012Validator

from . import session
from .job_supervisor import process_identity, process_identity_matches
from .revit_workspace_backend import WorkspaceBackend, verified_identity
from .state_io import atomic_write_bytes, ensure_private_directory, interprocess_file_lock

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
NATIVE_STATES = TERMINAL | {"queued", "running"}
READONLY_WINDOWS = frozenset({"windows_Screenshot", "windows_ControlStatus"})
MAX_WORKSPACES = 64
MAX_REQUESTS_PER_WORKSPACE = 2048
# A dead controller is NOT proof that a Windows GUI action has finished.
# Break-glass recovery is explicit/local-only and never occurs on TTL alone.
ORPHAN_UI_RECOVERY_MIN_SECONDS = 600


def document_key(path: str) -> str:
    """Lexical Windows alias normalization; Node must resolve physical aliases.

    Junctions, symlinks, short names and file IDs require the copying backend's
    immutable-source / fresh-workdir guarantee; Python cannot resolve them on Linux.
    """
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ValueError("INVALID_DOCUMENT_PATH")
    path = path.replace('/', '\\')
    # Reject device paths, ADS, relative paths and trailing-dot/space aliases.
    drive, tail = ntpath.splitdrive(path)
    if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith('\\'):
        raise ValueError("ABSOLUTE_WINDOWS_DOCUMENT_PATH_REQUIRED")
    if ':' in tail or any(part.rstrip(' .') != part for part in tail.split('\\') if part and part not in {'.', '..'}):
        raise ValueError("AMBIGUOUS_DOCUMENT_PATH")
    return ntpath.normcase(ntpath.normpath(path))


def _id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ValueError("INVALID_REQUEST_ID")
    return value


class WorkspaceManager:
    def __init__(self, state_path: Path, backend: WorkspaceBackend, *,
                 owner_provider: Callable[[], str | None] = session.get_current_session_id,
                 clock: Callable[[], float] = time.time, lease_seconds: float = 120):
        self.path = Path(state_path)
        self.backend = backend
        self.owner_provider = owner_provider
        self.clock = clock
        self.lease_seconds = min(max(lease_seconds, 1), 300)

    def _owner(self) -> str:
        owner = self.owner_provider()
        if not owner or not owner.startswith(("openai:", "mcp:")):
            raise ValueError("CHAT_OWNER_REQUIRED: no shared local owner allowed")
        return owner

    @contextmanager
    def _state(self) -> Iterator[dict[str, Any]]:
        ensure_private_directory(self.path.parent)
        with interprocess_file_lock(self.path.with_suffix('.lock')):
            if self.path.is_symlink():
                raise ValueError("INVALID_WORKSPACE_STATE")
            state = json.loads(self.path.read_bytes()) if self.path.exists() else {
                "version": 1, "workspaces": {}, "ui": None,
            }
            if state.get("version") != 1 or not isinstance(state.get("workspaces"), dict):
                raise ValueError("INVALID_WORKSPACE_STATE")
            ui = state.get("ui")
            if (ui and ui.get("state") == "inflight"
                and process_identity_matches(ui.get("controller_pid"), ui.get("controller_identity")) is False):
                # A dead controller proves only that its await ended. The
                # Windows action may still run: retain the fence for review.
                ui["state"] = "draining"
            yield state
            atomic_write_bytes(self.path, json.dumps(state, sort_keys=True).encode(), sync_directory=True)

    def _owned(self, state: dict, workspace_id: str, owner: str) -> dict:
        row = state["workspaces"].get(workspace_id)
        if row is None or row["owner"] != owner:
            raise ValueError("WORKSPACE_NOT_OWNED")
        return row

    @staticmethod
    def _active(state: dict) -> bool:
        return any(row["state"] != "released" for row in state["workspaces"].values())

    @staticmethod
    def _outstanding(row: dict) -> bool:
        return row["state"] in {"preparing", "binding", "releasing", "unknown", "fenced"} or any(
            item["status"] not in TERMINAL for item in row["requests"].values()
        )

    def _no_ui(self, state: dict) -> None:
        # Expiry prohibits new calls but NEVER silently releases a lease.
        if state["ui"] is not None:
            raise ValueError("DESKTOP_BUSY: UI lease/inflight/draining fence retained")

    def require_unscoped_allowed(self) -> None:
        with self._state() as state:
            if self._active(state):
                raise ValueError("WORKSPACE_REQUIRED: use revit_workspace_submit")
            # Native legacy reads must not overtake an unresolved Windows
            # command, even if every disposable RVT has been released.
            self._no_ui(state)

    def _public(self, row: dict) -> dict:
        result = deepcopy(row)
        result.pop("owner")
        result.pop("owner_proof", None)
        result["owner_scope"] = "durable" if row["owner"].startswith("openai:") else "transport"
        if result["owner_scope"] == "transport":
            result["reconnect_limitation"] = "MCP transport owner cannot recover after a new transport session"
        return result

    @staticmethod
    def _protected_payload(row: dict, payload: dict) -> dict:
        if "native_authorization_v1" not in row["instance"].get("capabilities", []):
            return payload
        proof = row.get("owner_proof")
        if not isinstance(proof, str) or len(proof) < 40:
            raise ValueError("WORKSPACE_OWNER_PROOF_REQUIRED")
        return {**payload, "owner_proof": proof}

    async def _hello(self, instance: dict) -> None:
        result = await self.backend.dispatch(instance, "hello", {})
        if verified_identity(result) != verified_identity(instance):
            raise ValueError("INSTANCE_REPLACED: pid/start/build/port identity changed")

    async def instances(self) -> dict:
        self._owner()
        discovered = await self.backend.discover()
        rows = discovered.get("instances")
        if not isinstance(rows, list):
            raise ValueError("INVALID_DISCOVERY")
        for row in rows:
            verified_identity(row)
            await self._hello(row)
        if len({row["instance_id"] for row in rows}) != len(rows):
            raise ValueError("DUPLICATE_INSTANCE_IDENTITY")
        return {"instances": rows}

    async def list(self) -> dict:
        owner = self._owner()
        with self._state() as state:
            return {"workspaces": [self._public(row) for row in state["workspaces"].values()
                                   if row["owner"] == owner]}

    async def create(self, source_path: str, instance_id: str, label: str | None = None) -> dict:
        owner = self._owner()
        source_key = document_key(source_path)
        instances = (await self.instances())["instances"]
        matches = [row for row in instances if row["instance_id"] == instance_id]
        if len(matches) != 1:
            raise ValueError("INSTANCE_NOT_FOUND")
        instance = matches[0]
        workspace_id = "ws_" + uuid.uuid4().hex
        with self._state() as state:
            self._no_ui(state)
            if len(state["workspaces"]) >= MAX_WORKSPACES:
                raise ValueError("WORKSPACE_LIMIT: archive reconciled records before admission")
            for other in state["workspaces"].values():
                previous = other["instance"]
                if other["state"] != "released" and any(previous[key] == instance[key]
                        for key in ("instance_id", "pid", "port")) and verified_identity(previous) != verified_identity(instance):
                    raise ValueError("INSTANCE_REPLACED: registered process identity is fenced")
            if any(other["document_path"] and document_key(other["document_path"]) == source_key
                   for other in state["workspaces"].values()):
                raise ValueError("IMMUTABLE_SOURCE_REQUIRED")
            owner_proof = (secrets.token_urlsafe(32)
                           if "native_authorization_v1" in instance.get("capabilities", []) else None)
            row = {"workspace_id": workspace_id, "owner": owner, "label": label,
                   "instance": instance, "source_path": source_path, "document_path": None,
                   "owner_proof": owner_proof,
                   "state": "preparing", "requests": {}, "lifecycle_request_id": None}
            state["workspaces"][workspace_id] = row
        # Failure or interruption leaves preparing/unknown. Never retry a copy or bind.
        try:
            prepared = (await self.backend.prepare(workspace_id, source_path, owner_proof)
                        if owner_proof else await self.backend.prepare(workspace_id, source_path))
            path = prepared["document_path"]
            path_key = document_key(path)
            if prepared.get("workspace_id") != workspace_id or path_key == source_key:
                raise ValueError("DISPOSABLE_COPY_REQUIRED")
            with self._state() as state:
                row = self._owned(state, workspace_id, owner)
                if any(document_key(other["source_path"]) == path_key or (
                       other["document_path"] and document_key(other["document_path"]) == path_key)
                       for key, other in state["workspaces"].items() if key != workspace_id):
                    raise ValueError("DOCUMENT_PATH_ALREADY_REGISTERED")
                row["document_path"] = path
                if "native_authorization_v1" in instance.get("capabilities", []):
                    # Host consent must precede native bind. The caller can
                    # now open this disposable QA RVT and approve it in Revit.
                    row["state"] = "prepared"
                    return self._public(row)
                row["state"] = "binding"
                # Deterministic native lifecycle ids survive a lost receipt.
                bind_id = "bind:" + workspace_id
                row["lifecycle_request_id"] = bind_id
                row["requests"][bind_id] = {
                    "operation": "bind", "status": "unknown", "request_id": bind_id,
                    "workspace_id": workspace_id, "instance_id": instance["instance_id"],
                    "document_path": path,
                }
            await self._hello(instance)
            result = await self.backend.dispatch(instance, "bind", {
                "workspace_id": workspace_id, "document_path": path,
            })
            self._record(workspace_id, owner, result, lifecycle="bind")
        except BaseException:
            with self._state() as state:
                row = self._owned(state, workspace_id, owner)
                # A concurrent status() may have already reconciled a bind
                # receipt while create() awaited the original response.
                # Never regress a proven terminal native outcome into
                # uncertainty when that older response times out.
                bind_id = row.get("lifecycle_request_id")
                receipt = row["requests"].get(bind_id) if bind_id else None
                if not (receipt and receipt.get("operation") == "bind"
                        and receipt.get("status") in TERMINAL
                        and row["state"] in {"ready", "bind_failed"}):
                    row["state"] = "unknown"
            raise
        with self._state() as state:
            return self._public(self._owned(state, workspace_id, owner))

    async def bind_prepared(self, workspace_id: str) -> dict:
        """Finish a prepared protected QA workspace after in-Revit approval."""
        owner = self._owner()
        with self._state() as state:
            row = self._owned(state, workspace_id, owner)
            self._no_ui(state)
            if row["state"] != "prepared" or self._outstanding(row):
                raise ValueError("WORKSPACE_NOT_PREPARED")
            if "native_authorization_v1" not in row["instance"].get("capabilities", []):
                raise ValueError("PROTECTED_WORKSPACE_REQUIRED")
            instance = deepcopy(row["instance"])
            path = row["document_path"]
            proof = row.get("owner_proof")
        await self._hello(instance)
        # Preflight does not mutate the native model or issue credentials.
        # An explicit in-Revit user approval must have reached Node first.
        authorization = await self.backend.dispatch(instance, "authorization_check",
                                                    {"workspace_id": workspace_id, "owner_proof": proof})
        if authorization.get("authorized") is not True or authorization.get("workspace_id") != workspace_id:
            raise ValueError("AUTHORIZATION_REQUIRED")
        # The user-granted native identity may have rotated during the
        # preflight. Do not even reserve a bind receipt for a changed PID.
        # Node also verifies the exact socket instance+PID on the bind call.
        await self._hello(instance)
        with self._state() as state:
            row = self._owned(state, workspace_id, owner)
            self._no_ui(state)
            if row["state"] != "prepared":
                raise ValueError("WORKSPACE_STATE_CHANGED")
            row["state"] = "binding"
            bind_id = "bind:" + workspace_id
            row["lifecycle_request_id"] = bind_id
            row["requests"][bind_id] = {
                "operation": "bind", "status": "unknown", "request_id": bind_id,
                "workspace_id": workspace_id, "instance_id": instance["instance_id"],
                "document_path": path,
            }
        try:
            result = await self.backend.dispatch(instance, "bind", {
                "workspace_id": workspace_id, "document_path": path, "owner_proof": proof,
            })
            self._record(workspace_id, owner, result, lifecycle="bind")
        except BaseException:
            # A lost network reply may hide an accepted native bind.
            # Never reset the lifecycle ID or silently repeat the operation.
            with self._state() as state:
                row = self._owned(state, workspace_id, owner)
                if row["state"] == "binding":
                    row["state"] = "unknown"
            raise
        with self._state() as state:
            return self._public(self._owned(state, workspace_id, owner))

    def _record(self, workspace_id: str, owner: str, result: dict,
                *, request_id: str | None = None, lifecycle: str | None = None) -> None:
        with self._state() as state:
            row = self._owned(state, workspace_id, owner)
            rid = result.get("request_id")
            if (result.get("workspace_id") != workspace_id
                or result.get("instance_id") != row["instance"]["instance_id"]
                or not rid or (request_id is not None and rid != request_id)
                or result.get("status") not in NATIVE_STATES
                or document_key(result.get("document_path")) != document_key(row["document_path"])):
                raise ValueError("INVALID_NATIVE_ENVELOPE: uncertainty retained")
            _id(rid)
            if any(rid in other["requests"] for key, other in state["workspaces"].items()
                   if key != workspace_id):
                raise ValueError("REQUEST_ID_ALREADY_REGISTERED")
            item = row["requests"].get(rid)
            if item and item["status"] in TERMINAL:
                if item["status"] != result["status"]:
                    raise ValueError("NATIVE_TERMINAL_CONFLICT")
                return  # A late running response must not regress terminal state.
            if lifecycle:
                if item and item.get("operation") != lifecycle:
                    raise ValueError("REQUEST_ID_CONFLICT")
                row["lifecycle_request_id"] = rid
            item = row["requests"].setdefault(rid, {"operation": lifecycle or "submit"})
            item.update({key: deepcopy(result[key]) for key in (
                "request_id", "workspace_id", "instance_id", "status", "document_path", "result", "error",
            ) if key in result})
            operation = item["operation"]
            if operation == "activate":
                ui = state["ui"]
                if not ui or ui.get("activation_request_id") != rid or ui["workspace_id"] != workspace_id:
                    raise ValueError("UI_ACTIVATION_FENCE_MISSING")
                if result["status"] == "succeeded":
                    ui["state"] = "idle"
                    ui["verified"] = True
                    ui["expires_at"] = self.clock() + self.lease_seconds
                elif result["status"] in TERMINAL:
                    ui["state"] = "activation_failed"
                    ui["verified"] = False
            if operation in {"bind", "unbind"}:
                if result["status"] == "succeeded":
                    row["state"] = "ready" if operation == "bind" else "released"
                elif result["status"] in TERMINAL:
                    row["state"] = "bind_failed" if operation == "bind" else "release_failed"
                else:
                    row["state"] = "binding" if operation == "bind" else "releasing"

    async def submit(self, workspace_id: str, command: str, arguments: dict,
                     request_id: str | None = None) -> dict:
        owner = self._owner()
        request_id = _id(request_id) if request_id is not None else "req_" + uuid.uuid4().hex
        if request_id.startswith(("bind:", "unbind:", "activate:")):
            raise ValueError("RESERVED_REQUEST_ID: native lifecycle IDs cannot be submitted by clients")
        if not isinstance(command, str) or not command or not isinstance(arguments, dict):
            raise ValueError("COMMAND_AND_ARGUMENTS_REQUIRED")
        fingerprint = hashlib.sha256(json.dumps([command, arguments], sort_keys=True,
                                               allow_nan=False).encode()).hexdigest()
        with self._state() as state:
            row = self._owned(state, workspace_id, owner)
            previous = row["requests"].get(request_id)
            if previous is not None:
                if previous.get("fingerprint") != fingerprint:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return deepcopy(previous)  # No retransmission, including unknown/rejected.
            # IDs are unique across the desktop to avoid ambiguous native status lookups.
            if any(request_id in other["requests"] for other in state["workspaces"].values()):
                raise ValueError("REQUEST_ID_ALREADY_REGISTERED")
            if len(row["requests"]) >= MAX_REQUESTS_PER_WORKSPACE:
                raise ValueError("WORKSPACE_REQUEST_LIMIT")
            self._no_ui(state)
            if row["state"] != "ready":
                raise ValueError("WORKSPACE_NOT_READY")
            if any(item["status"] == "unknown" for item in row["requests"].values()):
                raise ValueError("REQUEST_UNCERTAIN: reconcile original request_id first")
            row["requests"][request_id] = {"operation": "submit", "status": "unknown",
                                          "fingerprint": fingerprint, "request_id": request_id,
                                          "workspace_id": workspace_id, "instance_id": row["instance"]["instance_id"],
                                          "document_path": row["document_path"]}
            instance = deepcopy(row["instance"])
        await self._checked_dispatch(workspace_id, owner, instance, "submit", {
            "workspace_id": workspace_id, "request_id": request_id,
            "command": command, "arguments": arguments,
        }, request_id)
        with self._state() as state:
            return deepcopy(self._owned(state, workspace_id, owner)["requests"][request_id])

    async def _checked_dispatch(self, workspace_id: str, owner: str, instance: dict,
                                operation: str, payload: dict, request_id: str) -> None:
        with self._state() as state:
            row = self._owned(state, workspace_id, owner)
            payload = self._protected_payload(row, payload)
            item = row["requests"][request_id]
            if item["status"] not in TERMINAL:
                item["status"] = "unknown"
        try:
            await self._hello(instance)
        except BaseException:
            with self._state() as state:
                row = self._owned(state, workspace_id, owner)
                if row["state"] != "fenced":
                    row["before_fence"] = row["state"]
                    row["state"] = "fenced"
            raise
        with self._state() as state:
            row = self._owned(state, workspace_id, owner)
            if row["state"] == "fenced":
                row["state"] = row.pop("before_fence", "unknown")
        result = await self.backend.dispatch(instance, operation, payload)
        self._record(workspace_id, owner, result, request_id=request_id)

    async def status(self, workspace_id: str, request_id: str | None = None) -> dict:
        owner = self._owner()
        with self._state() as state:
            row = self._owned(state, workspace_id, owner)
            if request_id is not None and request_id not in row["requests"]:
                raise ValueError("REQUEST_NOT_FOUND")
            ids = [request_id] if request_id else [rid for rid, item in row["requests"].items()
                                                  if item["status"] not in TERMINAL]
            instance = deepcopy(row["instance"])
        # One bounded native poll per tool call. Remaining IDs stay outstanding.
        if ids:
            rid = ids[0]
            await self._checked_dispatch(workspace_id, owner, instance, "status",
                                         {"workspace_id": workspace_id, "request_id": rid}, rid)
        else:
            await self._hello(instance)
        with self._state() as state:
            row = self._owned(state, workspace_id, owner)
            answer = self._public(row)
            ui = state["ui"]
            if ui and ui.get("owner") == owner and ui.get("workspace_id") == workspace_id:
                answer["ui_lease"] = {key: ui.get(key) for key in (
                    "workspace_id", "token", "expires_at", "state", "verified", "activation_request_id"
                )}
            return answer

    async def cancel(self, workspace_id: str, request_id: str) -> dict:
        owner = self._owner()
        with self._state() as state:
            row = self._owned(state, workspace_id, owner)
            item = row["requests"].get(request_id)
            if item is None:
                raise ValueError("REQUEST_NOT_FOUND")
            if item["status"] in TERMINAL:
                return deepcopy(item)
            instance = deepcopy(row["instance"])
        await self._checked_dispatch(workspace_id, owner, instance, "cancel",
                                     {"workspace_id": workspace_id, "request_id": request_id}, request_id)
        with self._state() as state:
            return deepcopy(self._owned(state, workspace_id, owner)["requests"][request_id])

    async def release(self, workspace_id: str) -> dict:
        owner = self._owner()
        with self._state() as state:
            row = self._owned(state, workspace_id, owner)
            if row["state"] in {"released", "release_failed"}:
                return self._public(row)
            self._no_ui(state)
            if self._outstanding(row):
                raise ValueError("WORKSPACE_BUSY: reconcile native terminal status before release")
            if row["state"] == "prepared":
                # No native bind was ever submitted for prepared-only copies.
                row["state"] = "released"
                return self._public(row)
            row["state"] = "releasing"
            instance = deepcopy(row["instance"])
            unbind_id = "unbind:" + workspace_id
            if unbind_id in row["requests"]:
                raise ValueError("LIFECYCLE_REQUEST_ID_CONFLICT: unbind is already registered")
            row["lifecycle_request_id"] = unbind_id
            row["requests"][unbind_id] = {
                "operation": "unbind", "status": "unknown", "request_id": unbind_id,
                "workspace_id": workspace_id, "instance_id": instance["instance_id"],
                "document_path": row["document_path"],
            }
            payload = self._protected_payload(row, {"workspace_id": workspace_id})
        await self._hello(instance)
        result = await self.backend.dispatch(instance, "unbind", payload)
        self._record(workspace_id, owner, result, lifecycle="unbind")
        with self._state() as state:
            return self._public(self._owned(state, workspace_id, owner))

    async def acquire_ui(self, workspace_id: str) -> dict:
        owner = self._owner()
        with self._state() as state:
            instance = deepcopy(self._owned(state, workspace_id, owner)["instance"])
        await self._hello(instance)
        with self._state() as state:
            row = self._owned(state, workspace_id, owner)
            self._no_ui(state)
            if row["state"] not in {"ready", "prepared"} or any(self._outstanding(r) for r in state["workspaces"].values()):
                raise ValueError("DESKTOP_BUSY: native outstanding work")
            if len(row["requests"]) >= MAX_REQUESTS_PER_WORKSPACE:
                raise ValueError("WORKSPACE_REQUEST_LIMIT")
            lease = {"workspace_id": workspace_id, "owner": owner,
                     "token": secrets.token_urlsafe(32), "expires_at": self.clock() + self.lease_seconds,
                     "state": "idle", "verified": False, "activation_request_id": None}
            state["ui"] = lease
            # GUI selection must happen outside Revit ExternalEvent. The
            # holder may switch/open its QA RVT using Windows tools, then
            # call verify_active to check the document in native API context.
            return {key: lease.get(key) for key in (
                "workspace_id", "token", "expires_at", "state", "verified", "activation_request_id"
            )}

    async def verify_active(self, workspace_id: str, token: str) -> dict:
        """Check the GUI-selected RVT via native API without switching files."""
        owner = self._owner()
        with self._state() as state:
            row = self._owned(state, workspace_id, owner)
            ui = state["ui"]
            if (not ui or ui.get("owner") != owner or ui.get("workspace_id") != workspace_id
                or ui.get("token") != token or ui.get("state") != "idle"
                or ui.get("expires_at", 0) <= self.clock()):
                raise ValueError("UI_LEASE_REQUIRED: acquire GUI lease and finish UI operations")
            if row["state"] != "ready" or any(self._outstanding(r) for r in state["workspaces"].values()):
                raise ValueError("DESKTOP_BUSY: native outstanding work")
            if len(row["requests"]) >= MAX_REQUESTS_PER_WORKSPACE:
                raise ValueError("WORKSPACE_REQUEST_LIMIT")
            activation_id = "activate:" + uuid.uuid4().hex
            ui.update({"state": "verifying", "verified": False, "activation_request_id": activation_id})
            row["requests"][activation_id] = {
                "operation": "activate", "status": "unknown", "request_id": activation_id,
                "workspace_id": workspace_id, "instance_id": row["instance"]["instance_id"],
                "document_path": row["document_path"],
            }
            instance = deepcopy(row["instance"])
        # No GUI action here. The target document is checked atomically with
        # Revit's native serial queue. A wrong active RVT is rejected.
        await self._checked_dispatch(workspace_id, owner, instance, "activate", {
            "workspace_id": workspace_id, "request_id": activation_id,
        }, activation_id)
        with self._state() as state:
            ui = state["ui"]
            return {key: ui.get(key) for key in (
                "workspace_id", "token", "expires_at", "state", "verified", "activation_request_id"
            )}

    async def release_ui(self, workspace_id: str, token: str) -> dict:
        owner = self._owner()
        with self._state() as state:
            self._owned(state, workspace_id, owner)
            ui = state["ui"]
            if not ui or ui["owner"] != owner or ui["workspace_id"] != workspace_id or ui["token"] != token:
                raise ValueError("INVALID_UI_LEASE")
            if ui["state"] not in {"idle", "activation_failed"}:
                raise ValueError("UI_DRAINING: no native terminal proof for generic Windows call")
            state["ui"] = None
            return {"released": True}

    @staticmethod
    def _ui_fence_id(ui: dict) -> str:
        # Identify a fence without sharing its bearer token across chats.
        return hashlib.sha256(ui.get("operation_token", ui["token"]).encode("utf-8")).hexdigest()[:24]

    def _orphaned_unscoped_ui(self, state: dict) -> bool:
        """Check only durable facts; external Windows idleness still needs an operator."""
        ui = state.get("ui")
        if (not isinstance(ui, dict) or ui.get("state") != "draining"
                or ui.get("workspace_id") is not None
                or self._active(state)
                or any(self._outstanding(row) for row in state["workspaces"].values())):
            return False
        pid, identity = ui.get("controller_pid"), ui.get("controller_identity")
        started = ui.get("started_at")
        if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
                or not identity or not isinstance(started, (int, float))
                or self.clock() - started < ORPHAN_UI_RECOVERY_MIN_SECONDS):
            return False
        # Fail closed on inaccessible PID / ambiguous process identity.
        return process_identity_matches(pid, identity) is False

    async def ui_status(self) -> dict:
        """Report a GUI fence without exposing any UI lease credentials."""
        owner = self._owner()
        with self._state() as state:
            ui = state.get("ui")
            if ui is None:
                return {"state": "clear", "scope": None, "recoverable": False}
            scope = "workspace" if ui.get("workspace_id") is not None else "unscoped"
            owned = ui.get("owner") in {None, owner}
            row = state["workspaces"].get(ui.get("workspace_id"))
            scoped_ready = bool(row and row.get("owner") == owner and row.get("state") in {"ready", "prepared"})
            recoverable = (ui.get("state") == "draining" and owned
                           and (scoped_ready if scope == "workspace" else not self._active(state))
                           and not any(self._outstanding(r) for r in state["workspaces"].values()))
            # This does not allow the chat to recover a foreign fence. It
            # exposes only that a locally audited operator procedure exists.
            operator_recovery_eligible = self._orphaned_unscoped_ui(state)
            return {
                "state": ui.get("state"), "scope": scope,
                "fence_id": self._ui_fence_id(ui),
                "owner_matches": owned, "operation": ui.get("operation"),
                "recoverable": recoverable,
                "operator_recovery_eligible": operator_recovery_eligible,
                "operator_recovery_route": ("local_only_manual" if operator_recovery_eligible else None),
                "workspace_id": ui.get("workspace_id") if owned else None,
                "native_identity_check_required": scope == "workspace",
                "warning": ("Unknown GUI effects cannot be cleared automatically. "
                            "Inspect Windows ControlStatus and desktop before recovery."),
            }

    def _check_ui_recovery(self, state: dict, owner: str, fence_id: str) -> dict | None:
        ui = state["ui"]
        if ui.get("state") != "draining":
            raise ValueError("UI_NOT_DRAINING: an in-flight call cannot be recovered")
        if ui.get("owner") not in {None, owner}:
            raise ValueError("UI_FENCE_NOT_OWNED")
        if not isinstance(fence_id, str) or fence_id != self._ui_fence_id(ui):
            raise ValueError("UI_FENCE_CHANGED: inspect current fence_id")
        if any(self._outstanding(row) for row in state["workspaces"].values()):
            raise ValueError("NATIVE_WORKSPACE_ACTIVE")
        if ui.get("workspace_id") is not None:
            row = self._owned(state, ui["workspace_id"], owner)
            if ui.get("owner") != owner or row["state"] not in {"ready", "prepared"}:
                raise ValueError("WORKSPACE_NOT_READY")
            return row
        if self._active(state):
            raise ValueError("NATIVE_WORKSPACE_ACTIVE")
        return None

    async def recover_ui(self, fence_id: str, confirm_idle: bool, evidence: str) -> dict:
        """Recover the owner's uncertain GUI operation after explicit inspection.

        A scoped recovery preserves the lease, clears verification, and checks
        the same native instance before allowing a new GUI/verification step.
        """
        owner = self._owner()
        if confirm_idle is not True or not isinstance(evidence, str) or len(evidence.strip()) < 25:
            raise ValueError("OPERATOR_CONFIRMATION_REQUIRED: inspect GUI and document evidence")
        with self._state() as state:
            if state.get("ui") is None:
                return {"recovered": False, "state": "clear"}
            row = self._check_ui_recovery(state, owner, fence_id)
            instance = deepcopy(row["instance"]) if row is not None else None
        if instance is not None:
            await self._hello(instance)
        with self._state() as state:
            if state.get("ui") is None:
                raise ValueError("UI_FENCE_CHANGED: inspect current fence_id")
            row = self._check_ui_recovery(state, owner, fence_id)
            ui = state["ui"]
            history = state.setdefault("ui_recoveries", [])
            history.append({
                "fence_id": fence_id,
                "owner_scope": "legacy" if ui.get("owner") is None else "bound",
                "operation": ui.get("operation"), "evidence": evidence.strip()[:500],
                "workspace_id": ui.get("workspace_id"),
                "recovered_at": self.clock(),
            })
            del history[:-32]
            if row is None:
                state["ui"] = None
                return {"recovered": True, "state": "clear", "audit_entries": len(history)}
            ui.pop("operation_token", None)
            ui.update(state="idle", verified=False, activation_request_id=None,
                      expires_at=self.clock() + self.lease_seconds)
            return {"recovered": True, "state": "idle", "workspace_id": row["workspace_id"],
                    "verified": False, "expires_at": ui["expires_at"],
                    "next_action": ("Open/approve QA model and release UI lease before bind_prepared"
                                    if row["state"] == "prepared" else
                                    "Select the QA document and call revit_workspace_verify_active"),
                    "audit_entries": len(history)}

    def operator_abort_orphaned_ui(self, fence_id: str, *,
                                   confirmed_remote_idle: bool,
                                   evidence: str, reason: str) -> dict:
        """Locally operated break-glass for a DEAD, stale, unscoped controller.

        This is deliberately not registered as a network MCP tool and is
        never automatically invoked. Windows must be independently checked
        before the operator acknowledges that the remote work has finished.
        """
        if (confirmed_remote_idle is not True or not isinstance(evidence, str)
                or not 25 <= len(evidence.strip()) <= 500
                or not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 200):
            raise ValueError("OPERATOR_REMOTE_IDLE_EVIDENCE_REQUIRED")
        if not isinstance(fence_id, str) or len(fence_id) != 24:
            raise ValueError("UI_FENCE_CHANGED")
        import getpass
        if (not hasattr(os, "getuid") or not self.path.is_file()
                or self.path.is_symlink() or os.getuid() != self.path.stat().st_uid):
            raise ValueError("LOCAL_OPERATOR_OWNER_REQUIRED")
        with self._state() as state:
            ui = state.get("ui")
            if ui is None or self._ui_fence_id(ui) != fence_id:
                raise ValueError("UI_FENCE_CHANGED")
            if ui.get("state") != "draining" or ui.get("workspace_id") is not None:
                raise ValueError("ORPHAN_RECOVERY_UNSCOPED_DRAINING_ONLY")
            if not self._orphaned_unscoped_ui(state):
                raise ValueError("ORPHAN_RECOVERY_CONTROLLER_NOT_PROVEN_DEAD_OR_WORK_ACTIVE")
            # Recheck under the SAME file lock normal admissions use.
            # A late response cannot clear a newer operation: its token is
            # distinct from the retired orphan's operation_token.
            history = state.setdefault("ui_recoveries", [])
            history.append({
                "mode": "local_operator_break_glass",
                "fence_id": fence_id, "reason": reason.strip(),
                "evidence": evidence.strip(),
                "operation": ui.get("operation"),
                "controller_pid": ui["controller_pid"],
                "controller_dead_confirmed": True,
                "previous_owner_sha256": hashlib.sha256(str(ui.get("owner")).encode()).hexdigest(),
                "operator": getpass.getuser(),
                "age_seconds": max(0, round(self.clock() - float(ui["started_at"]))),
                "recovered_at": self.clock(),
            })
            del history[:-32]
            state["ui"] = None
            return {
                "recovered": True, "state": "clear",
                "mode": "local_operator_break_glass",
                "fence_id": fence_id, "audit_entries": len(history),
            }

    async def begin_remote(self, group: str, name: str, arguments: dict) -> tuple[dict, str | None]:
        """Atomically guard legacy Revit calls and every Windows mutation.

        A successful response completes a generic UI call. Disconnect/timeouts,
        cancellation and MCP isError retain a fence until explicit verified recovery.
        """
        args = deepcopy(arguments)
        if group == "windows" and name not in READONLY_WINDOWS:
            with self._state() as state:
                instance = (deepcopy(self._owned(state, args.get("_workspace_id"), self._owner())["instance"])
                            if self._active(state) else None)
            if instance is not None:
                await self._hello(instance)
        with self._state() as state:
            active = self._active(state)
            if group == "revit":
                if active:
                    raise ValueError("WORKSPACE_REQUIRED: use revit_workspace_submit")
                self._no_ui(state)
            elif name in READONLY_WINDOWS:
                args.pop("_workspace_id", None)
                args.pop("_lease_token", None)
                return args, None
            workspace_id = args.pop("_workspace_id", None)
            token = args.pop("_lease_token", None)
            if active:
                owner = self._owner()
                self._owned(state, workspace_id, owner)
                ui = state["ui"]
                if (not ui or ui["owner"] != owner or ui["workspace_id"] != workspace_id
                    or ui["token"] != token or ui["state"] != "idle"
                    or ui["expires_at"] <= self.clock()):
                    raise ValueError("UI_LEASE_REQUIRED")
                if any(self._outstanding(r) for r in state["workspaces"].values()):
                    raise ValueError("DESKTOP_BUSY")
            else:
                self._no_ui(state)
                state["ui"] = {
                    "owner": self._owner(), "workspace_id": None,
                    "token": secrets.token_urlsafe(32),
                    "operation": name, "started_at": self.clock(),
                }
            state["ui"].update(state="inflight", operation=name, started_at=self.clock(),
                               operation_token=secrets.token_urlsafe(32), controller_pid=os.getpid(),
                               controller_identity=process_identity(os.getpid()))
            if state["ui"]["workspace_id"] is not None:
                state["ui"]["verified"] = False
            return args, state["ui"]["operation_token"]

    def uncertain_remote(self, token: str | None) -> None:
        if token is None:
            return
        with self._state() as state:
            ui = state["ui"]
            if ui and ui.get("operation_token") == token and ui.get("state") == "inflight":
                ui["state"] = "draining"

    def complete_remote(self, token: str | None) -> None:
        if token is None:
            return
        with self._state() as state:
            ui = state["ui"]
            if ui and ui.get("operation_token") == token:
                if ui["workspace_id"] is None:
                    state["ui"] = None
                else:
                    ui.pop("operation_token", None)
                    ui["state"] = "idle"
                    ui["verified"] = False

    async def call(self, name: str, arguments: dict) -> dict:
        tool = next((item for item in workspace_tools() if item.name == name), None)
        if tool is None:
            raise ValueError("UNKNOWN_WORKSPACE_TOOL")
        Draft202012Validator(tool.input_schema).validate(arguments)
        method = name.removeprefix("revit_workspace_")
        return await getattr(self, method)(**arguments)


def workspace_tools() -> list[Tool]:
    """Exact private virtual schemas; never register these as public MCP tools."""
    string = {"type": "string", "minLength": 1}
    ws = {"workspace_id": string}
    definitions = [
        ("instances", {}, [], "Discover verified native v1 instances; no UI or model mutation."),
        ("create", {"source_path": string, "instance_id": string, "label": {"type": "string", "maxLength": 200}},
         ["source_path", "instance_id"], "Copy an immutable QA RVT. Protected instances return prepared; open the copy in Revit and approve Full access before bind_prepared. Legacy instances bind asynchronously."),
        ("bind_prepared", ws, ["workspace_id"], "Bind a prepared protected QA RVT after explicit in-Revit approval. Release desktop lease first; reconcile an uncertain bind instead of retrying."),
        ("list", {}, [], "List only this chat's persistent workspace records."),
        ("submit", {**ws, "command": string, "arguments": {"type": "object"}, "request_id": string},
         ["workspace_id", "command", "arguments"], "Queue a native document-scoped command, including send_code_to_revit. Not a security sandbox. Reconcile unknown IDs; never blindly resend."),
        ("status", {**ws, "request_id": string}, ["workspace_id"], "Poll one outstanding request by default, or the explicit request_id; no foreground wait. Native terminal proof clears outstanding work."),
        ("cancel", {**ws, "request_id": string}, ["workspace_id", "request_id"], "Request native cancellation; remains outstanding until native terminal confirmation."),
        ("release", ws, ["workspace_id"], "Unbind only after outstanding work is terminal. Never save or delete the model."),
        ("acquire_ui", ws, ["workspace_id"], "Reserve the Windows desktop for ready or prepared QA RVTs. For prepared, open the copy and approve Full in Revit, then release_ui before bind_prepared. For ready, run verify_active."),
        ("verify_active", {**ws, "token": string}, ["workspace_id", "token"], "Verify via native ExternalEvent that the GUI-selected document matches this workspace. Poll native status until terminal; no automatic switching."),
        ("release_ui", {**ws, "token": string}, ["workspace_id", "token"], "Release an idle lease. A timed out/disconnected Windows call stays draining; TTL cannot clear it."),
        ("ui_status", {}, [], "Inspect desktop UI fence without exposing bearer tokens; use before explicit recovery."),
        ("recover_ui", {
            "fence_id": string, "confirm_idle": {"type": "boolean"},
            "evidence": {"type": "string", "minLength": 25, "maxLength": 500},
        }, ["fence_id", "confirm_idle", "evidence"],
         "Recover an owned timed-out Windows call after ControlStatus/screenshot inspection. Scoped recovery checks native identity and preserves an unverified lease; in-flight/foreign/native-outstanding work is refused."),
    ]
    return [Tool(name="revit_workspace_" + name, description=description,
                 inputSchema={"type": "object", "properties": properties,
                              "required": required, "additionalProperties": False},
                 annotations=ToolAnnotations(readOnlyHint=name in {"instances", "list", "status", "ui_status"},
                                             destructiveHint=name not in {"instances", "list", "status", "ui_status"}))
            for name, properties, required, description in definitions]
