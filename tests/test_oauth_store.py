from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from chatgpt_web_oauth_mcp.oauth import OAuthManager, OAuthRuntimeConfig, _pkce_s256


BASE_URL = "https://mcp.example.test"


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
