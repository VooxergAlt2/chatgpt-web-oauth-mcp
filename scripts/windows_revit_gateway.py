"""Compose an existing Windows-MCP and local Revit-MCP behind one MCP endpoint.

Run this script from an interactive Windows user session after Windows-MCP is
installed and listening on loopback. Only the Revit stdio child is spawned.
No Windows-MCP source modifications or Revit plugin changes are needed.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.auth import StaticTokenVerifier

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
try:
    from windows_operation_journal import OperationJournal
except ImportError:
    from scripts.windows_operation_journal import OperationJournal

# The current Revit QA admission-gate contract permits exactly these methods
# without a privileged in-process Full grant. Do not assume all 124+ methods
# are safe just because their names contain "get".
SAFE_REVIT_TOOLS = frozenset({
    "get_project_info",
    "get_phases",
    "get_selected_elements",
    # These are internal endpoints, not raw model-visible escape hatches.
    # Opss exposes owner-bound virtual workspace operations instead.
    "workspace_discover",
    "workspace_prepare",
    "workspace_dispatch",
})


@dataclass(frozen=True)
class GatewayConfig:
    windows_url: str
    node_executable: str
    revit_server_js: Path
    host: str = "127.0.0.1"
    port: int = 8840
    auth_token: str | None = None
    windows_token: str | None = None
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None
    revit_tools: frozenset[str] = SAFE_REVIT_TOOLS
    operation_state_dir: Path | None = None

    def validate(self, *, validate_files: bool = True) -> None:
        upstream = urlsplit(self.windows_url)
        if upstream.scheme != "http" or upstream.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Windows-MCP upstream must be loopback HTTP; never expose it directly")
        if upstream.username or upstream.password or upstream.fragment or upstream.query:
            raise ValueError("Windows-MCP URL cannot contain credentials, query or fragment")
        if upstream.path.rstrip("/") != "/mcp":
            raise ValueError("Windows-MCP URL must point to /mcp")
        if not self.node_executable.strip():
            raise ValueError("Node executable is required")
        if not (1 <= self.port <= 65535):
            raise ValueError("Gateway port must be in 1..65535")
        try:
            is_local = self.host == "localhost" or ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            raise ValueError("Bind host must be a literal IPv4/IPv6 address or localhost") from None
        if bool(self.tls_certfile) != bool(self.tls_keyfile):
            raise ValueError("TLS certificate and key must be supplied together")
        if not is_local:
            if not self.auth_token or len(self.auth_token) < 32 or not self.tls_certfile:
                raise ValueError("Non-loopback gateway requires a 32+ character token and TLS")
        if not self.revit_tools or not self.revit_tools.issubset(SAFE_REVIT_TOOLS):
            raise ValueError("Only pre-approved read-only Revit QA tools may be exposed")
        if self.operation_state_dir is not None:
            if str(self.operation_state_dir).startswith(("//", "\\\\")):
                raise ValueError("Operation state dir cannot be a network share (NAS)")
        if validate_files:
            if not self.revit_server_js.is_file():
                raise FileNotFoundError(f"Revit MCP Node entrypoint not found: {self.revit_server_js}")
            if self.tls_certfile and not self.tls_certfile.is_file():
                raise FileNotFoundError("TLS certificate not found")
            if self.tls_keyfile and not self.tls_keyfile.is_file():
                raise FileNotFoundError("TLS private key not found")


def build_gateway(
    config: GatewayConfig,
    *,
    windows_target=None,
    revit_target=None,
    validate_files: bool = True,
) -> FastMCP:
    """Build but do not start either MCP backend or network listener.

    windows_target/revit_target enable hermetic contract tests without Windows,
    Revit, Node or network. Production uses actual MCP transports.
    """
    config.validate(validate_files=validate_files)
    windows_headers = (
        # Proxy forwarding uses lowercase "authorization" from the incoming
        # MCP request. Use the same key so this upstream credential replaces
        # the gateway client's bearer instead of emitting two case variants.
        {"authorization": f"Bearer {config.windows_token}"}
        if config.windows_token else None
    )
    if windows_target is None:
        windows_target = StreamableHttpTransport(config.windows_url, headers=windows_headers)
    if revit_target is None:
        # MCP stdio inherits only a small OS allowlist. The protected Node
        # grant receiver needs this gateway's selected key and Revit paths;
        # never forward the entire parent environment or Windows upstream key.
        child_env = {
            name: value for name in (
                "APPDATA", "LOCALAPPDATA", "REVIT_WORKSPACES_ROOT",
                "REVIT_INSTANCES_DIR", "REVIT_INSTANCES_ROOT",
            ) if (value := os.environ.get(name)) is not None
        }
        if config.auth_token:
            child_env["WINDOWS_REVIT_GATEWAY_TOKEN"] = config.auth_token
        revit_target = StdioTransport(
            config.node_executable,
            [str(config.revit_server_js)],
            env=child_env,
            cwd=str(config.revit_server_js.parent.parent),
            keep_alive=True,
        )

    auth = None
    if config.auth_token:
        auth = StaticTokenVerifier(
            tokens={config.auth_token: {"client_id": "opss-gip-core", "scopes": ["qa"]}}
        )
    gateway = FastMCP(
        "Windows Revit QA",
        instructions=(
            "Windows tools control the QA desktop, and may provide shell access. "
            "The Revit namespace exposes legacy reads and native workspace dispatch. "
            "Workspace dispatch allows full QA commands only through the native "
            "process queue and explicit document context. This is not a C# sandbox. "
            "This tool filter is not an OS security boundary: Windows shell "
            "access can bypass it. Use disposable QA models only."
        ),
        auth=auth,
    )
    windows_proxy = create_proxy(windows_target)
    # The installed Revit Node server uses MCP SDK v1 (2025 handshake).
    # Without an explicit era, the composite FastMCP 4 server may forward
    # modern discovery and reject tools/list missing resultType/cacheScope/
    # ttlMs. Keep the backend in its native handshake era.
    revit_proxy = create_proxy(revit_target, mode="legacy")
    revit_proxy.enable(names=set(config.revit_tools), components={"tool"}, only=True)
    gateway.mount(windows_proxy, namespace="windows")
    gateway.mount(revit_proxy, namespace="revit")

    if config.operation_state_dir is not None:
        journal = OperationJournal(
            config.operation_state_dir,
            windows_target=windows_target,
        )
        setattr(gateway, "operation_journal", journal)

        @gateway.tool(name="windows_operation_submit")
        async def windows_operation_submit(
            operation_id: str,
            name: str,
            arguments: dict | None = None,
        ) -> dict:
            return await journal.submit(operation_id, name, arguments)

        @gateway.tool(name="windows_operation_status")
        async def windows_operation_status(
            operation_id: str,
        ) -> dict:
            return await journal.status(operation_id)

        @gateway.tool(name="windows_operation_reconcile")
        async def windows_operation_reconcile(operation_id: str, request_digest: str) -> dict:
            return await journal.reconcile(operation_id, request_digest)

    return gateway


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Revit + Windows MCP gateway")
    parser.add_argument("--revit-server-js", type=Path, required=True)
    parser.add_argument("--node-executable", default="node")
    parser.add_argument("--windows-url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8840)
    parser.add_argument("--tls-certfile", type=Path)
    parser.add_argument("--tls-keyfile", type=Path)
    parser.add_argument("--operation-state-dir", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)

    default_state_dir = None
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        default_state_dir = Path(local_app_data) / "Astra-Revit-Bridge" / "WindowsMCP" / "operations"
    else:
        default_state_dir = Path.home() / "AppData" / "Local" / "Astra-Revit-Bridge" / "WindowsMCP" / "operations"

    cfg = GatewayConfig(
        windows_url=args.windows_url,
        node_executable=args.node_executable,
        revit_server_js=args.revit_server_js,
        host=args.host,
        port=args.port,
        auth_token=os.getenv("WINDOWS_REVIT_GATEWAY_TOKEN") or None,
        windows_token=os.getenv("WINDOWS_MCP_AUTH_KEY") or None,
        tls_certfile=args.tls_certfile,
        tls_keyfile=args.tls_keyfile,
        operation_state_dir=args.operation_state_dir or default_state_dir,
    )
    try:
        cfg.validate()
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if args.check_only:
        print("GATEWAY_CONFIGURATION_PASS: Windows-MCP and Revit-MCP runtime not started")
        return 0

    gateway = build_gateway(cfg)
    uvicorn_config = (
        {"ssl_certfile": str(cfg.tls_certfile), "ssl_keyfile": str(cfg.tls_keyfile)}
        if cfg.tls_certfile else None
    )
    gateway.run(
        transport="streamable-http",
        host=cfg.host,
        port=cfg.port,
        show_banner=False,
        stateless_http=True,
        uvicorn_config=uvicorn_config,
        # FastMCP 4 only auto-allows a bind address when it is loopback.
        # An explicit LAN bind otherwise yields HTTP 421 on the Host header.
        allowed_hosts=[cfg.host],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
