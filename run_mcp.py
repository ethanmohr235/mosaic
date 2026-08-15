#!/usr/bin/env python3
import argparse
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings

from mcp_server.auth import SingleUserOAuthProvider
from mcp_server.server import build_server, mcp

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_mcp",
        description="Serve MonOSS over MCP. Defaults to stdio, where the client "
        "starts this process itself; --http serves it over the network instead.",
    )
    parser.add_argument(
        "--http", action="store_true", help="Serve over streamable HTTP instead of stdio"
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Address to bind (--http only). Defaults to loopback -- expose it over a "
        "private network with `tailscale serve` rather than binding a public interface, "
        "since these tools have no authentication of their own unless --public-url is set.",
    )
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (--http only)")
    parser.add_argument(
        "--allowed-host", action="append", default=[], metavar="HOST",
        help="Extra value to accept in the Host header (--http only). Needed when a "
        "reverse proxy fronts this server under another name, e.g. the machine's "
        "<name>.<tailnet>.ts.net. Repeatable. --public-url's own host is added "
        "automatically.",
    )
    parser.add_argument(
        "--public-url", metavar="URL",
        help="The externally-reachable base URL for this server, e.g. "
        "https://<machine>.<tailnet>.ts.net (--http only). Turns on OAuth: some MCP "
        "clients (Claude Desktop's remote-connector UI) refuse to connect to a server "
        "that doesn't support dynamic client registration and an authorize/token flow, "
        "even for a single-user server like this one. See docs/mcp.md and "
        "mcp_server/auth.py for what that layer does and doesn't protect against -- it "
        "satisfies the protocol requirement, it is not an additional access boundary "
        "beyond who can already reach this port.",
    )
    return parser


def transport_security(host: str, allowed_hosts: list[str]) -> TransportSecuritySettings:
    """DNS-rebinding protection, on by default.

    Without it a page in the user's browser could drive a loopback-bound
    server: the browser sends an Origin the allowlist below rejects,
    while a real MCP client sends none and passes. The SDK turns this on
    itself for loopback binds, but only ever trusts localhost names --
    anything fronted by a proxy (including the OAuth routes under
    --public-url) has to be named explicitly.
    """
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if host not in _LOCAL_HOSTS:
        hosts.append(f"{host}:*")
    for name in allowed_hosts:
        # A proxy on a default port sends no port at all, so accept both forms.
        hosts += [name, f"{name}:*"]
        origins.append(f"https://{name}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def build_auth(public_url: str) -> tuple[SingleUserOAuthProvider, AuthSettings]:
    if not public_url.startswith("https://"):
        logging.warning(
            "--public-url %r is not https:// -- most MCP clients require TLS for "
            "OAuth and will refuse to complete the connection.", public_url,
        )
    settings = AuthSettings(
        issuer_url=public_url,
        resource_server_url=public_url,
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=["mcp"]),
        revocation_options=RevocationOptions(enabled=True),
    )
    return SingleUserOAuthProvider(), settings


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.public_url and not args.http:
        parser.error("--public-url requires --http")
    # stderr, explicitly: on stdio transport stdout carries the JSON-RPC
    # stream, so anything printed there corrupts the protocol.
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s: %(message)s", stream=sys.stderr
    )

    if not args.http:
        mcp.run()
        return

    server = mcp
    allowed_hosts = list(args.allowed_host)
    if args.public_url:
        provider, auth_settings = build_auth(args.public_url)
        server = build_server(auth_server_provider=provider, auth_settings=auth_settings)
        public_host = urlparse(args.public_url).netloc
        if public_host and public_host not in allowed_hosts:
            allowed_hosts.append(public_host)

    if args.host not in _LOCAL_HOSTS:
        logging.warning(
            "Binding %s exposes MonOSS on a non-loopback interface%s -- anything that "
            "can reach this port can read and delete your gardens.", args.host,
            "" if args.public_url else " with no authentication",
        )
    server.run(
        "streamable-http",
        host=args.host,
        port=args.port,
        # Every tool opens its own connection and holds nothing between
        # calls, so there is no session state worth keeping -- stateless
        # lets clients reconnect freely and survives a server restart.
        # (OAuth tokens themselves are still forgotten on restart, since
        # SingleUserOAuthProvider is in-memory -- see mcp_server/auth.py.)
        stateless_http=True,
        transport_security=transport_security(args.host, allowed_hosts),
    )


if __name__ == "__main__":
    main()
