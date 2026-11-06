"""ACP identity adapter for the provider-neutral supervision contract."""
from .acp_session_store import AcpSessionIdentity
from .delegate_progress_policy import DelegateProgressPolicy
from .supervision import AgentEndpoint, SupervisorBinding, SupervisionError


def supervisor_binding(identity: AcpSessionIdentity, reference: str, turn_id: str) -> SupervisorBinding:
    return SupervisorBinding(
        owner=identity.owner, execution_id=turn_id, session_ref=reference,
        endpoint=AgentEndpoint(provider="google", transport="acp", account_id=identity.account,
                               model=identity.model, reasoning=identity.thinking),
        workspace=identity.workspace, scope_digest=identity.scope_digest,
    )


class ProgressCheckpoints:
    """Request cooperative gates, never snapshot an actively writing provider."""

    def __init__(self, channel, max_tool_calls: int):
        self.channel = channel
        self.policy = DelegateProgressPolicy(max_tool_calls)
        self.last_tools = 0
        self.last_warning = None

    def observe(self, tools: int, remaining_seconds: float | None = None) -> None:
        if type(tools) is not int or tools < 0:
            return
        self.last_tools = max(self.last_tools, tools)
        reason = self.policy.due(self.last_tools, remaining_seconds)
        if reason is None:
            return
        try:
            state = self.channel.store.snapshot(self.channel.binding)
            if state["state"] != "running" or state.get("expired"):
                return
            if reason == "early_review" and state.get("checkpoint"):
                self.policy.acknowledge(reason)
                return
            self.channel.store.command(
                self.channel.binding, action="request_checkpoint",
                command_id="progress-" + reason, expected_revision=state["revision"],
                instruction=reason + ": publish completed work, exact remaining step and blocker. "
                "Use supervisor_checkpoint; scope and execution deadline remain unchanged. "
                "Tool-call counts are advisory. Do not repeat supplied audit.",
            )
            self.policy.acknowledge(reason)
        except SupervisionError as exc:
            if exc.code not in {"supervisor_stale_revision", "supervisor_checkpoint_pending"}:
                self.last_warning = exc.code
        except (OSError, ValueError):
            self.last_warning = "progress_checkpoint_unavailable"

    def snapshot(self) -> dict:
        return {**self.policy.snapshot(), "observed_tool_calls": self.last_tools,
                "delivery": "cooperative_request", "native_interception": False,
                "warning": self.last_warning}
