# mcp_server/auth.py
"""A minimal OAuth 2.1 authorization server, for the one case that
needs it: some MCP clients (Claude Desktop's remote-connector UI, not
Claude Code's `mcp add --transport http`) refuse to connect to an HTTP
MCP server that doesn't support dynamic client registration and an
authorize/token flow, even for a server with exactly one trusted user.

This satisfies that protocol requirement without pretending to be real
access control. There is no login screen and no consent screen --
authorize() grants immediately -- because there is no second party here
to protect the resource from: whoever can reach this process's port
already has the same access the CLI and GUI have on this machine. The
actual security boundary is reachability (Tailscale, --allowed-host),
exactly as it is for the unauthenticated stdio and plain-HTTP paths;
this class only produces the token a client insists on presenting.

Everything lives in memory. A server restart forgets every registered
client and issued token -- acceptable for a personal tool talking to
clients on the same tailnet, who just reconnect (see docs/mcp.md).
"""
import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

_AUTH_CODE_TTL_SECONDS = 300
_ACCESS_TOKEN_TTL_SECONDS = 3600


class SingleUserOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self) -> None:
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            scopes=params.scopes or [],
            expires_at=time.time() + _AUTH_CODE_TTL_SECONDS,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        self._auth_codes[code.code] = code
        return construct_redirect_uri(str(params.redirect_uri), code=code.code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._auth_codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        if code.expires_at < time.time():
            del self._auth_codes[authorization_code]
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: gone whether this exchange succeeds or a retry finds
        # it already consumed.
        self._auth_codes.pop(authorization_code.code, None)
        return self._issue_tokens(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token = self._refresh_tokens.get(refresh_token)
        return token if token is not None and token.client_id == client.client_id else None

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Rotate both tokens, as the protocol recommends.
        self._refresh_tokens.pop(refresh_token.token, None)
        return self._issue_tokens(client.client_id, scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self._access_tokens.get(token)
        if access is None:
            return None
        if access.expires_at is not None and access.expires_at < time.time():
            del self._access_tokens[token]
            return None
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._access_tokens.pop(token.token, None)
        self._refresh_tokens.pop(token.token, None)

    def _issue_tokens(self, client_id: str, scopes: list[str], resource: str | None = None) -> OAuthToken:
        access = AccessToken(
            token=secrets.token_urlsafe(32),
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time()) + _ACCESS_TOKEN_TTL_SECONDS,
            resource=resource,
        )
        # No expiry on the refresh token: the client re-authenticating
        # every hour buys nothing here, since there's no second user for
        # a short-lived access token to protect the resource from.
        refresh = RefreshToken(token=secrets.token_urlsafe(32), client_id=client_id, scopes=scopes)
        self._access_tokens[access.token] = access
        self._refresh_tokens[refresh.token] = refresh
        return OAuthToken(
            access_token=access.token,
            expires_in=_ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh.token,
        )
