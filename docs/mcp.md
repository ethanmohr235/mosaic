# MCP server

`run_mcp.py` starts an MCP server over stdio, letting an MCP client
(Claude Code, Claude Desktop, anything else that speaks the protocol)
read and edit gardens directly. It is a thin adapter over `core/` — see
the MCP server section of `docs/architecture.md` for the design.

## Setup

```
pip install -r requirements.txt
```

`.mcp.json` at the project root registers the server for clients that
read project-scoped config:

```json
{
  "mcpServers": {
    "monoss": {
      "command": "/home/ethan/projects/monoss/.venv/bin/python",
      "args": ["/home/ethan/projects/monoss/run_mcp.py"]
    }
  }
}
```

The interpreter is spelled out rather than left as `python3`: the
`python3` first on PATH here is conda's and does not have this project's
dependencies. Restart the client after adding or editing this file.

Paths are absolute because the client may launch the server from any
working directory. If you move or clone the project elsewhere, update
both.

## Tools

30 tools, grouped: gardens (`list_gardens`, `create_garden`,
`export_garden`), motes (`list_motes`, `get_mote`, `create_mote`,
`update_mote`, `delete_property`, `delete_mote`), relationships
(`list_relationships`, `mote_relationships`, `create_relationship`,
`update_relationship`, `delete_relationship`), species (`list_species`,
`species_of`, `instances_of`, `set_species`, `set_template`,
`template_for`, `create_instance`), graph (`neighborhood`,
`whole_garden`), simulation (`preview_simulation`, `list_runs`,
`get_run`, `run_changes`, `run_ticks`, `mote_history`), and
`reserved_names`.

Every tool takes an optional `garden`, defaulting to `default`.

Property values keep whatever JSON type the client sends: `21` is stored
as a number, `"21"` as a string. This is the one place the MCP adapter
deliberately differs from `ui/api.py`, which interprets incoming strings
as JSON because GUI text fields deliver everything as text.

`preview_simulation` computes a run entirely in memory and returns what
*would* change — nothing is written or logged. Applying a run to the
graph is not exposed; do that in the app or the CLI.

## Reaching it from other machines

`run_mcp.py` defaults to stdio, where the client starts the process
itself and talks over a pipe — that is what `.mcp.json` above uses, and
it needs nothing running in advance. To reach the same graph from
another machine, serve it over HTTP instead:

```
python run_mcp.py --http                  # 127.0.0.1:8000
python run_mcp.py --http --port 9000
```

**These tools have no authentication of their own.** Anything that can
reach the port can read, edit and delete every garden, and deletes are
not undoable. So the bind stays on loopback, and the right way to reach
it from your other devices is a private network rather than a public
interface. With Tailscale:

```
tailscale serve --bg 8000
python run_mcp.py --http --allowed-host "$(tailscale status --json | jq -r .Self.DNSName | sed 's/\.$//')"
```

`tailscale serve` proxies your tailnet to the loopback port over TLS, so
nothing is published to the internet and only your own devices can
connect. It forwards the original `Host` header, which the server
rejects unless that name is allowlisted — hence `--allowed-host`, which
is repeatable. Without it you get `421 Invalid Host header`.

That rejection is DNS-rebinding protection, and it is on by default: a
page in your browser can otherwise drive a loopback-bound server. A real
MCP client sends no `Origin` and passes; a browser sends one and gets
`403 Invalid Origin header`.

Point a client at the tailnet URL:

```
claude mcp add --transport http monoss https://<machine>.<tailnet>.ts.net/mcp
```

Binding a non-loopback address directly (`--host 0.0.0.0`) works and
logs a warning, but puts an unauthenticated, delete-capable endpoint on
every interface the machine has. Prefer the proxy.

Note this reaches Claude Code and Claude Desktop running on **your own
devices**. It does not reach claude.ai or the mobile app: those fetch
connectors from Anthropic's infrastructure, not your browser, so they
need a publicly resolvable HTTPS endpoint — which in turn needs real
authentication, not just a private network.

The machine serving this has to be awake. Nothing here changes where the
gardens live: they stay in `gardens/` on this machine, and the desktop
app keeps reading the same files.

## Running it alongside the app

Both can be open on the same garden. Writes from the MCP server wait out
a lock the GUI is holding rather than failing, but the GUI does not
notice external changes until it refetches — switch gardens and back, or
reopen, to see motes created through MCP.

An always-on `--http` server contends for the SQLite file far more than
an occasional CLI run does. `busy_timeout` absorbs short collisions; if
you start seeing "database is locked" in practice, the fix is switching
`core/db.py` to WAL journal mode, which lets readers and writers proceed
concurrently. That is a change to shared code the CLI and UI also use,
so it has not been made yet.

## Checking it works

```
python run_mcp.py
```

It should start and wait on stdin without printing anything: on stdio
transport, stdout carries the JSON-RPC stream, so any stray output
corrupts the protocol. Logging goes to stderr for that reason.
