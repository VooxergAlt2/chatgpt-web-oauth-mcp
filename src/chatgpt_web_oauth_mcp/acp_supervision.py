"""ACP identity adapter for the provider-neutral supervision contract."""
from .acp_session_store import AcpSessionIdentity
from .supervision import AgentEndpoint, SupervisorBinding


def supervisor_binding(identity: AcpSessionIdentity, reference: str, turn_id: str) -> SupervisorBinding:
    return SupervisorBinding(
        owner=identity.owner, execution_id=turn_id, session_ref=reference,
        endpoint=AgentEndpoint(provider="google", transport="acp", account_id=identity.account,
                               model=identity.model, reasoning=identity.thinking),
        workspace=identity.workspace, scope_digest=identity.scope_digest,
    )
