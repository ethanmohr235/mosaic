#!/usr/bin/env python3
import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp.server.transport_security import TransportSecuritySettings

from mcp_server.server import mcp

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
        "since these tools have no authentication of their own.",
    )
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (--http only)")
    parser.add_argument(
        "--allowed-host", action="append", default=[], metavar="HOST",
        help="Extra value to accept in the Host header (--http only). Needed when a "
        "reverse proxy fronts this server under another name, e.g. the machine's "
        "<name>.<tailnet>.ts.net. Repeatable.",
    )
    return parser


def transport_security(host: str, port: int, allowed_hosts: list[str]) -> TransportSecuritySettings:
    """DNS-rebinding protection, on by default.

    Without it a page in the user's browser could drive a loopback-bound
    server: the browser sends an Origin the allowlist below rejects,
    while a real MCP client sends none and passes. The SDK turns this on
    itself for loopback binds, but only ever trusts localhost names --
    anything fronted by a proxy has to be named explicitly.
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


def main() -> None:
    args = build_parser().parse_args()
    # stderr, explicitly: on stdio transport stdout carries the JSON-RPC
    # stream, so anything written there corrupts the protocol.
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s: %(message)s", stream=sys.stderr
    )

    if not args.http:
        mcp.run()
        return

    if args.host not in _LOCAL_HOSTS:
        logging.warning(
            "Binding %s exposes MonOSS on a non-loopback interface with no "
            "authentication -- anything that can reach this port can read and "
            "delete your gardens.", args.host,
        )
    mcp.run(
        "streamable-http",
        host=args.host,
        port=args.port,
        # Every tool opens its own connection and holds nothing between
        # calls, so there is no session state worth keeping -- stateless
        # lets clients reconnect freely and survives a server restart.
        stateless_http=True,
        transport_security=transport_security(args.host, args.port, args.allowed_host),
    )


if __name__ == "__main__":
    main()
