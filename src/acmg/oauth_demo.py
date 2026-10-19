"""Demo OAuth metadata for Alexa+ discovery. This is not a production identity provider."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlparse

from acmg.config import Settings
from acmg.store import utcnow


def s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_s256(verifier: str, challenge: str) -> bool:
    if not verifier or not challenge:
        return False
    expected = s256_challenge(verifier)
    return secrets.compare_digest(expected, challenge)


class DemoAuth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.codes: dict[str, dict] = {}

    def protected_resource_metadata(self) -> dict:
        return {
            "resource": self.settings.mcp_resource,
            "authorization_servers": [self.settings.public_base_url.rstrip("/")],
            "scopes_supported": ["commerce.shop", "commerce.refund"],
            "bearer_methods_supported": ["header"],
        }

    def authorization_server_metadata(self) -> dict:
        base = self.settings.public_base_url.rstrip("/")
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "client_credentials"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["commerce.shop", "commerce.refund"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        }

    def issue_code(self, *, client_id: str, redirect_uri: str, code_challenge: str, scope: str, resource: str) -> str:
        code = secrets.token_urlsafe(24)
        self.codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "scope": scope,
            "resource": resource,
            "expires_at": utcnow() + timedelta(minutes=5),
        }
        return code

    def exchange_code(self, *, code: str, client_id: str, redirect_uri: str, code_verifier: str, resource: str) -> dict:
        record = self.codes.get(code)
        if record is None or record["expires_at"] < utcnow():
            raise ValueError("That authorization code is unknown or expired.")
        if record["client_id"] != client_id or record["redirect_uri"] != redirect_uri:
            raise ValueError("The code does not match this client.")
        if resource and record["resource"] and resource != record["resource"]:
            raise ValueError("The resource indicator does not match.")
        if not verify_s256(code_verifier, record["code_challenge"]):
            raise ValueError("PKCE verification failed.")
        del self.codes[code]
        return self.token_response(record["scope"])

    def token_response(self, scope: str) -> dict:
        return {
            "access_token": self.settings.demo_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": scope or "commerce.shop commerce.refund",
        }

    def client_ok(self, client_id: str, client_secret: str) -> bool:
        return token_matches(client_id, self.settings.demo_client_id) and token_matches(
            client_secret, self.settings.demo_client_secret
        )

    def redirect_allowed(self, redirect_uri: str) -> bool:
        parsed = urlparse(redirect_uri)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.hostname in {"localhost", "127.0.0.1"}:
            return True
        return redirect_uri.startswith(self.settings.public_base_url.rstrip("/"))

    def resource_ok(self, resource: str | None) -> bool:
        if not resource:
            return True
        return resource.rstrip("/") == self.settings.mcp_resource.rstrip("/")


def token_matches(presented: str, expected: str) -> bool:
    if not presented or not expected:
        return False
    presented_hash = hashlib.sha256(presented.encode()).digest()
    expected_hash = hashlib.sha256(expected.encode()).digest()
    return secrets.compare_digest(presented_hash, expected_hash)


def basic_credentials(header: str) -> tuple[str, str] | None:
    if not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
    except Exception:
        return None
    if ":" not in decoded:
        return None
    client_id, client_secret = decoded.split(":", 1)
    return client_id, client_secret


def authorize_redirect(redirect_uri: str, code: str, state: str | None) -> str:
    params = {"code": code}
    if state:
        params["state"] = state
    join = "&" if "?" in redirect_uri else "?"
    return redirect_uri + join + urlencode(params)
