"""Bind an ACP OAuth principal to its existing CLI account before admission.

The operator verifies identity once through Google's read-only userinfo API.
Runtime admission only checks a private attestation against stable credentials,
the CLI principal and canonical profile paths. Access-token refreshes do not
invalidate a binding. This is a trusted-operator enrollment record, not a way
for a model to select accounts or to create additional quota capacity.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

from .acp_session_store import AcpStateError
from .state_io import atomic_write_bytes

ACCOUNTS = frozenset({"antigravity", "antigravity2", "antigravity3"})
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_SOURCE = "google_oauth_userinfo"
_MAX_JSON_BYTES = 65536


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _private_json(path: Path, code: str) -> dict:
    descriptor = None
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        )
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > _MAX_JSON_BYTES):
            raise ValueError
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_JSON_BYTES + 1)
        if len(raw) > _MAX_JSON_BYTES:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError):
        raise AcpStateError(code) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def cli_credential_path(account: str, cli_home: Path | None = None) -> Path:
    """Resolve the same native account HOME used by the existing CLI harness."""
    if account not in ACCOUNTS:
        raise AcpStateError("acp_account_binding_account_invalid")
    if cli_home is None:
        if account == "antigravity":
            cli_home = Path.home()
        else:
            state = Path(os.environ.get(
                "CHATGPT_MCP_STATE_DIR",
                str(Path.home() / ".local/state/chatgpt-web-oauth-mcp"),
            )).expanduser()
            cli_home = Path(os.environ.get(
                f"CHATGPT_MCP_{account.upper()}_HOME", str(state / f"{account}-home")
            )).expanduser()
    if not cli_home.is_absolute():
        raise AcpStateError("acp_account_binding_cli_home_invalid")
    return cli_home.resolve() / ".gemini/antigravity-cli/antigravity-oauth-token"


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 16384


def _email(value: object) -> str:
    if not isinstance(value, str):
        raise AcpStateError("acp_account_binding_identity_missing")
    normalized = value.strip().casefold()
    if (not normalized or len(normalized) > 320 or normalized.count("@") != 1
            or any(character.isspace() for character in normalized)):
        raise AcpStateError("acp_account_binding_identity_missing")
    return normalized


def _snapshot(profile: Path, account: str, cli_home: Path | None):
    cli_path = cli_credential_path(account, cli_home)
    profile_path = profile.resolve(strict=True)
    acp = _private_json(
        profile_path / "antigravity-acp/acp_token.json",
        "acp_account_binding_profile_invalid",
    )
    cli = _private_json(cli_path, "acp_account_binding_cli_identity_invalid")
    # Ignore access tokens, expiry and unrelated fields: they rotate routinely.
    stable_acp = {key: acp.get(key) for key in ("client_id", "client_secret", "refresh_token")}
    if not all(_nonempty(value) for value in stable_acp.values()):
        raise AcpStateError("acp_account_binding_profile_invalid")
    native_token = cli.get("token")
    if not isinstance(native_token, dict) or not _nonempty(native_token.get("refresh_token")):
        raise AcpStateError("acp_account_binding_cli_identity_invalid")
    native_email = _email(cli.get("id_token_email"))
    fingerprints = {
        "account": account,
        "profile_digest": _digest(str(profile_path)),
        "acp_credential_digest": _digest(stable_acp),
        "cli_path_digest": _digest(str(cli_path)),
        "cli_identity_digest": _digest(native_email),
        "cli_credential_digest": _digest(native_token["refresh_token"]),
    }
    return fingerprints, stable_acp, native_email


def _proof_path(proof_root: Path, account: str, *, create: bool = False) -> Path:
    if account not in ACCOUNTS:
        raise AcpStateError("acp_account_binding_account_invalid")
    try:
        if proof_root.is_symlink():
            raise ValueError
        if create:
            proof_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = proof_root.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ValueError
    except (OSError, ValueError):
        raise AcpStateError("acp_account_binding_store_invalid") from None
    return proof_root / f"{account}.json"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward OAuth credentials or bearer headers to another origin.
        return None


def _google_identity(credentials: dict[str, str], timeout_seconds: float) -> str:
    """Refresh only in memory and return a verified email; never persist tokens."""
    opener = urllib.request.build_opener(_NoRedirect())
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        **credentials,
    }).encode("ascii")
    try:
        with opener.open(
            urllib.request.Request(_GOOGLE_TOKEN_URL, data=body, method="POST"),
            timeout=timeout_seconds,
        ) as response:
            token_raw = response.read(_MAX_JSON_BYTES + 1)
        if len(token_raw) > _MAX_JSON_BYTES:
            raise ValueError
        token = json.loads(token_raw)
        access_token = token.get("access_token") if isinstance(token, dict) else None
        if not _nonempty(access_token):
            raise ValueError
        with opener.open(
            urllib.request.Request(
                _GOOGLE_USERINFO_URL,
                headers={"Authorization": "Bearer " + access_token},
            ),
            timeout=timeout_seconds,
        ) as response:
            identity_raw = response.read(_MAX_JSON_BYTES + 1)
        if len(identity_raw) > _MAX_JSON_BYTES:
            raise ValueError
        identity = json.loads(identity_raw)
        if not isinstance(identity, dict) or identity.get("email_verified") is not True:
            raise ValueError
        return _email(identity.get("email"))
    except (OSError, ValueError, TypeError, AcpStateError):
        # Provider bodies may contain credentials or personal information.
        raise AcpStateError("acp_account_identity_probe_failed") from None


def validate_binding(
    *,
    profile: Path,
    account: str,
    proof_root: Path,
    cli_home: Path | None = None,
    now: float | None = None,
    max_age_seconds: float | None = None,
) -> dict[str, object]:
    """Check enrollment offline. A changed credential/principal fails closed.

    There is no periodic admission expiry: unchanged refresh credentials cannot
    acquire another account's identity. Callers may explicitly request a freshness
    bound for acceptance; provider authorization and quota checks remain separate.
    """
    path = _proof_path(proof_root, account)
    proof = _private_json(path, "acp_account_binding_missing_or_invalid")
    try:
        expected, _, _ = _snapshot(profile, account, cli_home)
    except OSError:
        raise AcpStateError("acp_account_binding_profile_invalid") from None
    expected_keys = {*expected, "version", "source", "verified_at"}
    if (set(proof) != expected_keys or proof.get("version") != 1
            or proof.get("source") != _SOURCE):
        raise AcpStateError("acp_account_binding_invalid")
    if any(proof.get(key) != value for key, value in expected.items()):
        raise AcpStateError("acp_account_binding_changed")
    checked = proof.get("verified_at")
    current = time.time() if now is None else now
    if (isinstance(checked, bool) or not isinstance(checked, (int, float))
            or not math.isfinite(checked) or checked <= 0
            or not math.isfinite(current) or checked > current + 300):
        raise AcpStateError("acp_account_binding_timestamp_invalid")
    if max_age_seconds is not None:
        if (isinstance(max_age_seconds, bool) or not math.isfinite(max_age_seconds)
                or max_age_seconds <= 0):
            raise AcpStateError("acp_account_binding_max_age_invalid")
        if current - checked > max_age_seconds:
            raise AcpStateError("acp_account_binding_stale")
    return {
        "account": account,
        "identity_verified": True,
        "identity_source": _SOURCE,
        "verified_at": checked,
        "proof_version": 1,
    }


def verify_binding(
    *,
    profile: Path,
    account: str,
    proof_root: Path,
    cli_home: Path | None = None,
    now: float | None = None,
    timeout_seconds: float = 10,
) -> dict[str, object]:
    """Operator-only enrollment; write hashes, never OAuth values or emails."""
    if (isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds)
            or not 1 <= timeout_seconds <= 30):
        raise AcpStateError("acp_account_identity_timeout_invalid")
    try:
        before, credentials, native_email = _snapshot(profile, account, cli_home)
    except OSError:
        raise AcpStateError("acp_account_binding_profile_invalid") from None
    observed_email = _google_identity(credentials, timeout_seconds)
    if observed_email != native_email:
        raise AcpStateError("acp_account_binding_mismatch")
    # Credential login/rotation concurrent with the probe must not be attested.
    after, _, _ = _snapshot(profile, account, cli_home)
    if before != after:
        raise AcpStateError("acp_account_binding_changed_during_verification")
    checked = time.time() if now is None else now
    if isinstance(checked, bool) or not math.isfinite(checked) or checked <= 0:
        raise AcpStateError("acp_account_binding_timestamp_invalid")
    path = _proof_path(proof_root, account, create=True)
    if path.is_symlink():
        raise AcpStateError("acp_account_binding_missing_or_invalid")
    proof = {**before, "version": 1, "source": _SOURCE, "verified_at": checked}
    atomic_write_bytes(
        path,
        (json.dumps(proof, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        mode=0o600,
        sync_directory=True,
    )
    return validate_binding(
        profile=profile, account=account, proof_root=proof_root, cli_home=cli_home,
        now=checked,
    )
