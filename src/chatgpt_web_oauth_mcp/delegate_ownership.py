"""Validate ownership recorded by the server, independently of inbox retention."""
from __future__ import annotations

import hashlib


def persisted_owner_scope(payload: object, *, delegate_id: str, session_id: str) -> str:
    """Use the canonical logical owner; a provider claim or digest alone is no grant."""
    if payload is None:
        return "unowned"
    if not isinstance(payload, dict) or payload.get("delegate_id") != delegate_id:
        return "conflict"
    owner = payload.get("logical_session_id")
    if owner is None:
        return "unowned"
    if not isinstance(owner, str) or not owner:
        return "conflict"
    contexts = () if payload.get("execution_backend") != "acp" else (
        payload[key] for key in ("harness_metadata", "acp_resume_context")
        if key in payload and not (key == "acp_resume_context" and payload[key] is None)
    )
    for metadata in contexts:
        if not isinstance(metadata, dict):
            return "conflict"
        if "owner_digest" in metadata:
            try:
                expected = hashlib.sha256(owner.encode("utf-8")).hexdigest()
            except UnicodeError:
                return "conflict"
            if metadata["owner_digest"] != expected:
                return "conflict"
    return "owned_here" if owner == session_id else "owned_elsewhere"
