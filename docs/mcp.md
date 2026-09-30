# MCP server

`run_mcp.py` starts an MCP server over stdio, letting an MCP client
(Claude Code, Claude Desktop, anything else that speaks the protocol)
read and edit gardens directly. It is a thin adapter over `core/` — see
the MCP server section of `docs/architecture.md` for the design.

## Setup

```
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

`--system-site-packages` is only load-bearing for the desktop app (see
`docs/ui-setup.md`); it's harmless for the MCP server and CLI, so one venv
covers all three.

`.mcp.json` at the project root registers the server for clients that
read project-scoped config:

```json
{
  "mcpServers": {
    "monoss": {
      "command": "${CLAUDE_PROJECT_DIR:-.}/.venv/bin/python",
      "args": ["${CLAUDE_PROJECT_DIR:-.}/run_mcp.py"]
    }
  }
}
```

The interpreter is spelled out rather than left as `python3`: on a
machine with conda or another Python on `PATH`, `python3` may not resolve
to an interpreter with this project's dependencies. Restart the client
after adding or editing this file.

The client may launch the server from any working directory, so paths
can't be relative to cwd — `${CLAUDE_PROJECT_DIR}` (Claude Code only;
falls back to `.` when unset, since the variable lives in the *server's*
spawned environment, not Claude Code's own) resolves them relative to the
project root instead, so this file works unmodified regardless of where
the repo is cloned. Other MCP clients (e.g. Claude Desktop) don't expand
this variable; on those, replace both occurrences with the project's
absolute path.

## Tools

47 tools, grouped: gardens (`list_gardens`, `create_garden`,
`export_garden`), motes (`list_motes`, `search_motes`, `get_mote`,
`create_mote`, `update_mote`, `delete_property`, `delete_mote`), relationships
(`list_relationships`, `mote_relationships`, `create_relationship`,
`update_relationship`, `delete_relationship`), species (`list_species`,
`species_of`, `instances_of`, `set_species`, `set_template`,
`template_for`, `create_instance`), graph (`neighborhood`,
`whole_garden`), simulation (`preview_simulation`, `why`),
dependencies (`impact`, `requires`, `producers`, `consumers`),
the worksheet (`read_worksheet`, `write_worksheet`), `solve_health`,
`check_roles`, `list_scenarios`, notes (`resolve_modoc`), and
`reserved_names`.

The relationship-type parameter is `type` on every tool that takes one
(`list_relationships`, `create_relationship`, `update_relationship`); it
was `relationship_type` on those three while core called it `type_`.

Every tool takes an optional `garden`, defaulting to `default`.

`list_gardens` returns every garden.

Property values keep whatever JSON type the client sends: `21` is stored
as a number, `"21"` as a string. This is the one place the MCP adapter
deliberately differs from `monarch/api.py`, which interprets incoming strings
as JSON because GUI text fields deliver everything as text.

`preview_simulation` computes a run entirely in memory and returns what
*would* change — nothing is written. Solving and writing the answer back
is not exposed; do that in the app or the CLI.

Its steps come back as `{"steps": [...]}`, each carrying `step`,
`changes`, `summary`, `equation_count` and `constraint_violations` — the
same shape Motion's timeline receives (`core.simulation.step_payloads`).

`resolve_modoc(mote, step)` reads a mote's `.modoc` note and everything
its macros currently resolve to -- which mote each `\mote{...}` points at,
what each `\val{...}` comes to, the saved Moxie view each `\graph{...}`
draws (re-solved, summarised -- a sweep's curves point by point, a
surface's extent rather than its grid), and what didn't resolve. `step` behaves exactly like `preview_simulation`'s: 0 is
the garden as stored, higher is a projection that far ahead, and neither
writes. There is no write tool -- a note is a property, so `update_mote`
on `modoc` is how you edit one. See `docs/modoc.md`.

`reserved_names()` is the complete equation/simulation reference for an
MCP client, not just a list of reserved keys: it also returns what each
reserved property and relationship type means, the full equation
expression language (`targets()`/`sources()` for cross-mote references,
`property()`, `prev(x)`, `t`, `sum()`/`count()`, `abs`/`min`/`max`), and
every built-in physical-property provider's operations, inputs, and
units. Call it before hand-writing an `equation` or a property-model
mote — `docs/simulation.md` has the same content with more prose, but
isn't itself reachable over MCP.

`why` explains how a solved value was determined, recursively, as of the
last solve that wrote to the garden. It needs such a solve to have
happened; a preview or a scenario solve leaves nothing for it to read.

The four dependency tools read the *rules* instead, so they answer
before anything has been solved. Note that MonOSS solves constraint
systems: `producers` returns every equation that could determine a
value, which for a coupled system is several, and an empty result means
nothing can determine it at all.

`preview_simulation` also returns `constraint_violations`: comparison
relations (`efficiency <= 1`) the answer does not satisfy. These are
reported, never enforced, and one depending on a value the run could not
determine is reported as *not checked* rather than passing.

`list_scenarios` lists named perturbations of the garden, and
`preview_simulation` takes a `scenario` to solve under one. A preview
never writes either way; a scenario changes which question is being
previewed, not whether the answer is applied.

`check_roles` reports assemblies wired to the wrong parts, and separately
which rules are asserted about one mote rather than a whole species --
which is inferred from what `applies_to` points at, and changes the first
time someone makes an instance of that mote. See `docs/simulation.md`.

`read_worksheet` / `write_worksheet` are the fastest way for a client to
build or edit a calculation: `bd.x = 80 in` is a given, `bd.l = sqrt(bd.x^2
+ bd.y^2)` is an equation and makes `l` free on `bd`, `bd.Ax = ?` frees a
value with no number yet, `bd.Ax + bd.Bx == 0` is a relation that declares
no freedom, and a trailing `! ΣFx` names the rule. Eight lines replace the
ten motes and ten relationships the same model used to need.

**`write_worksheet` replaces, and it deletes.** The lines are the whole set
of the garden's `once`-scoped equations, so a rule with no line is removed
(`deleted` says which). Call `read_worksheet` first and edit what comes
back, rather than sending lines composed against a garden you have not
read. Rules about a *species* are never rendered or touched — those are
reusable models, and `untouched` counts them. Nothing is deleted while any
line fails to parse.

`solve_health` answers "can this be solved, and if not, which line is
wrong" *before* anything runs. A failed solve says "underdetermined",
which is true of the whole garden and names nothing; this names the coupled
**block** of unknowns that is short an equation (`groups`), the free values
nothing can determine at all (`culprits`), and the dimensionally
inconsistent equations (`unitProblems`). Read `verdict`, not the totals:
`equations == unknowns` is neither necessary nor sufficient, since three of
each can be two blocks with one short and one surplus. See
`docs/simulation.md`.

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

## Connecting Claude Desktop's remote connector

Desktop has two separate ways to reach an MCP server, and only one of
them needs anything below. A server entry in Desktop's own config file
(the same shape as `.mcp.json` above) spawns `run_mcp.py` over stdio
exactly like Claude Code does, and needs nothing extra. Desktop's
**Custom Connector** UI (Settings → Connectors) is different: it always
speaks HTTP and always insists on OAuth — dynamic client registration
plus an authorize/token exchange — even for a server with one trusted
user on a private network. Without it, adding the connector fails with
something like *"Couldn't register with MonOSS's sign-in service."*

`--public-url` turns that on:

```
python run_mcp.py --http --public-url https://compass.tail62282b.ts.net \
  --allowed-host "$(tailscale status --json | jq -r .Self.DNSName | sed 's/\.$//')"
```

(`--allowed-host` is still needed alongside `--public-url` — the flags
answer different questions. `--public-url` is what the server tells
*clients* to use as its OAuth issuer; `--allowed-host` is what the
server itself accepts in an incoming `Host` header. The two happen to
name the same machine here, which is why they look redundant, but
nothing derives one from the other.)

Point the connector at `https://<machine>.<tailnet>.ts.net/mcp`, same as
the Claude Code command above.

**This is not real access control, and doesn't try to be.** `mcp_server/auth.py`
implements enough of OAuth 2.1 to satisfy a client that insists on the
protocol — dynamic registration, PKCE, token issuance and refresh — but
`authorize()` grants every request immediately, with no login screen and
no consent step. There's no second party to ask: whoever can reach this
port already has the access the CLI and GUI have on this machine, same
as the plain-HTTP path above. The token exists because the client
demands one, not because it gates anything the network boundary
(Tailscale, `--allowed-host`) wasn't already gating. Everything the
provider issues lives in memory, so restarting the server forgets every
registered client and token — Desktop will need to reconnect the
connector, the same inconvenience as any other locally-run dev server
restarting under it.

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
