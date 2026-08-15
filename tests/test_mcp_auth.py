import asyncio
import base64
import hashlib
import secrets

import pytest

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from mcp_server.auth import _ACCESS_TOKEN_TTL_SECONDS, SingleUserOAuthProvider


def run(coro):
    """This suite's only async calls; a bare asyncio.run per test avoids
    adding a pytest-asyncio/anyio-marker dependency for the sake of nine
    tests."""
    return asyncio.run(coro)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


@pytest.fixture
def provider():
    return SingleUserOAuthProvider()


@pytest.fixture
def client():
    return OAuthClientInformationFull(
        client_id="client-1",
        redirect_uris=["http://localhost:9999/callback"],
        grant_types=["authorization_code", "refresh_token"],
        token_endpoint_auth_method="none",
    )


async def _authorize(provider, client, *, state="xyz"):
    _, challenge = pkce_pair()
    url = await provider.authorize(client, AuthorizationParams(
        state=state, scopes=["mcp"], code_challenge=challenge,
        redirect_uri=client.redirect_uris[0], redirect_uri_provided_explicitly=True,
    ))
    code = url.split("code=")[1].split("&")[0]
    return url, code


def test_register_then_get_round_trips(provider, client):
    async def body():
        await provider.register_client(client)
        return await provider.get_client(client.client_id)

    fetched = run(body())
    assert fetched.client_id == client.client_id


def test_unregistered_client_returns_none(provider):
    assert run(provider.get_client("no-such-client")) is None


def test_authorize_grants_immediately_no_consent_step(provider, client):
    # The whole point: this server has one trusted user and no second
    # party to protect the resource from, so there is nothing to ask.
    url, code = run(_authorize(provider, client))
    assert url.startswith("http://localhost:9999/callback?")
    assert f"code={code}" in url
    assert "state=xyz" in url


def test_authorization_code_is_scoped_to_its_client(provider, client):
    async def body():
        await provider.register_client(client)
        _, code = await _authorize(provider, client)
        wrong_client = client.model_copy(update={"client_id": "someone-else"})
        return (
            await provider.load_authorization_code(client, code),
            await provider.load_authorization_code(wrong_client, code),
        )

    own, other = run(body())
    assert own is not None
    assert other is None


def test_unknown_authorization_code_returns_none(provider, client):
    assert run(provider.load_authorization_code(client, "never-issued")) is None


def test_expired_authorization_code_is_rejected(provider, client, monkeypatch):
    # Negative TTL guarantees the code is already expired the instant it's
    # minted -- deterministic, unlike sleeping across a real clock tick.
    monkeypatch.setattr("mcp_server.auth._AUTH_CODE_TTL_SECONDS", -1)

    async def body():
        _, code = await _authorize(provider, client)
        return await provider.load_authorization_code(client, code)

    assert run(body()) is None


def test_exchange_authorization_code_issues_tokens_and_consumes_the_code(provider, client):
    async def body():
        _, code = await _authorize(provider, client)
        auth_code = await provider.load_authorization_code(client, code)
        tokens = await provider.exchange_authorization_code(client, auth_code)
        still_there = await provider.load_authorization_code(client, code)
        return tokens, still_there

    tokens, still_there = run(body())
    assert tokens.access_token and tokens.refresh_token
    assert tokens.scope == "mcp"
    assert tokens.expires_in == _ACCESS_TOKEN_TTL_SECONDS
    # Single-use: gone after exchange, so a retried/replayed request can't
    # mint a second, independent token pair from the same code.
    assert still_there is None


def test_access_token_expires(provider, client, monkeypatch):
    monkeypatch.setattr("mcp_server.auth._ACCESS_TOKEN_TTL_SECONDS", -1)

    async def body():
        _, code = await _authorize(provider, client)
        auth_code = await provider.load_authorization_code(client, code)
        tokens = await provider.exchange_authorization_code(client, auth_code)
        return await provider.load_access_token(tokens.access_token)

    assert run(body()) is None


def test_access_token_round_trips_before_expiry(provider, client):
    async def body():
        _, code = await _authorize(provider, client)
        auth_code = await provider.load_authorization_code(client, code)
        tokens = await provider.exchange_authorization_code(client, auth_code)
        return await provider.load_access_token(tokens.access_token)

    fresh = run(body())
    assert fresh is not None and fresh.client_id == "client-1"


def test_refresh_rotates_both_tokens(provider, client):
    async def body():
        _, code = await _authorize(provider, client)
        auth_code = await provider.load_authorization_code(client, code)
        first = await provider.exchange_authorization_code(client, auth_code)

        refresh_token = await provider.load_refresh_token(client, first.refresh_token)
        second = await provider.exchange_refresh_token(client, refresh_token, ["mcp"])

        return first, second, await provider.load_refresh_token(client, first.refresh_token)

    first, second, old_refresh_after_rotation = run(body())
    assert second.access_token != first.access_token
    assert second.refresh_token != first.refresh_token
    # Rotated away, not just re-issued: the old refresh token can't be
    # replayed for a second, independent access token.
    assert old_refresh_after_rotation is None


def test_revoke_removes_the_access_token(provider, client):
    async def body():
        _, code = await _authorize(provider, client)
        auth_code = await provider.load_authorization_code(client, code)
        tokens = await provider.exchange_authorization_code(client, auth_code)
        access = await provider.load_access_token(tokens.access_token)
        await provider.revoke_token(access)
        return await provider.load_access_token(tokens.access_token)

    assert run(body()) is None
