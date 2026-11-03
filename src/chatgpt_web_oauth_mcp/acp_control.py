"""Private, owner-bound ACP control mailbox shared by MCP and durable workers.

Only selected native question options cross the live request boundary. Steering
is explicitly queued for a later turn. Committing a reply before returning it to
the SDK provides at-most-once handling; a worker crash never authorizes replay.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .acp_session_store import AcpSessionIdentity, AcpSessionStore, AcpStateError

_MAX_HISTORY = 512
_MAX_MESSAGES = 128
_MAX_QUEUE = 8
_REQUEST_STATES = {"pending", "replied", "consumed", "expired", "cancelled"}
_TURN_STATES = {"new", "active", "completed", "cancelled", "blocked", "failed", "uncertain", "abandoned"}


def _identifier(value, code="acp_control_invalid_identifier", *, maximum=128):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0," + str(maximum - 1) + r"}", value):
        raise AcpStateError(code)
    return value


def _text(value, maximum, code):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum or "\x00" in value:
        raise AcpStateError(code)
    return value


def _options(options):
    if not isinstance(options, list) or not 1 <= len(options) <= 16:
        raise AcpStateError("acp_control_invalid_options")
    result = []
    seen = set()
    for item in options:
        if not isinstance(item, dict) or set(item) != {"id", "name", "kind"}:
            raise AcpStateError("acp_control_invalid_options")
        identifier = _text(item["id"], 200, "acp_control_invalid_options")
        name = _text(item["name"], 1024, "acp_control_invalid_options")
        if identifier in seen or item["kind"] not in {"allow_once", "reject_once"}:
            raise AcpStateError("acp_control_invalid_options")
        seen.add(identifier)
        result.append({"id": identifier, "name": name, "kind": item["kind"]})
    return result


def _request_key(turn_id, request_id):
    return hashlib.sha256(json.dumps([turn_id, request_id]).encode()).hexdigest()


class AcpControlStore:
    """Separate short OS leases let a controller reply while a turn is leased."""

    def __init__(self, root: Path):
        self._files = AcpSessionStore(root)
        self.root = self._files.root

    @contextmanager
    def _lease(self, reference):
        # Unlike the long-lived session lease, this lock spans only local
        # read/validate/atomic-write operations and never an await or provider
        # operation. Concurrent controller replies must serialize, not race.
        fd = os.open(self._files._path(reference, ".lock"),
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise AcpStateError("acp_lock_not_private")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self, reference, identity):
        path = self._files._path(reference, ".json")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return {"version": 1, "identity": asdict(identity), "active_turn": None,
                    "turn_state": "new", "request": None, "requests_seen": {}, "messages": []}
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 262144:
                raise AcpStateError("acp_control_invalid")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                state = json.load(stream)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise AcpStateError("acp_control_corrupt") from exc
        finally:
            os.close(fd)
        if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1:
            raise AcpStateError("acp_control_invalid")
        if state.get("identity") != asdict(identity):
            raise AcpStateError("acp_session_binding_mismatch")
        if not isinstance(state.get("turn_state"), str) or state["turn_state"] not in _TURN_STATES:
            raise AcpStateError("acp_control_invalid")
        if state.get("active_turn") is not None:
            _identifier(state["active_turn"], "acp_control_invalid")
        history = state.get("requests_seen")
        messages = state.get("messages")
        request = state.get("request")
        if (not isinstance(history, dict) or len(history) > _MAX_HISTORY
                or any(not re.fullmatch(r"[a-f0-9]{64}", k) or not isinstance(v, str) or v not in _REQUEST_STATES for k, v in history.items())
                or not isinstance(messages, list) or len(messages) > _MAX_MESSAGES):
            raise AcpStateError("acp_control_invalid")
        if request is not None:
            if not isinstance(request, dict) or not isinstance(request.get("status"), str) or request["status"] not in _REQUEST_STATES:
                raise AcpStateError("acp_control_invalid")
            _identifier(request.get("turn_id"), "acp_control_invalid")
            _identifier(request.get("request_id"), "acp_control_invalid", maximum=200)
            _text(request.get("title"), 8192, "acp_control_invalid")
            _options(request.get("options"))
            expiry = request.get("expires_at")
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                raise AcpStateError("acp_control_invalid")
            if request["status"] in {"replied", "consumed"}:
                reply = request.get("reply")
                if not isinstance(reply, dict) or reply.get("option_id") not in {o["id"] for o in request["options"]}:
                    raise AcpStateError("acp_control_invalid")
        ids = set()
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("status"), str) or message["status"] not in {"queued", "claimed", "cancelled"}:
                raise AcpStateError("acp_control_invalid")
            mid = _identifier(message.get("message_id"), "acp_control_invalid")
            _identifier(message.get("source_turn_id"), "acp_control_invalid")
            if mid in ids:
                raise AcpStateError("acp_control_invalid")
            ids.add(mid)
            if message["status"] == "queued":
                _text(message.get("text"), 8192, "acp_control_invalid")
            elif message["status"] == "claimed":
                _identifier(message.get("target_turn_id"), "acp_control_invalid")
        return state

    def _save(self, reference, state):
        self._files.save(reference, state)

    @staticmethod
    def _require_turn(state, turn_id, *, active=True):
        _identifier(turn_id)
        if state["active_turn"] != turn_id:
            raise AcpStateError("acp_control_turn_mismatch")
        if active and state["turn_state"] != "active":
            raise AcpStateError("acp_control_turn_closed")

    @staticmethod
    def _expire(state):
        request = state["request"]
        if request and request["status"] in {"pending", "replied"} and time.time() >= request["expires_at"]:
            request["status"] = "expired"
            state["requests_seen"][_request_key(request["turn_id"], request["request_id"])] = "expired"
            return True
        return False

    @staticmethod
    def _view(state):
        return {"turn_id": state["active_turn"], "turn_state": state["turn_state"],
                "pending_request": state["request"],
                "steering": [{k: v for k, v in item.items() if k != "text"} for item in state["messages"]]}

    def snapshot(self, reference: str, identity: AcpSessionIdentity) -> dict:
        with self._lease(reference):
            state = self._load(reference, identity)
            if self._expire(state):
                self._save(reference, state)
            return self._view(state)

    def begin_turn(self, reference, identity, *, turn_id) -> list[dict]:
        """Worker-only: caller must already own the matching session lease."""
        _identifier(turn_id)
        with self._lease(reference):
            state = self._load(reference, identity)
            if state["turn_state"] in {"uncertain", "abandoned"}:
                raise AcpStateError("acp_control_session_frozen")
            if state["active_turn"] == turn_id:
                raise AcpStateError("acp_control_turn_already_started")
            request = state["request"]
            if request and request["status"] in {"pending", "replied"}:
                request["status"] = "cancelled"
                state["requests_seen"][_request_key(request["turn_id"], request["request_id"])] = "cancelled"
            claimed = []
            for item in state["messages"]:
                if item["status"] == "queued":
                    claimed.append({"message_id": item["message_id"], "text": item["text"]})
                    item.update(status="claimed", delivery="claimed_for_turn", target_turn_id=turn_id)
            state.update(active_turn=turn_id, turn_state="active", request=None)
            self._save(reference, state)
            return claimed

    def publish_request(self, reference, identity, *, turn_id, request_id, title, options, expires_at):
        _identifier(request_id, maximum=200)
        _text(title, 8192, "acp_control_invalid_question")
        options = _options(options)
        if (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
                or not math.isfinite(expires_at) or not 0 < expires_at - time.time() <= 900):
            raise AcpStateError("acp_input_expired")
        with self._lease(reference):
            state = self._load(reference, identity)
            self._require_turn(state, turn_id)
            key = _request_key(turn_id, request_id)
            if key in state["requests_seen"]:
                raise AcpStateError("acp_control_request_already_seen")
            if len(state["requests_seen"]) >= _MAX_HISTORY:
                raise AcpStateError("acp_control_request_limit")
            if state["request"] and state["request"]["status"] in {"pending", "replied"}:
                raise AcpStateError("acp_control_request_pending")
            request = {"kind": "question", "turn_id": turn_id, "request_id": request_id,
                       "title": title, "options": options, "created_at": time.time(),
                       "expires_at": expires_at, "status": "pending", "disposition": "awaiting_reply"}
            state["request"] = request
            state["requests_seen"][key] = "pending"
            self._save(reference, state)
            return dict(request)

    def _request(self, state, turn_id, request_id):
        self._require_turn(state, turn_id)
        request = state["request"]
        if not request or request["request_id"] != request_id or request["turn_id"] != turn_id:
            raise AcpStateError("acp_control_request_mismatch")
        return request

    def reply(self, reference, identity, *, turn_id, request_id, option_id):
        """Controller: record a single selection; this is not a delivery receipt."""
        _identifier(request_id, maximum=200)
        with self._lease(reference):
            state = self._load(reference, identity)
            request = self._request(state, turn_id, request_id)
            if self._expire(state):
                self._save(reference, state)
            if request["status"] in {"replied", "consumed"}:
                raise AcpStateError("acp_control_reply_duplicate")
            if request["status"] != "pending":
                raise AcpStateError("acp_control_request_closed")
            if not isinstance(option_id, str) or option_id not in {o["id"] for o in request["options"]}:
                raise AcpStateError("acp_control_option_mismatch")
            request.update(status="replied", reply={"option_id": option_id, "received_at": time.time()})
            state["requests_seen"][_request_key(turn_id, request_id)] = "replied"
            self._save(reference, state)
            return {"status": "replied", "delivery": "recorded", "turn_id": turn_id,
                    "request_id": request_id, "option_id": option_id}

    def consume_reply(self, reference, identity, *, turn_id, request_id):
        """Worker-only: persist consumption before returning one native outcome."""
        with self._lease(reference):
            state = self._load(reference, identity)
            request = self._request(state, turn_id, request_id)
            if self._expire(state):
                self._save(reference, state)
            if request["status"] == "pending":
                return None
            if request["status"] == "expired":
                raise AcpStateError("acp_input_expired")
            if request["status"] != "replied":
                raise AcpStateError("acp_control_request_closed")
            request.update(status="consumed", consumed_at=time.time(), disposition="response_committed")
            state["requests_seen"][_request_key(turn_id, request_id)] = "consumed"
            self._save(reference, state)
            return request["reply"]["option_id"]

    def enqueue(self, reference, identity, *, source_turn_id, message_id, text):
        _identifier(message_id)
        _text(text, 8192, "acp_control_invalid_message")
        with self._lease(reference):
            state = self._load(reference, identity)
            self._require_turn(state, source_turn_id, active=False)
            if state["turn_state"] in {"new", "uncertain", "abandoned"}:
                raise AcpStateError("acp_control_session_frozen")
            if any(item["message_id"] == message_id for item in state["messages"]):
                raise AcpStateError("acp_control_message_duplicate")
            if len(state["messages"]) >= _MAX_MESSAGES or sum(item["status"] == "queued" for item in state["messages"]) >= _MAX_QUEUE:
                raise AcpStateError("acp_control_message_limit")
            item = {"message_id": message_id, "source_turn_id": source_turn_id,
                    "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "status": "queued", "delivery": "queued_next_turn", "created_at": time.time()}
            state["messages"].append(item)
            self._save(reference, state)
            return {k: v for k, v in item.items() if k != "text"}

    def finish_turn(self, reference, identity, *, turn_id, status):
        """Worker-only: freeze pending responses when native ownership ends."""
        if status not in _TURN_STATES - {"new", "active"}:
            raise AcpStateError("acp_control_invalid_status")
        with self._lease(reference):
            state = self._load(reference, identity)
            self._require_turn(state, turn_id, active=False)
            request = state["request"]
            if request and request["status"] in {"pending", "replied"}:
                request.update(status="cancelled", disposition="turn_closed")
                state["requests_seen"][_request_key(turn_id, request["request_id"])] = "cancelled"
            state["turn_state"] = status
            if status == "abandoned":
                for message in state["messages"]:
                    if message["status"] == "queued":
                        message.update(status="cancelled", delivery="session_abandoned")
                        message.pop("text", None)
            self._save(reference, state)
