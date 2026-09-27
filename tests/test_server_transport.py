from __future__ import annotations

import contextlib
import base64
import hashlib
import json
import logging
import secrets
import socket
import stat
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware as StarletteMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from chatgpt_web_oauth_mcp import server
from chatgpt_web_oauth_mcp.http_compat import (
    DEBUG_REQUEST_CAPTURE_MAX_BYTES,
    MCPDebugLoggingMiddleware,
    MCPSessionTrackingMiddleware,
    OAUTH_REQUEST_BODY_MAX_BYTES,
    OAUTH_REQUEST_MAX_FIELDS,
    OAuthRequestBodyTooLarge,
    _read_bounded_request_body,
    _expected_request_deadline,
)
from chatgpt_web_oauth_mcp.server import build_http_app


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _running_server(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(server, "AUTH_TOKEN", "")
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
    app = server.build_http_app()
    port = _find_free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="on",
    )
    uvicorn_server = uvicorn.Server(config)
    uvicorn_server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    ready = False
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                ready = True
                break
        time.sleep(0.05)
    if not ready:
        raise AssertionError("Timed out waiting for test MCP server to start.")

    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "uvicorn test server did not shut down cleanly"


def test_run_command_default_deadline_uses_configured_command_timeout() -> None:
    assert _expected_request_deadline(
        started_at=100.0,
        tool="run_command",
        arguments=[{"command": "sleep 1"}],
        default_stall_seconds=180,
        default_command_timeout_seconds=300,
    ) == 430.0
    assert _expected_request_deadline(
        started_at=100.0,
        tool="run_command",
        arguments=[{"command": "sleep 1", "timeout": 900}],
        default_stall_seconds=180,
        default_command_timeout_seconds=300,
    ) == 1030.0
    assert _expected_request_deadline(
        started_at=100.0,
        tool="search",
        arguments=[{"query": "TODO"}],
        default_stall_seconds=180,
        default_command_timeout_seconds=300,
    ) == 280.0
    assert _expected_request_deadline(
        started_at=100.0,
        tool="run_command,search",
        arguments=[{"command": "sleep 1"}, {"query": "TODO"}],
        default_stall_seconds=180,
        default_command_timeout_seconds=300,
    ) == 430.0
    assert _expected_request_deadline(
        started_at=100.0,
        tool="run_command",
        arguments=[{"command": "sleep 1"}],
        default_stall_seconds=60,
        default_command_timeout_seconds=300,
        command_timeout_cap_seconds=105,
    ) == 235.0


def test_session_tracking_disconnect_calls_foreground_cancel_once() -> None:
    cancelled: list[str] = []
    payload = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
        b'{"name":"run_command","arguments":{"command":"sleep 30"}}}'
    )
    messages = iter(
        [
            {"type": "http.request", "body": payload, "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    async def send(_message):
        return None

    async def app(_scope, wrapped_receive, _send):
        await wrapped_receive()
        await wrapped_receive()

    middleware = MCPSessionTrackingMiddleware(
        app,
        mcp_path="/mcp",
        default_request_stall_seconds=180,
        default_command_timeout_seconds=300,
        cancel_foreground_owner=cancelled.append,
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"x-openai-session", b"disconnect-test")],
    }

    anyio.run(middleware, scope, receive, send)
    assert len(cancelled) == 1
    assert cancelled[0]


def test_real_mcp_disconnect_cleans_foreground_process(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    from chatgpt_web_oauth_mcp.shell import ForegroundProcessRegistry

    foreground_registry = ForegroundProcessRegistry()
    monkeypatch.setattr(server, "foreground_process_registry", foreground_registry)

    with _running_server(tmp_path, monkeypatch) as url:

        async def scenario() -> None:
            base_headers = {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": "2025-06-18",
                "X-OpenAI-Session": "raw-disconnect-test",
            }
            initialize = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "disconnect-test", "version": "1"},
                },
            }
            async with httpx.AsyncClient(timeout=10.0, headers=base_headers) as setup:
                response = await setup.post(url, json=initialize)
                assert response.status_code == 200
                session_id = response.headers.get("mcp-session-id")
                assert session_id
                initialized = {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }
                initialized_response = await setup.post(
                    url,
                    headers={"mcp-session-id": session_id},
                    json=initialized,
                )
                assert initialized_response.status_code == 202

            tool_call = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "run_command",
                    "arguments": {
                        "command": "python3 -c 'import time; time.sleep(30)'",
                        "cwd": str(tmp_path),
                        "timeout": 30,
                    },
                },
            }
            parsed = urlparse(url)
            assert parsed.hostname is not None
            assert parsed.port is not None
            _reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
            body = json.dumps(tool_call, separators=(",", ":")).encode("utf-8")
            raw_request = (
                f"POST {parsed.path} HTTP/1.1\r\n"
                f"Host: {parsed.hostname}:{parsed.port}\r\n"
                "Accept: application/json, text/event-stream\r\n"
                "Content-Type: application/json\r\n"
                "MCP-Protocol-Version: 2025-06-18\r\n"
                f"Mcp-Session-Id: {session_id}\r\n"
                "X-OpenAI-Session: raw-disconnect-test\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: keep-alive\r\n"
                "\r\n"
            ).encode("ascii") + body
            writer.write(raw_request)
            await writer.drain()

            deadline = time.monotonic() + 3
            while foreground_registry.active_count() != 1 and time.monotonic() < deadline:
                await anyio.sleep(0.02)
            assert foreground_registry.active_count() == 1

            writer.close()
            await writer.wait_closed()

            deadline = time.monotonic() + 3
            while foreground_registry.active_count() and time.monotonic() < deadline:
                await anyio.sleep(0.05)
            assert foreground_registry.active_count() == 0

        anyio.run(scenario)


def test_http_app_uses_streamable_http_transport() -> None:
    app = build_http_app()

    assert app.state.transport_type == "streamable-http"
    assert app.state.path == "/mcp"


def test_http_app_supports_head_on_mcp(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.head("/mcp")

    assert response.status_code == 204
    assert response.headers["allow"] == "GET, POST, DELETE, HEAD, OPTIONS"


def test_http_app_supports_options_preflight(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.options(
            "/mcp",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "POST",
            },
        )

    # Preflights must succeed even without a bearer token.
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"



def test_internal_health_uses_dedicated_token(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "mcp-secret")
    monkeypatch.setattr(server, "HEALTH_TOKEN", "health-secret")
    monkeypatch.setattr(
        server,
        "_current_health_snapshot",
        lambda: {"success": True, "state": "idle", "summary": {"sessions": 0}},
    )
    app = build_http_app()

    with TestClient(app) as client:
        missing = client.get("/internal/health")
        wrong = client.get(
            "/internal/health",
            headers={"X-Ops-Health-Token": "wrong"},
        )
        ok = client.get(
            "/internal/health",
            headers={"X-Ops-Health-Token": "health-secret"},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ok.status_code == 200
    assert ok.json()["state"] == "idle"
    assert ok.headers["cache-control"] == "no-store"


def test_internal_health_is_disabled_without_health_token(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "mcp-secret")
    monkeypatch.setattr(server, "HEALTH_TOKEN", "")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/internal/health")

    assert response.status_code == 503
    assert response.json()["error"] == "health_disabled"


def test_http_app_treats_root_as_mcp_compat_alias(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "")
    app = build_http_app()

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    with TestClient(app) as client:
        response = client.post(
            "/",
            json=initialize,
            headers={"Accept": "application/json, text/event-stream"},
        )

    assert response.status_code == 200
    assert "protocolVersion" in response.text

def test_http_app_exposes_server_card(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/.well-known/mcp.json")

    assert response.status_code == 200
    body = response.json()
    assert body["transport"] == {"type": "streamable-http", "endpoint": "/mcp"}
    assert body["authentication"] == {"required": True, "schemes": ["bearer"]}
    # Discovery should not depend on server card revision fields that are not in the spec.
    assert "version" not in body


def test_http_app_server_card_reflects_disabled_auth(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/.well-known/mcp.json")

    assert response.status_code == 200
    assert response.json()["authentication"] == {"required": False, "schemes": []}


def test_http_app_returns_server_card_for_plain_get_mcp(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/mcp", headers={"Accept": "*/*"})

    assert response.status_code == 200
    assert response.json()["transport"]["endpoint"] == "/mcp"


def test_http_app_rejects_unauthenticated_sse_get(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/mcp", headers={"Accept": "text/event-stream"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"].lower().startswith("bearer")


def test_http_app_rejects_unauthenticated_plain_get(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/mcp", headers={"Accept": "*/*"})

    assert response.status_code == 401


def test_http_app_rejects_unauthenticated_messages_post(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.post(
            "/messages/",
            params={"session_id": "anything"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )

    assert response.status_code == 401


def test_http_app_allows_discovery_without_token(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/.well-known/mcp.json")

    assert response.status_code == 200


def test_http_app_allows_unauthenticated_head_probe(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.head("/mcp")

    assert response.status_code == 204
    assert response.headers["allow"] == "GET, POST, DELETE, HEAD, OPTIONS"


def test_http_app_accepts_valid_bearer_on_head(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.head("/mcp", headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 204


def test_http_app_does_not_require_auth_for_oauth_discovery_probe(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 404


def test_shared_token_mode_rejects_when_auth_token_is_empty(monkeypatch) -> None:
    # Guard against an empty/misconfigured AUTH_TOKEN silently allowing requests
    # that omit the Authorization header (both sides would compare equal as "").
    monkeypatch.setattr(server, "AUTH_MODE", "shared_token")
    monkeypatch.setattr(server, "AUTH_TOKEN", "")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})

    assert response.status_code == 401


def test_oauth_mode_requires_public_base_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "OAUTH_LOGIN_TOKEN", "oauth-login-secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)

    with pytest.raises(ValueError, match="PUBLIC_BASE_URL is required"):
        build_http_app()


def test_oauth_mode_requires_dedicated_login_token(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "shared-token")
    monkeypatch.setattr(server, "OAUTH_LOGIN_TOKEN", "")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)

    with pytest.raises(ValueError, match="OAUTH_LOGIN_TOKEN is required"):
        build_http_app()


def test_oauth_metadata_uses_configured_public_base_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "OAUTH_LOGIN_TOKEN", "oauth-login-secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get(
            "/.well-known/oauth-authorization-server",
            headers={
                "Host": "attacker.example",
                "X-Forwarded-Host": "also-attacker.example",
                "X-Forwarded-Proto": "http",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["issuer"] == "https://mcp.example.test"
    assert body["authorization_endpoint"] == "https://mcp.example.test/oauth/authorize"


def test_oauth_mode_rejects_shared_auth_token_bearer(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "shared-token")
    monkeypatch.setattr(server, "OAUTH_LOGIN_TOKEN", "oauth-login-secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get(
            "/mcp",
            headers={
                "Accept": "text/event-stream",
                "Authorization": "Bearer shared-token",
            },
        )

    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]


def test_oauth_register_keeps_client_store_bounded(monkeypatch, tmp_path) -> None:
    from chatgpt_web_oauth_mcp.oauth import MAX_REGISTERED_CLIENTS

    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "OAUTH_LOGIN_TOKEN", "oauth-login-secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    payload = {
        "client_name": "ChatGPT",
        "redirect_uris": ["https://chat.openai.com/aip/callback"],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    with TestClient(app) as client:
        for _ in range(MAX_REGISTERED_CLIENTS):
            ok = client.post("/oauth/register", json=payload)
            assert ok.status_code == 201
        replacement = client.post("/oauth/register", json=payload)

    assert replacement.status_code == 201
    store = json.loads((tmp_path / "oauth.json").read_text(encoding="utf-8"))
    assert len(store["clients"]) == MAX_REGISTERED_CLIENTS
    assert replacement.json()["client_id"] in store["clients"]


def test_oauth_store_file_permissions_are_locked_down(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "OAUTH_LOGIN_TOKEN", "oauth-login-secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={
                "client_name": "ChatGPT",
                "redirect_uris": ["https://chat.openai.com/aip/callback"],
            },
        )

    assert registration.status_code == 201
    oauth_path = tmp_path / "oauth.json"
    assert oauth_path.exists()
    file_mode = stat.S_IMODE(oauth_path.stat().st_mode)
    assert file_mode == 0o600, f"oauth.json mode={oct(file_mode)} (expected 0o600)"


def test_http_app_exposes_minimal_oauth_metadata(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "OAUTH_LOGIN_TOKEN", "oauth-login-secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    with TestClient(app) as client:
        resource = client.get("/.well-known/oauth-protected-resource/mcp")
        issuer = client.get("/.well-known/oauth-authorization-server")

    assert resource.status_code == 200
    assert resource.json()["resource"] == "https://mcp.example.test/mcp"
    assert resource.json()["authorization_servers"] == ["https://mcp.example.test"]
    assert resource.json()["scopes_supported"] == ["local-ops"]
    assert resource.json()["bearer_methods_supported"] == ["header"]

    assert issuer.status_code == 200
    issuer_body = issuer.json()
    assert issuer_body["issuer"] == "https://mcp.example.test"
    assert issuer_body["authorization_endpoint"] == "https://mcp.example.test/oauth/authorize"
    assert issuer_body["token_endpoint"] == "https://mcp.example.test/oauth/token"
    assert issuer_body["registration_endpoint"] == "https://mcp.example.test/oauth/register"
    assert issuer_body["grant_types_supported"] == [
        "authorization_code",
        "refresh_token",
    ]
    assert issuer_body["code_challenge_methods_supported"] == ["S256"]


def test_http_app_oauth_challenge_advertises_resource_metadata(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "OAUTH_LOGIN_TOKEN", "oauth-login-secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/mcp", headers={"Accept": "text/event-stream"})

    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    expected_metadata = 'resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource/mcp"'
    assert expected_metadata in challenge
    assert 'scope="local-ops"' in challenge


def test_oauth_authorize_page_requires_explicit_action_and_is_not_cacheable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "OAUTH_LOGIN_TOKEN", "oauth-login-secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get(
            "/oauth/authorize",
            params={
                "client_id": "mcp_client_test",
                "redirect_uri": "https://chat.openai.com/aip/callback",
                "response_type": "code",
                "state": "state-123",
                "scope": "local-ops",
                "resource": "https://mcp.example.test/mcp",
                "code_challenge": "challenge-123",
                "code_challenge_method": "S256",
            },
        )

    assert response.status_code == 200
    html = response.text
    assert 'id="oauth-authorize-form"' in html
    assert 'id="oauth-login-token"' in html
    assert 'autocomplete="off"' in html
    assert "<script" not in html
    assert "localStorage" not in html
    assert "requestSubmit" not in html
    assert "form.submit()" not in html
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_oauth_register_rejects_oversized_request_body(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "OAUTH_LOGIN_TOKEN", "oauth-login-secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    with TestClient(app) as client:
        response = client.post(
            "/oauth/register",
            content=b"x" * (OAUTH_REQUEST_BODY_MAX_BYTES + 1),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 413
    assert response.json()["error"] == "invalid_request"
    assert "exceeds" in response.json()["error_description"]


def test_oauth_bounded_reader_rejects_streamed_body_without_content_length() -> None:
    chunks = [
        b"a" * (OAUTH_REQUEST_BODY_MAX_BYTES // 2),
        b"b" * (OAUTH_REQUEST_BODY_MAX_BYTES // 2 + 1),
    ]

    async def scenario() -> None:
        messages = iter(
            [
                {"type": "http.request", "body": chunks[0], "more_body": True},
                {"type": "http.request", "body": chunks[1], "more_body": False},
            ]
        )

        async def receive():
            return next(messages)

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": "/oauth/register",
                "raw_path": b"/oauth/register",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("mcp.example.test", 443),
            },
            receive,
        )
        try:
            await _read_bounded_request_body(request)
        except OAuthRequestBodyTooLarge:
            return
        raise AssertionError("streamed oversized OAuth body must be rejected")

    anyio.run(scenario)


def test_oauth_register_rejects_malformed_json(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "OAUTH_LOGIN_TOKEN", "oauth-login-secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    with TestClient(app) as client:
        response = client.post(
            "/oauth/register",
            content=b"{",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_request",
        "error_description": "Invalid JSON request body.",
    }


def test_oauth_token_rejects_too_many_form_fields(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "OAUTH_LOGIN_TOKEN", "oauth-login-secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()
    body = "&".join(f"field-{index}=x" for index in range(OAUTH_REQUEST_MAX_FIELDS + 1))

    with TestClient(app) as client:
        response = client.post(
            "/oauth/token",
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_request",
        "error_description": "OAuth form request has too many fields.",
    }


def test_http_app_oauth_dcr_pkce_flow_allows_mcp_access(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "")
    monkeypatch.setattr(server, "OAUTH_LOGIN_TOKEN", "oauth-login-secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    redirect_uri = "https://chat.openai.com/aip/callback"

    with TestClient(app, follow_redirects=False) as client:
        registration = client.post(
            "/oauth/register",
            json={
                "client_name": "ChatGPT",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert registration.status_code == 201
        assert registration.json()["grant_types"] == ["authorization_code"]
        client_id = registration.json()["client_id"]

        authorize = client.post(
            "/oauth/authorize",
            data={
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "state": "state-123",
                "scope": "local-ops",
                "resource": "https://mcp.example.test/mcp",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "login_token": "oauth-login-secret",
            },
        )
        assert authorize.status_code == 303
        parsed_redirect = urlparse(authorize.headers["location"])
        params = parse_qs(parsed_redirect.query)
        assert parsed_redirect.geturl().startswith(redirect_uri)
        assert params["state"] == ["state-123"]
        code = params["code"][0]

        token = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": "https://mcp.example.test/mcp",
            },
        )
        assert token.status_code == 200
        access_token = token.json()["access_token"]
        assert token.json()["token_type"] == "Bearer"
        assert "refresh_token" not in token.json()
        assert token.headers["cache-control"] == "no-store"
        assert token.headers["pragma"] == "no-cache"

        response = client.get(
            "/mcp",
            headers={"Accept": "*/*", "Authorization": f"Bearer {access_token}"},
        )
        assert response.status_code == 200
        assert response.json()["transport"]["endpoint"] == "/mcp"


def test_http_app_oauth_refresh_token_rotates_without_reauthorization(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "")
    monkeypatch.setattr(server, "OAUTH_LOGIN_TOKEN", "oauth-login-secret")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    redirect_uri = "https://chat.openai.com/aip/callback"

    with TestClient(app, follow_redirects=False) as client:
        registration = client.post(
            "/oauth/register",
            json={
                "client_name": "ChatGPT",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert registration.status_code == 201
        client_id = registration.json()["client_id"]

        authorize = client.post(
            "/oauth/authorize",
            data={
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "local-ops",
                "resource": "https://mcp.example.test/mcp",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "login_token": "oauth-login-secret",
            },
        )
        code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
        initial = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": "https://mcp.example.test/mcp",
            },
        )
        assert initial.status_code == 200
        old_refresh = initial.json()["refresh_token"]

        refreshed = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": old_refresh,
                "client_id": client_id,
                "resource": "https://mcp.example.test/mcp",
            },
        )
        assert refreshed.status_code == 200
        assert refreshed.headers["cache-control"] == "no-store"
        assert refreshed.headers["pragma"] == "no-cache"
        assert refreshed.json()["refresh_token"] != old_refresh

        access = client.get(
            "/mcp",
            headers={
                "Accept": "*/*",
                "Authorization": f"Bearer {refreshed.json()['access_token']}",
            },
        )
        assert access.status_code == 200

        reused = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": old_refresh,
                "client_id": client_id,
                "resource": "https://mcp.example.test/mcp",
            },
        )
        assert reused.status_code == 400
        assert reused.json()["error"] == "invalid_grant"

        missing_grant = client.post("/oauth/token", data={})
        assert missing_grant.status_code == 400
        assert missing_grant.json()["error"] == "invalid_request"

        unsupported = client.post(
            "/oauth/token",
            data={"grant_type": "client_credentials"},
        )
        assert unsupported.status_code == 400
        assert unsupported.json()["error"] == "unsupported_grant_type"


def test_http_app_supports_legacy_sse_get_on_mcp(tmp_path, monkeypatch) -> None:
    with _running_server(tmp_path, monkeypatch) as url:
        async def scenario() -> None:
            async with httpx.AsyncClient(timeout=5.0) as client:
                async with client.stream(
                    "GET",
                    url,
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("text/event-stream")

        anyio.run(scenario)


def test_debug_logging_middleware_logs_rpc_method_and_preserves_body(caplog) -> None:
    async def echo_json(request) -> JSONResponse:
        return JSONResponse(await request.json())

    app = Starlette(
        routes=[Route("/mcp", endpoint=echo_json, methods=["POST"])],
        middleware=[
            StarletteMiddleware(
                MCPDebugLoggingMiddleware,
                get_debug_enabled=lambda: True,
                mcp_path="/mcp",
            )
        ],
    )

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search", "arguments": {"mode": "text", "query": "TODO"}},
    }

    caplog.set_level(logging.INFO, logger="chatgpt_web_oauth_mcp.mcp_debug")
    with TestClient(app) as client:
        response = client.post("/mcp", json=payload, headers={"mcp-session-id": "sess-123"})

    assert response.status_code == 200
    assert response.json() == payload
    messages = [record.message for record in caplog.records if record.name == "chatgpt_web_oauth_mcp.mcp_debug"]
    assert any("phase=request" in message and '"method":"tools/call"' in message for message in messages)
    assert any('"tool":"search"' in message for message in messages)
    assert any('"tool_args":"{\\"mode\\":\\"text\\",\\"query\\":\\"TODO\\"}"' in message for message in messages)
    assert any("phase=response_end" in message and "status=200" in message for message in messages)


def test_debug_logging_middleware_bounds_body_capture_and_preserves_full_request(caplog) -> None:
    async def echo_size(request) -> JSONResponse:
        body = await request.body()
        return JSONResponse({"bytes": len(body)})

    app = Starlette(
        routes=[Route("/mcp", endpoint=echo_size, methods=["POST"])],
        middleware=[
            StarletteMiddleware(
                MCPDebugLoggingMiddleware,
                get_debug_enabled=lambda: True,
                mcp_path="/mcp",
            )
        ],
    )
    payload = b"x" * (DEBUG_REQUEST_CAPTURE_MAX_BYTES + 4096)

    caplog.set_level(logging.INFO, logger="chatgpt_web_oauth_mcp.mcp_debug")
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            content=payload,
            headers={"Content-Type": "application/octet-stream"},
        )

    assert response.status_code == 200
    assert response.json() == {"bytes": len(payload)}
    messages = [
        record.message
        for record in caplog.records
        if record.name == "chatgpt_web_oauth_mcp.mcp_debug"
    ]
    request_log = next(message for message in messages if "phase=request" in message)
    assert f"body_bytes={len(payload)}" in request_log
    assert '"kind":"truncated"' in request_log
    assert f'"captured_bytes":{DEBUG_REQUEST_CAPTURE_MAX_BYTES}' in request_log


def test_http_app_debug_logging_does_not_break_streamable_http_initialize(tmp_path, monkeypatch) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "DEBUG_MCP_LOGGING", True)

    with _running_server(tmp_path, monkeypatch) as url:

        async def scenario() -> None:
            headers = {"Authorization": "Bearer secret-token"}
            async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
                async with streamable_http_client(url, http_client=client) as (
                    read_stream,
                    write_stream,
                    _,
                ):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()

        anyio.run(scenario)
