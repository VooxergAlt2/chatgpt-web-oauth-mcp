"""Incremental, bounded AGY stream-JSON tool-call meter for durable readers."""

from __future__ import annotations

import json
from pathlib import Path


_MAX_EVENT_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 128 * 1024


class AgyToolCallMeter:
    """Count unique tool steps, not duplicate ACTIVE/DONE progress messages.

    Finishing is exempt so an agent can still deliver its result after using
    its permitted research calls. Never persist parameters or file paths.
    """

    def __init__(self, *, limit: int) -> None:
        if not 1 <= limit <= 64:
            raise ValueError("AGY tool-call limit must be between 1 and 64")
        self.limit = limit
        self.count = 0
        self.tool_counts: dict[str, int] = {}
        self._seen_steps: set[tuple[str, int, str]] = set()
        self._cursor = 0
        self._pending = b""
        self.invalid_stream = False

    @property
    def exceeded(self) -> bool:
        return self.count > self.limit or self.invalid_stream

    def scan(self, path: Path) -> int:
        """Read at most one bounded chunk; retain incomplete NDJSON lines."""

        if self.exceeded:
            return 0
        try:
            with path.open("rb") as handle:
                handle.seek(self._cursor)
                chunk = handle.read(_READ_CHUNK_BYTES)
        except OSError:
            return 0
        if not chunk:
            return 0
        self._cursor += len(chunk)
        data = self._pending + chunk
        lines = data.split(b"\n")
        self._pending = lines.pop()
        if len(self._pending) > _MAX_EVENT_BYTES:
            self.invalid_stream = True
            return len(chunk)
        for line in lines:
            if len(line) > _MAX_EVENT_BYTES:
                self.invalid_stream = True
                return len(chunk)
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(event, dict) or event.get("event") != "step_update":
                continue
            step = event.get("step_update")
            if not isinstance(step, dict):
                continue
            name = step.get("tool_name")
            index = step.get("step_index")
            if isinstance(name, str) and name != "finish" and (
                not isinstance(index, int) or isinstance(index, bool)
            ):
                # A changed AGY protocol must not silently bypass a promised
                # hard limit when its tool events cannot be deduplicated.
                self.invalid_stream = True
                return len(chunk)
            if (
                not isinstance(name, str)
                or name == "finish"
                or not isinstance(index, int)
                or isinstance(index, bool)
            ):
                continue
            key = (str(step.get("conversation_id") or event.get("conversation_id") or ""), index, name)
            if key in self._seen_steps:
                continue
            self._seen_steps.add(key)
            self.count += 1
            self.tool_counts[name] = self.tool_counts.get(name, 0) + 1
            if self.exceeded:
                return len(chunk)
        return len(chunk)

    def snapshot(self) -> dict[str, object]:
        return {
            "limit": self.limit,
            "used": self.count,
            "tool_call_counts": dict(sorted(self.tool_counts.items())),
            "enforcement": "durable_agy_stream",
            "stream_valid": not self.invalid_stream,
        }
