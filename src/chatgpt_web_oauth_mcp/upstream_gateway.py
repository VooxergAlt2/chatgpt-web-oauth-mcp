"""Opt-in FastMCP proxy for the Windows/Revit QA gateway.

This only composes MCP tools; Linux code/build/jobs remain owned by Opss.
There is no fallback through Windows-MCP PowerShell for Revit API operations.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy


def gateway_transport(*, url: str, token: str, ca_cert: str | None = None):
    """Reject insecure remote URLs and credentials embedded in the URL."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Windows/Revit gateway URL must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError("Windows/Revit gateway URL may not contain credentials or query")
    if parsed.path.rstrip("/") != "/mcp":
        raise ValueError("Windows/Revit gateway URL must end with /mcp")
    if not token or len(token.strip()) < 32 or token != token.strip():
        raise ValueError("Windows/Revit gateway needs a 32+ character bearer token")
    if parsed.scheme == "http":
        try:
            is_loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise ValueError("Remote Windows/Revit gateway must use HTTPS")
    if ca_cert:
        if parsed.scheme != "https":
            raise ValueError("CA certificate requires HTTPS")
        if not Path(ca_cert).is_file():
            raise FileNotFoundError("Windows/Revit gateway CA certificate unavailable")
    return StreamableHttpTransport(
        url=url,
        headers={"Authorization": f"Bearer {token}"},
        verify=ca_cert or True,
    )


def mount_gateway_from_env(server, *, environ=None) -> bool:
    """Mount only when configured; no VM connection during Opss import."""
    values = os.environ if environ is None else environ
    url = (values.get("OPSS_WINVM_GATEWAY_URL") or "").strip()
    if not url:
        return False
    token = values.get("OPSS_WINVM_GATEWAY_TOKEN") or ""
    ca_cert = values.get("OPSS_WINVM_GATEWAY_CA_CERT") or None
    transport = gateway_transport(url=url, token=token, ca_cert=ca_cert)
    server.mount(create_proxy(transport), namespace="winvm")
    return True
