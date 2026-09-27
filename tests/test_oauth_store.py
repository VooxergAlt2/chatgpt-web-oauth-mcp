from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import chatgpt_web_oauth_mcp.oauth as oauth_module
from chatgpt_web_oauth_mcp.oauth import (
    MAX_ACCESS_TOKENS,
    MAX_AUTHORIZATION_CODES,
    MAX_REGISTERED_CLIENTS,
    OAuthManager,
    OAuthRuntimeConfig,
    _is_allowed_redirect_uri,
    _pkce_s256,
)


BASE_URL = "https://mcp.example.test"


@pytest.mark.parametrize(
    "uri",
    [
        "https://client.example.test/callback",
        "https://client.example.test:8443/callback?tenant=one",
        "http://localhost/callback",
        "http://localhost:43123/callback",
        "http://127.0.0.1:43123/callback",
        "http://[::1]:43123/callback",
    ],
)
def test_allowed_redirect_uri_accepts_https_and_loopback_http(uri: str) -> None:
    assert _is_allowed_redirect_uri(uri) is True


@pytest.mark.parametrize(
    "uri",
    [
        "http://client.example.test/callback",
        "https://client.example.test/callback#fragment",
        "https://client.example.test/callback#",
        "https://user@client.example.test/callback",
        "https://user:password@client.example.test/callback",
        "https://client.example.test\\@attacker.example/callback",
        " https://client.example.test/callback",
        "https://client.example.test/callback\n",
        "https://client.example.test:bad/callback",
        "https://[::1/callback",
        "/relative/callback",
        "",
    ],
)
def test_allowed_redirect_uri_rejects_ambiguous_or_unsafe_values(uri: str) -> None:
    assert _is_allowed_redirect_uri(uri) is False


def _manager(state_dir: str) -> OAuthManager:
    return OAuthManager(
        OAuthRuntimeConfig(
            auth_mode="oauth",
            auth_token="secret-token",
            public_base_url=BASE_URL,
            state_dir=Path(state_dir),
            oauth_login_token="secret-token",
            oauth_scopes=("local-ops",),
            oauth_token_ttl_seconds=3600,
        ),
        mcp_path="/mcp",
    )


def _register_client_worker(state_dir: str, index: int) -> str:
    registration = _manager(state_dir).register_client(
        {
            "client_name": f"client-{index}",
            "redirect_uris": [f"https://client-{index}.example.test/callback"],
        }
    )
    return str(registration["client_id"])


def _exchange_code_worker(state_dir: str, payload: dict[str, str]) -> tuple[str, str]:
    try:
        token = _manager(state_dir).exchange_code(payload, base_url=BASE_URL)
    except ValueError as exc:
        return "error", str(exc)
    return "ok", str(token["access_token"])


@pytest.mark.skipif(os.name != "posix", reason="Production OAuth store locking is exercised on Linux.")
def test_parallel_client_registration_preserves_all_clients(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    with ProcessPoolExecutor(max_workers=8, mp_context=context) as pool:
        futures = [pool.submit(_register_client_worker, str(tmp_path), index) for index in range(8)]
        client_ids = [future.result(timeout=10) for future in futures]

    store = json.loads((tmp_path / "oauth.json").read_text(encoding="utf-8"))
    assert len(client_ids) == 8
    assert len(set(client_ids)) == 8
    assert set(store["clients"]) == set(client_ids)
    assert not list(tmp_path.glob(".oauth.json.*.tmp"))


@pytest.mark.skipif(os.name != "posix", reason="Production OAuth store locking is exercised on Linux.")
def test_authorization_code_can_be_redeemed_by_only_one_process(tmp_path: Path) -> None:
    manager = _manager(str(tmp_path))
    redirect_uri = "https://client.example.test/callback"
    client_id = str(
        manager.register_client(
            {"client_name": "client", "redirect_uris": [redirect_uri]}
        )["client_id"]
    )
    verifier = "v" * 43
    authorize_url = manager.authorize(
        {
            "login_token": "secret-token",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "local-ops",
            "resource": f"{BASE_URL}/mcp",
            "code_challenge": _pkce_s256(verifier),
            "code_challenge_method": "S256",
        },
        base_url=BASE_URL,
    )
    code = parse_qs(urlparse(authorize_url).query)["code"][0]
    exchange_payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": f"{BASE_URL}/mcp",
    }

    context = multiprocessing.get_context("fork")
    with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
        results = [
            future.result(timeout=10)
            for future in [
                pool.submit(_exchange_code_worker, str(tmp_path), exchange_payload),
                pool.submit(_exchange_code_worker, str(tmp_path), exchange_payload),
            ]
        ]

    assert [status for status, _ in results].count("ok") == 1
    assert [status for status, _ in results].count("error") == 1
    assert any(message == "invalid authorization code" for status, message in results if status == "error")
    store = json.loads((tmp_path / "oauth.json").read_text(encoding="utf-8"))
    assert code not in store["codes"]
    assert len(store["tokens"]) == 1


def test_oauth_store_prunes_expired_and_bounds_ephemeral_records(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = 1_000_000
    monkeypatch.setattr(oauth_module.time, "time", lambda: float(now))
    manager = _manager(str(tmp_path))
    store = {
        "clients": {},
        "codes": {
            "expired-code": {"expires_at": now - 1},
            "malformed-code": "invalid",
            **{
                f"code-{index:04d}": {
                    "created_at": index + 1,
                    "expires_at": now + 300,
                }
                for index in range(MAX_AUTHORIZATION_CODES + 7)
            },
        },
        "tokens": {
            "expired-token": {"expires_at": now - 1},
            "malformed-token": ["invalid"],
            **{
                f"token-{index:04d}": {
                    "created_at": index + 1,
                    "expires_at": now + 3600,
                    "resource": f"{BASE_URL}/mcp",
                    "scope": "local-ops",
                }
                for index in range(MAX_ACCESS_TOKENS + 11)
            },
        },
    }
    (tmp_path / "oauth.json").write_text(json.dumps(store), encoding="utf-8")

    assert manager.verify_access_token("missing-token", base_url=BASE_URL) is False

    persisted = json.loads((tmp_path / "oauth.json").read_text(encoding="utf-8"))
    assert "expired-code" not in persisted["codes"]
    assert "malformed-code" not in persisted["codes"]
    assert "expired-token" not in persisted["tokens"]
    assert "malformed-token" not in persisted["tokens"]
    assert len(persisted["codes"]) == MAX_AUTHORIZATION_CODES
    assert len(persisted["tokens"]) == MAX_ACCESS_TOKENS
    assert "code-0000" not in persisted["codes"]
    assert f"code-{MAX_AUTHORIZATION_CODES + 6:04d}" in persisted["codes"]
    assert "token-0000" not in persisted["tokens"]
    assert f"token-{MAX_ACCESS_TOKENS + 10:04d}" in persisted["tokens"]


def test_new_authorization_code_survives_full_store_with_equal_timestamps(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = 2_000_000
    monkeypatch.setattr(oauth_module.time, "time", lambda: float(now))
    manager = _manager(str(tmp_path))
    redirect_uri = "https://client.example.test/callback"
    client_id = str(
        manager.register_client(
            {"client_name": "client", "redirect_uris": [redirect_uri]}
        )["client_id"]
    )
    store = json.loads((tmp_path / "oauth.json").read_text(encoding="utf-8"))
    store["codes"] = {
        f"existing-code-{index:04d}": {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": "unused",
            "scope": "local-ops",
            "resource": f"{BASE_URL}/mcp",
            "created_at": now,
            "expires_at": now + 300,
        }
        for index in range(MAX_AUTHORIZATION_CODES)
    }
    (tmp_path / "oauth.json").write_text(json.dumps(store), encoding="utf-8")
    verifier = "v" * 43

    authorize_url = manager.authorize(
        {
            "login_token": "secret-token",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "local-ops",
            "resource": f"{BASE_URL}/mcp",
            "code_challenge": _pkce_s256(verifier),
            "code_challenge_method": "S256",
        },
        base_url=BASE_URL,
    )
    issued_code = parse_qs(urlparse(authorize_url).query)["code"][0]

    persisted = json.loads((tmp_path / "oauth.json").read_text(encoding="utf-8"))
    assert len(persisted["codes"]) == MAX_AUTHORIZATION_CODES
    assert issued_code in persisted["codes"]


def test_new_access_token_survives_full_store_with_equal_timestamps(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = 3_000_000
    monkeypatch.setattr(oauth_module.time, "time", lambda: float(now))
    manager = _manager(str(tmp_path))
    redirect_uri = "https://client.example.test/callback"
    client_id = str(
        manager.register_client(
            {"client_name": "client", "redirect_uris": [redirect_uri]}
        )["client_id"]
    )
    verifier = "v" * 43
    authorize_url = manager.authorize(
        {
            "login_token": "secret-token",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "local-ops",
            "resource": f"{BASE_URL}/mcp",
            "code_challenge": _pkce_s256(verifier),
            "code_challenge_method": "S256",
        },
        base_url=BASE_URL,
    )
    code = parse_qs(urlparse(authorize_url).query)["code"][0]
    store = json.loads((tmp_path / "oauth.json").read_text(encoding="utf-8"))
    store["tokens"] = {
        f"existing-token-{index:04d}": {
            "client_id": client_id,
            "scope": "local-ops",
            "resource": f"{BASE_URL}/mcp",
            "created_at": now,
            "expires_at": now + 3600,
        }
        for index in range(MAX_ACCESS_TOKENS)
    }
    (tmp_path / "oauth.json").write_text(json.dumps(store), encoding="utf-8")

    token = manager.exchange_code(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": f"{BASE_URL}/mcp",
        },
        base_url=BASE_URL,
    )
    issued_token = str(token["access_token"])

    persisted = json.loads((tmp_path / "oauth.json").read_text(encoding="utf-8"))
    assert len(persisted["tokens"]) == MAX_ACCESS_TOKENS
    assert issued_token in persisted["tokens"]


def test_client_registration_evicts_oldest_unreferenced_client(tmp_path: Path) -> None:
    manager = _manager(str(tmp_path))
    clients = {
        f"client-{index:03d}": {
            "client_id": f"client-{index:03d}",
            "client_name": f"client-{index:03d}",
            "redirect_uris": [f"https://client-{index}.example.test/callback"],
            "created_at": index + 1,
        }
        for index in range(MAX_REGISTERED_CLIENTS)
    }
    protected_id = "client-000"
    store = {
        "clients": clients,
        "codes": {},
        "tokens": {
            "active-token": {
                "client_id": protected_id,
                "scope": "local-ops",
                "resource": f"{BASE_URL}/mcp",
                "created_at": 1,
                "expires_at": 9_999_999_999,
            }
        },
    }
    (tmp_path / "oauth.json").write_text(json.dumps(store), encoding="utf-8")

    registration = manager.register_client(
        {
            "client_name": "new-client",
            "redirect_uris": ["https://new-client.example.test/callback"],
        }
    )

    persisted = json.loads((tmp_path / "oauth.json").read_text(encoding="utf-8"))
    assert len(persisted["clients"]) == MAX_REGISTERED_CLIENTS
    assert protected_id in persisted["clients"]
    assert "client-001" not in persisted["clients"]
    assert registration["client_id"] in persisted["clients"]


def test_client_registration_fails_when_all_clients_are_referenced(tmp_path: Path) -> None:
    manager = _manager(str(tmp_path))
    clients = {
        f"client-{index:03d}": {
            "client_id": f"client-{index:03d}",
            "client_name": f"client-{index:03d}",
            "redirect_uris": [f"https://client-{index}.example.test/callback"],
            "created_at": index + 1,
        }
        for index in range(MAX_REGISTERED_CLIENTS)
    }
    tokens = {
        f"token-{index:03d}": {
            "client_id": f"client-{index:03d}",
            "scope": "local-ops",
            "resource": f"{BASE_URL}/mcp",
            "created_at": index + 1,
            "expires_at": 9_999_999_999,
        }
        for index in range(MAX_REGISTERED_CLIENTS)
    }
    (tmp_path / "oauth.json").write_text(
        json.dumps({"clients": clients, "codes": {}, "tokens": tokens}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="all retained registrations are still referenced"):
        manager.register_client(
            {
                "client_name": "new-client",
                "redirect_uris": ["https://new-client.example.test/callback"],
            }
        )

    persisted = json.loads((tmp_path / "oauth.json").read_text(encoding="utf-8"))
    assert len(persisted["clients"]) == MAX_REGISTERED_CLIENTS
    assert set(persisted["clients"]) == set(clients)
