from acmg.config import Settings
from acmg.oauth_demo import DemoAuth, s256_challenge, verify_s256


def test_pkce_s256_round_trip():
    verifier = "a" * 50
    challenge = s256_challenge(verifier)
    assert verify_s256(verifier, challenge)
    assert not verify_s256("b" * 50, challenge)


def test_authorization_code_exchange_returns_demo_token():
    auth = DemoAuth(Settings(public_base_url="http://localhost:4317"))
    challenge = s256_challenge("verifier-value-with-enough-length")
    code = auth.issue_code(
        client_id="demo-client",
        redirect_uri="http://127.0.0.1:4317/callback",
        code_challenge=challenge,
        scope="commerce.shop",
        resource="http://localhost:4317/mcp",
    )
    token = auth.exchange_code(
        code=code,
        client_id="demo-client",
        redirect_uri="http://127.0.0.1:4317/callback",
        code_verifier="verifier-value-with-enough-length",
        resource="http://localhost:4317/mcp",
    )
    assert token["access_token"] == "demo-shopper-token"
    assert token["token_type"] == "Bearer"
    metadata = auth.authorization_server_metadata()
    assert "S256" in metadata["code_challenge_methods_supported"]
    assert "WWW-Authenticate" not in metadata
