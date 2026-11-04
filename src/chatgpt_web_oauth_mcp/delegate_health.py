"""Observed account/transport health, projected from existing durable terminal events."""
from __future__ import annotations

import time

from .delegate_failures import error_parts, runtime_provider_failure
from .routing.failure_taxonomy import FailureType, classify_failure

STALE_SECONDS = 900.0


def health_classification(snapshot: dict) -> tuple[str, str]:
    """Return state and failure domain, without retaining provider error text."""
    meta = snapshot.get("harness_metadata")
    meta = meta if isinstance(meta, dict) else {}
    if snapshot.get("outcome_uncertain") is True or meta.get("outcome_uncertain") is True:
        return "reconcile_required", "session"
    code, _ = error_parts(snapshot.get("error"))
    failure = classify_failure(snapshot).failure_type
    _, eligibility, _ = runtime_provider_failure(
        harness=str(snapshot.get("harness") or ""), error=snapshot.get("error"))
    if eligibility:
        return "eligibility_blocked", "account"
    if failure == FailureType.AUTH_ERROR:
        return "auth_required", "account"
    if failure == FailureType.RATE_LIMIT:
        return "quota_limited", "account"
    if code in {"acp_transport_error", "acp_timeout", "acp_cancel_unconfirmed",
                "delegate_harness_unavailable", "antigravity_unavailable"}:
        return "transport_error", "transport"
    if failure == FailureType.PROVIDER_ERROR:
        return "provider_error", "transport"
    if snapshot.get("status") == "succeeded":
        return "healthy", "transport"
    return "unknown", "task"


class DelegateHealth:
    """Caller serializes updates with the registry lock. No provider probes."""

    def __init__(self, *, clock=time.time):
        self.clock = clock
        self.observations: dict[tuple[str, str], dict] = {}
        self.account_observations: dict[str, dict] = {}

    def observe(self, snapshot: dict) -> None:
        account = str(snapshot.get("harness") or "")
        transport = snapshot.get("execution_backend", "cli")
        observed = snapshot.get("completed_at_epoch")
        now = self.clock()
        if (not account or transport not in {"cli", "acp"}
                or type(observed) not in {int, float} or not 0 < observed <= now):
            return
        state, domain = health_classification(snapshot)
        if state == "unknown":
            return
        key = (account, transport)
        previous = self.observations.get(key, {})
        if observed <= previous.get("observed_at_epoch", 0):
            return
        code, _ = error_parts(snapshot.get("error"))
        row = {"state": state, "domain": domain, "observed_at_epoch": observed,
               "delegate_id": str(snapshot.get("delegate_id") or "")[:128],
               "observed_kind": str(snapshot.get("kind") or "unknown")[:32],
               "error_code": code[:128], "source": "durable_terminal_event"}
        self.observations[key] = row
        if domain == "account" or state == "healthy":
            old = self.account_observations.get(account, {})
            if observed > old.get("observed_at_epoch", 0):
                self.account_observations[account] = row

    def snapshot(self, account: str, transport: str) -> dict:
        row = dict(self.observations.get((account, transport), {}))
        shared = self.account_observations.get(account, {})
        if (row.get("domain") == "account" and shared.get("state") == "healthy"
                and shared.get("observed_at_epoch", 0) > row.get("observed_at_epoch", 0)):
            row.update(state="unknown", domain="transport",
                       reason="account_recovered_transport_unobserved")
        if (shared.get("domain") == "account"
                and shared.get("observed_at_epoch", 0) >= row.get("observed_at_epoch", 0)):
            row = dict(shared)
        observed = row.get("observed_at_epoch")
        if observed is None:
            return {"version": 1, "state": "unknown", "observed_at_epoch": None,
                    "account": account, "transport": transport,
                    "source": "no_terminal_observation"}
        age = max(0.0, self.clock() - observed)
        historical_state = row["state"]
        row.update(version=1, account=account, transport=transport,
                   age_seconds=round(age, 3), last_observed_state=historical_state)
        if age > STALE_SECONDS:
            row["state"] = "stale"
        return row
