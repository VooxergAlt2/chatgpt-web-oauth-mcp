"""Task-owned ACP v1 session core, independent of SDK, launcher and scheduler.

The caller owns OS isolation/account admission and supplies a connection factory.
This module deliberately never enables production routing or grants permissions.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from collections import Counter
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from .acp_control import AcpControlStore
from .acp_session_store import AcpSessionIdentity, AcpSessionStore, AcpStateError
from .acp_telemetry import AcpTelemetry, NATIVE_EVENTS
from .acp_tool_profiles import bind_native_tool_profile, native_tool_profile, profile_sdk_kwargs


def field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


@dataclass(frozen=True)
class AcpRunLimits:
    timeout_seconds: float = 120
    cancel_grace_seconds: float = 5
    max_answer_bytes: int = 65536
    max_events: int = 2048
    max_tool_calls: int = 24

    def __post_init__(self) -> None:
        if not 1 <= self.timeout_seconds <= 900 or not 0.1 <= self.cancel_grace_seconds <= 15:
            raise ValueError("Invalid ACP time budget")
        if self.cancel_grace_seconds >= self.timeout_seconds:
            raise ValueError("Cancellation grace must fit inside the total ACP budget")
        if not 1024 <= self.max_answer_bytes <= 65536 or not 8 <= self.max_events <= 8192:
            raise ValueError("Invalid ACP event/output budget")
        if not 1 <= self.max_tool_calls <= 64:
            raise ValueError("Invalid ACP tool budget")


class AcpTurnClient:
    """Synchronous state mutation inside SDK callbacks; cancellation is external.

    Replay events and foreign sessions cannot contaminate the next turn. Thought
    chunks, raw tool input/output and auth responses are intentionally not stored.
    """

    def __init__(self, limits: AcpRunLimits, progress: Callable[[dict], None] | None = None,
                 *, question_handler: Callable | None = None, allow_native_tools: bool = False,
                 telemetry: AcpTelemetry | None = None):
        self.limits = limits
        self.progress = progress
        self.telemetry = telemetry
        self.question_handler = question_handler
        self.allow_native_tools = allow_native_tools is True
        self.session_id: str | None = None
        self.live = False
        self.closed = False
        self.text = ""
        self.answer_bytes = 0
        self.events: Counter[str] = Counter()
        self.tool_ids: set[str] = set()
        self.tool_statuses: dict[str, str] = {}
        self.usage: dict[str, int] = {}
        self.terminal_response: dict | None = None
        self.pending: dict | None = None
        self.stop = asyncio.Event()
        self.stop_code: str | None = None

    def stop_with(self, code: str) -> None:
        if self.stop_code is None:
            self.stop_code = code
        self.stop.set()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if not self.live or session_id != self.session_id or self.stop.is_set():
            return
        kind = str(field(update, "session_update", "unknown"))
        kind = kind if kind in NATIVE_EVENTS else "unknown"
        self.events[kind] += 1
        if self.telemetry is not None:
            self.telemetry.native_event(kind)
        if sum(self.events.values()) > self.limits.max_events:
            self.stop_with("acp_event_budget_exceeded")
            return
        if kind == "agent_message_chunk":
            text = field(field(update, "content"), "text")
            if isinstance(text, str):
                size = len(text.encode("utf-8"))
                if self.answer_bytes + size > self.limits.max_answer_bytes:
                    self.stop_with("acp_output_budget_exceeded")
                    return
                self.answer_bytes += size
                self.text += text
        elif kind in {"tool_call", "tool_call_update"}:
            identifier = field(update, "tool_call_id")
            if not isinstance(identifier, str) or not identifier or len(identifier) > 200:
                self.stop_with("acp_invalid_tool_identity")
                return
            self.tool_ids.add(identifier)
            status = field(update, "status")
            if status in {"completed", "failed", "pending", "in_progress"}:
                self.tool_statuses[identifier] = status
            elif identifier not in self.tool_statuses:
                self.tool_statuses[identifier] = "pending"
            if len(self.tool_ids) > self.limits.max_tool_calls:
                self.stop_with("acp_tool_budget_exceeded")
        elif kind == "usage_update":
            for key in ("used", "size"):
                value = field(update, key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self.usage[key] = value
        if self.progress is not None:
            self.progress({"event": kind, "tools": len(self.tool_ids), "answer_bytes": self.answer_bytes})

    async def request_permission(self, session_id: str, tool_call: Any, options: list, **kwargs: Any) -> dict:
        if self.closed or self.stop.is_set():
            return {"outcome": {"outcome": "cancelled"}}
        identifier = str(field(tool_call, "tool_call_id", ""))
        interaction = identifier.startswith("interaction_")
        # Pinned runtime _handle_interaction emits this typed shape. A title or
        # an arbitrary executable tool must never acquire question semantics.
        question = (interaction and field(tool_call, "kind") in {None, "other"}
                    and field(tool_call, "raw_input") in (None, {}))
        bound = self.live and session_id == self.session_id
        if bound:
            if not identifier or len(identifier) > 200 or any(ord(c) < 32 for c in identifier):
                self.stop_with("acp_invalid_tool_identity")
                return {"outcome": {"outcome": "cancelled"}}
            # Permission callbacks may precede streamed tool_call notifications.
            # Count the stable ID before allowing a confined writer action.
            self.tool_ids.add(identifier)
            if len(self.tool_ids) > self.limits.max_tool_calls:
                self.stop_with("acp_tool_budget_exceeded")
                return {"outcome": {"outcome": "cancelled"}}
        code = "acp_input_required" if question else "acp_permission_denied"
        if not bound:
            code = "acp_unbound_permission_request"
        safe_options = [
            {"id": str(field(o, "option_id", "")), "name": str(field(o, "name", ""))[:256],
             "kind": str(field(o, "kind", ""))}
            for o in options[:16]
        ]
        if bound and question and self.question_handler is not None:
            request = {"kind": "question", "request_id": identifier,
                       "title": str(field(tool_call, "title", ""))[:2048], "options": safe_options}
            self.pending = request
            try:
                selected = await self.question_handler(request)
            except AcpStateError as exc:
                request["disposition"] = "expired" if exc.code == "acp_input_expired" else "cancelled"
                self.stop_with(exc.code)
            else:
                if selected is not None:
                    self.pending = None
                    return {"outcome": {"outcome": "selected", "option_id": selected}}
                request["disposition"] = "cancelled"
                self.stop_with("acp_cancelled")
            return {"outcome": {"outcome": "cancelled"}}
        # This flag is set only by the trusted worker after writer confinement
        # is validated. It never accepts session-wide or title-based grants.
        if bound and not interaction and self.allow_native_tools:
            if any(o["id"] == "allow" and o["kind"] == "allow_once" for o in safe_options):
                return {"outcome": {"outcome": "selected", "option_id": "allow"}}
        if self.pending is None:
            self.pending = {
                "kind": "question" if question else "permission",
                "request_id": identifier[:200],
                "title": str(field(tool_call, "title", ""))[:2048],
                "options": safe_options,
                "disposition": "cancelled_requires_continuation",
            }
        self.stop_with(code)
        # Never use allow_always or display text to authorize a tool.
        return {"outcome": {"outcome": "cancelled"}}

    async def read_text_file(self, **kwargs: Any) -> dict:
        raise AcpStateError("acp_client_filesystem_disabled")

    async def write_text_file(self, **kwargs: Any) -> dict:
        raise AcpStateError("acp_client_filesystem_disabled")

    async def create_terminal(self, **kwargs: Any) -> dict:
        raise AcpStateError("acp_client_terminal_disabled")


class AcpSessionRunner:
    """One durable turn on an explicitly bound session.

    connection_factory(client) returns an async context manager yielding an ACP
    connection. It is supplied by trusted supervisor code, never model input.
    SDK/OS process ownership stays outside this reusable state machine.
    """

    def __init__(self, store: AcpSessionStore, connection_factory: Callable):
        self.store = store
        self.connection_factory = connection_factory

    async def run(
        self, *, reference: str, identity: AcpSessionIdentity, turn_id: str,
        prompt: str, limits: AcpRunLimits | None = None,
        cancel: asyncio.Event | None = None,
        progress: Callable[[dict], None] | None = None,
        control: AcpControlStore | None = None,
        allow_native_tools: bool = False,
        mcp_servers: list | None = None,
        defer_completion: bool = False,
        expected_previous_turn: str | None = None,
    ) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", turn_id):
            raise ValueError("Invalid ACP turn ID")
        if expected_previous_turn is not None and (
            not isinstance(expected_previous_turn, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", expected_previous_turn)
        ):
            raise ValueError("Invalid ACP previous turn ID")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 131072:
            raise ValueError("Invalid ACP prompt size")
        limits = limits or AcpRunLimits()
        mcp_servers = list(mcp_servers or [])  # supplied only by the trusted task gateway
        with self.store.lease(reference):
            state = self.store.load(reference, identity)
            if state.get("phase") == "running":
                raise AcpStateError("acp_turn_outcome_unknown")
            if state.get("phase") == "abandoned":
                raise AcpStateError("acp_session_abandoned")
            if expected_previous_turn is not None and state.get("turn") != expected_previous_turn:
                # Queue admission is not a history reservation. Recheck under the
                # lease so an older source cannot branch from newer native state.
                raise AcpStateError("acp_resume_source_stale")
            seen = state.get("turns_seen", [])
            if not isinstance(seen, list) or any(not isinstance(t, str) for t in seen):
                raise AcpStateError("acp_checkpoint_invalid")
            if state.get("turn") == turn_id or turn_id in seen:
                # Never silently reuse a turn ID even if its result looked good.
                raise AcpStateError("acp_turn_already_recorded")
            if len(seen) >= 512:
                # Never evict an old deduplication key to make space.
                raise AcpStateError("acp_session_turn_limit")
            if cancel is not None and cancel.is_set():
                return {"status": "cancelled", "success": False, "error": {"code": "acp_cancelled_before_start"}}
            started = time.monotonic()
            profile = native_tool_profile(writer=allow_native_tools is True, questions=control is not None)
            if bind_native_tool_profile(state, profile):
                # Persist before the provider sees a session configuration. A
                # crash must not permit the same reference to change its tools.
                self.store.save(reference, state)
            telemetry = AcpTelemetry("session")

            def report(event):
                if progress is not None:
                    try:
                        progress({**event, "runtime": telemetry.progress()})
                    except Exception:  # noqa: BLE001
                        # Diagnostics are advisory. A broken observer cannot
                        # change native finality or replace the failure cause.
                        # Cancellation and other BaseExceptions still propagate.
                        pass

            def enter(phase):
                if telemetry.enter(phase):
                    report({"event": "phase", "tools": len(client.tool_ids),
                            "answer_bytes": client.answer_bytes})

            client = AcpTurnClient(limits, report, allow_native_tools=allow_native_tools, telemetry=telemetry)
            deadline = started + limits.timeout_seconds
            sent = False
            control_started = False
            steering = []
            if control is not None:
                async def answer_question(request):
                    return await self._answer_question(control, reference, identity, turn_id,
                                                       client, request, cancel, deadline - limits.cancel_grace_seconds)
                client.question_handler = answer_question
            result: dict = {}
            transport_stage = "connect"
            transport_failure = None

            @asynccontextmanager
            async def measured_connection():
                async with self.connection_factory(client) as transport:
                    try:
                        yield transport
                    except BaseException:
                        telemetry.fail()
                        raise
                    finally:
                        enter("teardown")

            try:
                enter("connect")
                async with asyncio.timeout_at(deadline):
                    async with measured_connection() as transport:
                        conn = transport[0] if isinstance(transport, tuple) else transport
                        transport_stage = "initialize"
                        enter(transport_stage)
                        hello = await conn.initialize(protocol_version=1)
                        if field(hello, "protocol_version") != 1:
                            raise AcpStateError("acp_protocol_version_mismatch")
                        if mcp_servers and field(field(field(hello, "agent_capabilities"), "mcp_capabilities"), "http") is not True:
                            raise AcpStateError("acp_mcp_http_unsupported")
                        if cancel is not None and cancel.is_set():
                            raise AcpStateError("acp_cancelled_before_start")
                        previous = state.get("session_id")
                        if previous:
                            if not isinstance(previous, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", previous):
                                raise AcpStateError("acp_invalid_provider_session")
                            caps = field(hello, "agent_capabilities")
                            if field(caps, "load_session") is not True:
                                raise AcpStateError("acp_session_load_unsupported")
                            client.session_id = previous
                            transport_stage = "load_session"
                            enter(transport_stage)
                            session = await conn.load_session(
                                session_id=previous, cwd=identity.workspace, mcp_servers=mcp_servers,
                                **profile_sdk_kwargs(profile),
                            )
                        else:
                            transport_stage = "new_session"
                            enter(transport_stage)
                            session = await conn.new_session(
                                cwd=identity.workspace, mcp_servers=mcp_servers, **profile_sdk_kwargs(profile),
                            )
                            sid = field(session, "session_id")
                            if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", sid):
                                raise AcpStateError("acp_invalid_provider_session")
                            state["session_id"] = sid
                            client.session_id = sid
                            state["phase"] = "ready"
                            self.store.save(reference, state)
                        if client.stop.is_set():
                            raise AcpStateError(client.stop_code or "acp_permission_denied")
                        transport_stage = "configure"
                        enter(transport_stage)
                        await self._configure(conn, session, client.session_id, identity)
                        if cancel is not None and cancel.is_set():
                            raise AcpStateError("acp_cancelled_before_start")
                        # History was ignored while loading. Now begin the new turn.
                        state.update(phase="running", turn=turn_id, result=None, turns_seen=[*seen, turn_id])
                        self.store.save(reference, state)
                        if control is not None:
                            prompt, steering = control.begin_prompt_turn(reference, identity, turn_id=turn_id,
                                                                        prompt=prompt)
                            control_started = True
                            if prompt is None:
                                raise AcpStateError("acp_prompt_composition_too_large")
                        client.live = True
                        sent = True
                        transport_stage = "prompt"
                        enter(transport_stage)
                        result = await self._prompt(conn, client, prompt, limits, cancel, deadline, enter)
                        if not result.get("success"):
                            telemetry.fail()
                        transport_stage = "teardown"
            except TimeoutError:
                telemetry.fail()
                result = self._failure("acp_timeout", "timed_out")
            except AcpStateError as exc:
                telemetry.fail()
                result = self._failure(exc.code)
            except asyncio.CancelledError:
                telemetry.fail()
                # Preserve running state for reconciliation after a killed owner.
                if control_started:
                    control.finish_turn(reference, identity, turn_id=turn_id, status="uncertain")
                raise
            except Exception as exc:  # noqa: BLE001
                telemetry.fail()
                # Exceptions may contain OAuth URLs or request credentials.
                result = self._failure("acp_transport_error")
                exception_type = type(exc).__name__
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", exception_type):
                    exception_type = "Exception"
                transport_failure = {"stage": transport_stage, "exception_type": exception_type}
            finally:
                client.live = False
                client.closed = True
            # A received terminal response remains useful after cancellation or
            # teardown failure, but can never turn those outcomes into success.
            if client.terminal_response is not None:
                result.update(client.terminal_response)
            result["session_ref"] = reference
            result["turn_id"] = turn_id
            result["prompt_sent"] = sent
            result["duration_seconds"] = round(time.monotonic() - started, 3)
            result["harness_metadata"] = {
                "backend": "acp", "model": identity.model,
                "reasoning_effort": identity.thinking,
                "owner_digest": identity.owner,
                "scope_digest": identity.scope_digest,
                "conversation_id": state.get("session_id"),
                "event_counts": dict(client.events), "tool_calls": len(client.tool_ids),
                "usage_context": client.usage,
                "steering_message_ids": [item["message_id"] for item in steering],
                "native_tool_profile": {**profile, "enabled_tools": list(profile["enabled_tools"])},
                "acp_session_diagnostics": telemetry.snapshot(),
            }
            if client.terminal_response is not None and "usage" in client.terminal_response:
                # SDK 0.12.1 marks usage unstable and has conflicting turn/session
                # scope descriptions. Preserve provider values without summing.
                result["harness_metadata"].update(
                    usage_source="prompt_response", usage_scope="provider_reported_unspecified",
                )
            if transport_failure is not None:
                result["harness_metadata"]["transport_failure"] = transport_failure
            if client.pending is not None:
                result["pending_request"] = client.pending
            # Useful text is advisory on failures and never changes status.
            if client.text:
                result["answer"] = client.text
            code = (result.get("error") or {}).get("code")
            unknown = sent and code in {
                "acp_transport_error", "acp_timeout", "acp_cancel_unconfirmed",
                "acp_unfinished_tools",
            }
            result["outcome_uncertain"] = unknown
            phase = (
                "running" if unknown else "completed" if result.get("success")
                else "blocked" if result.get("status") == "blocked"
                else "cancelled" if result.get("status") == "cancelled" else "failed"
            )
            checkpoint_result = result
            if defer_completion is True and result.get("success") is True:
                # Native completion is not a writer promotion receipt. Keep a
                # frozen checkpoint across the handoff to server verification,
                # including a crash after this lease is released. The caller
                # receives the native result and must finalize under a new lease.
                checkpoint_result = dict(result)
                checkpoint_result.update(
                    status="failed", success=False, outcome_uncertain=True,
                    error={"code": "acp_writer_postprocessing_incomplete"},
                    native_outcome={"status": result["status"], "success": True,
                                    "stop_reason": result.get("stop_reason")},
                )
                phase = "running"
            state.update(phase=phase, turn=turn_id, result=checkpoint_result, turns_seen=[*seen, turn_id])
            self.store.save(reference, state)
            if control_started:
                control.finish_turn(reference, identity, turn_id=turn_id, status="uncertain" if phase == "running" else phase)
            return result

    @staticmethod
    async def _answer_question(control, reference, identity, turn_id, client, request, cancel, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AcpStateError("acp_input_expired")
        request.update(control.publish_request(
            reference, identity, turn_id=turn_id, request_id=request["request_id"],
            title=request["title"], options=request["options"], expires_at=time.time() + min(remaining, 300),
        ))
        if client.progress is not None:
            client.progress({"event": "input_required", "request_id": request["request_id"],
                             "expires_at": request["expires_at"]})
        while client.live and not client.closed and not client.stop.is_set():
            if cancel is not None and cancel.is_set():
                return None
            try:
                option = control.consume_reply(reference, identity, turn_id=turn_id, request_id=request["request_id"])
            except AcpStateError as exc:
                if exc.code != "acp_session_busy":
                    raise
            else:
                if option is not None:
                    return option
            await asyncio.sleep(0.1)
        return None

    @staticmethod
    async def _configure(conn: Any, session: Any, sid: str, identity: AcpSessionIdentity) -> None:
        choices = {field(o, "id"): o for o in (field(session, "config_options") or [])}
        desired = {"model": identity.model, "thinking_level": identity.thinking, "mode": "default"}
        for key, value in desired.items():
            option = choices.get(key)
            # Fail closed rather than silently keeping provider defaults/YOLO.
            if option is None:
                raise AcpStateError("acp_required_config_missing")
            advertised = {field(o, "value") for o in (field(option, "options") or [])}
            if value not in advertised:
                raise AcpStateError("acp_requested_config_unavailable")
            if field(option, "current_value") != value:
                changed = await conn.set_config_option(config_id=key, session_id=sid, value=value)
                confirmed = next((o for o in (field(changed, "config_options") or []) if field(o, "id") == key), None)
                if field(confirmed, "current_value") != value:
                    raise AcpStateError("acp_config_not_confirmed")

    @staticmethod
    def _failure(code: str, status: str = "failed") -> dict:
        if code in {"acp_input_required", "acp_input_expired"}:
            status = "blocked"
        elif code.startswith("acp_cancelled"):
            status = "cancelled"
        return {"status": status, "success": False, "error": {"code": code}}

    @staticmethod
    def _terminal_metadata(response) -> dict:
        # Exact SDK 0.12.1 enum; unknown provider text must never enter metadata.
        reason = field(response, "stop_reason")
        allowed = {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}
        result = {"stop_reason": reason if isinstance(reason, str) and reason in allowed else "unknown"}
        usage = field(response, "usage")
        if usage is not None:
            values = {}
            for key in ("total_tokens", "input_tokens", "output_tokens", "thought_tokens", "cached_read_tokens", "cached_write_tokens"):
                number = field(usage, key)
                if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
                    values[key] = number
            result["usage"] = values
        return result

    async def _prompt(self, conn, client, prompt, limits, cancel, deadline, enter) -> dict:
        # Dict content blocks are accepted by the SDK and keep core tests free
        # from an installed protocol package. All signatures match SDK 0.12.1.
        operation = asyncio.create_task(conn.prompt(
            session_id=client.session_id, prompt=[{"type": "text", "text": prompt}],
        ))
        stop_wait = asyncio.create_task(client.stop.wait())
        external_wait = asyncio.create_task(cancel.wait()) if cancel is not None else None
        waits = {operation, stop_wait}
        if external_wait is not None:
            waits.add(external_wait)
        try:
            done, _ = await asyncio.wait(waits, timeout=max(0, deadline - time.monotonic() - limits.cancel_grace_seconds), return_when=asyncio.FIRST_COMPLETED)
            if operation in done and not client.stop.is_set() and not (cancel and cancel.is_set()):
                response = await operation
                client.terminal_response = self._terminal_metadata(response)
                reason = client.terminal_response["stop_reason"]
                if reason == "cancelled":
                    return self._failure("acp_cancelled", "cancelled")
                if reason != "end_turn":
                    return self._failure("acp_incomplete_turn")
                if any(s in {"pending", "in_progress"} for s in client.tool_statuses.values()):
                    return self._failure("acp_unfinished_tools")
                if not client.text.strip():
                    return self._failure("acp_empty_answer")
                # A native helper may report failure as an ordinary end_turn text.
                if client.text.lstrip().startswith("Agent execution error:"):
                    return self._failure("acp_native_execution_error")
                return {"status": "succeeded", "success": True, **client.terminal_response}
            code = client.stop_code or ("acp_cancelled" if cancel and cancel.is_set() else "acp_timeout")
            if client.telemetry is not None:
                client.telemetry.fail()
            enter("cancel")
            try:
                cancel_deadline = min(deadline, time.monotonic() + limits.cancel_grace_seconds)
                # The pinned runtime doesn't latch an early session/cancel:
                # native send resets its cancel flag, and a model rebuild may
                # replace the agent before prompt dispatch. Repeat only the
                # idempotent control notification, never the prompt. A terminal
                # prompt response remains the sole acknowledgement.
                interval = 0.25
                attempts = max(1, math.ceil(limits.cancel_grace_seconds / interval))
                async with asyncio.timeout_at(cancel_deadline):
                    for _ in range(attempts):
                        await conn.cancel(session_id=client.session_id)
                        finished, _ = await asyncio.wait(
                            {operation}, timeout=min(interval, max(0, cancel_deadline - time.monotonic())),
                        )
                        if operation in finished:
                            client.terminal_response = self._terminal_metadata(await operation)
                            break
                    else:
                        client.terminal_response = self._terminal_metadata(await asyncio.shield(operation))
            except Exception:  # noqa: BLE001
                return self._failure("acp_cancel_unconfirmed")
            return self._failure(code, "timed_out" if code == "acp_timeout" else "failed")
        finally:
            # A cancel notification can fail even though prompt already returned.
            # Retain only its diagnostics; the conservative outcome stays intact.
            if (client.terminal_response is None and operation.done()
                    and not operation.cancelled() and operation.exception() is None):
                client.terminal_response = self._terminal_metadata(operation.result())
            # Freeze state before SDK teardown delivers any late updates.
            client.live = False
            client.closed = True
            for task in waits:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
            # A task may finish with a terminal response while handling its
            # local cancellation. Preserve its counters after cleanup as well;
            # this late observation never confirms cancellation or success.
            if (client.terminal_response is None and operation.done()
                    and not operation.cancelled() and operation.exception() is None):
                client.terminal_response = self._terminal_metadata(operation.result())
