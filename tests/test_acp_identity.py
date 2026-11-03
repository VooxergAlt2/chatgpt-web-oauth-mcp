from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request

import pytest

from chatgpt_web_oauth_mcp import acp_identity
from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError


@pytest.fixture
def binding(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    oauth = profile / "antigravity-acp/acp_token.json"
    oauth.parent.mkdir(parents=True, mode=0o700)
    oauth.write_text(json.dumps({
        "client_id": "synthetic-client",
        "client_secret": "synthetic-client-secret",
        "refresh_token": "synthetic-acp-refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "access_token": "first-ephemeral-access-token",
        "expiry": 100,
    }))
    oauth.chmod(0o600)
    home = tmp_path / "cli-home"
    native = home / ".gemini/antigravity-cli/antigravity-oauth-token"
    native.parent.mkdir(parents=True)
    native.write_text(json.dumps({
        "id_token_email": "qa@example.invalid",
        "token": {"refresh_token": "synthetic-cli-refresh", "access_token": "native-access-1"},
    }))
    native.chmod(0o600)
    calls = []
    def identify(credentials, timeout):
        calls.append((dict(credentials), timeout))
        return "qa@example.invalid"
    monkeypatch.setattr(acp_identity, "_google_identity", identify)
    return {
        "profile": profile, "account": "antigravity3",
        "proof_root": tmp_path / "bindings", "cli_home": home,
    }, oauth, native, calls


def enroll(binding):
    values, _, _, _ = binding
    return acp_identity.verify_binding(**values, now=1000)


def mutate(path, transform):
    value = json.loads(path.read_text())
    transform(value)
    path.write_text(json.dumps(value))


def test_binding_proves_exact_account_without_retaining_secrets(binding):
    values, oauth, native, calls = binding
    originals = oauth.read_bytes(), native.read_bytes()
    result = enroll(binding)
    assert result["account"] == "antigravity3" and result["identity_verified"]
    assert len(calls) == 1
    proof = values["proof_root"] / "antigravity3.json"
    content = proof.read_text()
    for secret in ("qa@example.invalid", "synthetic-client", "synthetic-cli-refresh",
                   "synthetic-acp-refresh", "native-access-1"):
        assert secret not in content
        assert secret not in str(result)
    assert proof.stat().st_mode & 0o777 == 0o600
    assert values["proof_root"].stat().st_mode & 0o777 == 0o700
    assert (oauth.read_bytes(), native.read_bytes()) == originals


def test_validation_is_offline_and_tolerates_normal_access_token_refresh(binding):
    values, oauth, native, calls = binding
    enroll(binding)
    mutate(oauth, lambda data: data.update(access_token="new-access", expiry=9999))
    mutate(native, lambda data: data["token"].update(access_token="new-native", expiry="later"))
    result = acp_identity.validate_binding(**values, now=100000000)
    assert result["identity_verified"] and len(calls) == 1
    with pytest.raises(AcpStateError, match="stale"):
        acp_identity.validate_binding(**values, now=100000000, max_age_seconds=86400)


@pytest.mark.parametrize("target,key,value", [
    ("oauth", "refresh_token", "replacement-refresh"),
    ("oauth", "client_id", "replacement-client"),
    ("oauth", "client_secret", "replacement-secret"),
    ("native", "id_token_email", "another@example.invalid"),
    ("native_refresh", "refresh_token", "replacement-native-refresh"),
])
def test_principal_or_stable_credential_changes_invalidate(binding, target, key, value):
    values, oauth, native, _ = binding
    enroll(binding)
    path = oauth if target == "oauth" else native
    if target == "native_refresh":
        mutate(path, lambda data: data["token"].update({key: value}))
    else:
        mutate(path, lambda data: data.update({key: value}))
    with pytest.raises(AcpStateError, match="binding_changed"):
        acp_identity.validate_binding(**values, now=1001)


def test_mismatch_never_creates_proof_or_reports_identity(binding, monkeypatch):
    values, _, _, _ = binding
    monkeypatch.setattr(acp_identity, "_google_identity", lambda *args: "other@example.invalid")
    with pytest.raises(AcpStateError, match="^acp_account_binding_mismatch$"):
        enroll(binding)
    assert not values["proof_root"].exists()


def test_probe_race_cannot_attest_replaced_credentials(binding, monkeypatch):
    values, oauth, _, _ = binding
    def race(*args):
        mutate(oauth, lambda data: data.update(refresh_token="new-login"))
        return "qa@example.invalid"
    monkeypatch.setattr(acp_identity, "_google_identity", race)
    with pytest.raises(AcpStateError, match="changed_during_verification"):
        enroll(binding)
    assert not values["proof_root"].exists()


def test_wrong_account_profile_and_native_path_fail_closed(binding, tmp_path):
    values, oauth, native, _ = binding
    enroll(binding)
    proof = values["proof_root"] / "antigravity3.json"
    alternate = values["proof_root"] / "antigravity.json"
    alternate.write_bytes(proof.read_bytes())
    alternate.chmod(0o600)
    with pytest.raises(AcpStateError, match="changed"):
        acp_identity.validate_binding(**{**values, "account": "antigravity"}, now=1001)
    other_profile = tmp_path / "other-profile"
    new_token = other_profile / "antigravity-acp/acp_token.json"
    new_token.parent.mkdir(parents=True)
    new_token.write_bytes(oauth.read_bytes())
    new_token.chmod(0o600)
    with pytest.raises(AcpStateError, match="changed"):
        acp_identity.validate_binding(**{**values, "profile": other_profile}, now=1001)
    other_home = tmp_path / "other-home"
    new_native = other_home / ".gemini/antigravity-cli/antigravity-oauth-token"
    new_native.parent.mkdir(parents=True)
    new_native.write_bytes(native.read_bytes())
    new_native.chmod(0o600)
    with pytest.raises(AcpStateError, match="changed"):
        acp_identity.validate_binding(**{**values, "cli_home": other_home}, now=1001)


@pytest.mark.parametrize("mode", [0o640, 0o644])
def test_insecure_proof_permissions_are_rejected(binding, mode):
    values, _, _, _ = binding
    enroll(binding)
    (values["proof_root"] / "antigravity3.json").chmod(mode)
    with pytest.raises(AcpStateError, match="missing_or_invalid"):
        acp_identity.validate_binding(**values, now=1001)


def test_symlink_and_future_or_malformed_proofs_are_rejected(binding, tmp_path):
    values, _, _, _ = binding
    enroll(binding)
    proof = values["proof_root"] / "antigravity3.json"
    original = proof.read_bytes()
    other = tmp_path / "proof.json"
    other.write_bytes(original)
    other.chmod(0o600)
    proof.unlink()
    proof.symlink_to(other)
    with pytest.raises(AcpStateError, match="missing_or_invalid"):
        acp_identity.validate_binding(**values, now=1001)
    proof.unlink()
    proof.write_bytes(original)
    proof.chmod(0o600)
    mutate(proof, lambda data: data.update(verified_at=1000000))
    with pytest.raises(AcpStateError, match="timestamp_invalid"):
        acp_identity.validate_binding(**values, now=1001)
    proof.write_bytes(original)
    mutate(proof, lambda data: data.update(extra="unexpected"))
    with pytest.raises(AcpStateError, match="binding_invalid"):
        acp_identity.validate_binding(**values, now=1001)


def test_cli_home_mapping_uses_existing_environment_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATGPT_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("CHATGPT_MCP_ANTIGRAVITY2_HOME", raising=False)
    monkeypatch.setenv("CHATGPT_MCP_ANTIGRAVITY3_HOME", str(tmp_path / "third"))
    assert str(acp_identity.cli_credential_path("antigravity2")).endswith(
        "/state/antigravity2-home/.gemini/antigravity-cli/antigravity-oauth-token"
    )
    assert str(acp_identity.cli_credential_path("antigravity3")).endswith(
        "/third/.gemini/antigravity-cli/antigravity-oauth-token"
    )
    with pytest.raises(AcpStateError, match="account_invalid"):
        acp_identity.cli_credential_path("../antigravity")


def test_google_probe_uses_fixed_origins_and_keeps_credentials_in_memory(monkeypatch):
    # This test uses no real credentials and never opens a network connection.
    requests = []
    class FakeOpener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            if len(requests) == 1:
                return BytesIO(b'{"access_token":"synthetic-bearer"}')
            return BytesIO(b'{"email":"QA@Example.Invalid","email_verified":true}')
    monkeypatch.setattr(acp_identity.urllib.request, "build_opener", lambda *_: FakeOpener())
    result = acp_identity._google_identity({
        "client_id": "c", "client_secret": "s", "refresh_token": "r",
    }, 5)
    assert result == "qa@example.invalid"
    assert [r.full_url for r, _ in requests] == [
        "https://oauth2.googleapis.com/token",
        "https://www.googleapis.com/oauth2/v3/userinfo",
    ]
    assert urllib.parse.parse_qs(requests[0][0].data.decode())["refresh_token"] == ["r"]
    assert requests[1][0].get_header("Authorization") == "Bearer synthetic-bearer"
    assert acp_identity._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid") is None


@pytest.mark.parametrize("payload", [
    b'{"email":"qa@example.invalid","email_verified":false}',
    b'{"email":"qa@example.invalid"}',
    b'{"email_verified":true}',
    b'{}',
    b'{"email":"sensitive",',
    b'x' * 65537,
])
def test_provider_failures_are_sanitized(monkeypatch, payload):
    class FakeOpener:
        count = 0
        def open(self, request, timeout):
            self.count += 1
            return BytesIO(b'{"access_token":"sensitive-bearer"}' if self.count == 1 else payload)
    monkeypatch.setattr(acp_identity.urllib.request, "build_opener", lambda *_: FakeOpener())
    with pytest.raises(AcpStateError, match="^acp_account_identity_probe_failed$"):
        acp_identity._google_identity({
            "client_id": "c", "client_secret": "s", "refresh_token": "r",
        }, 5)
