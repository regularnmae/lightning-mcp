# lightning-mcp — agent notes

Flat three-module Python MCP server (not a package). `server.py` is the app —
config, read tools, the gated spend tools, the satoshi budget and the
two-token middleware; `rpc.py` is its **stdlib-only** clnrest client;
`healthcheck.py` is the container healthcheck probe. Rootless podman/quadlet;
the runtime — its quadlet unit, the `npm-network` it shares and the env wiring —
is owned by the `stack` repo (https://github.com/regularnmae/stack), not here.
uv only. `mcp` is 2.x (`MCPServer`).

One of four sibling crypto MCP servers. This is a **money server** — the
safety posture matters more than the features. Read `README.md` first.

## Commands

```sh
uv run ruff check .            # lint (CI gate)
uv run pyright                 # typecheck (CI gate)
uv run pytest -q               # tests, no network
uv run python scripts/smoke.py # boots the real app on a loopback port
```

Deployment is owned by the `stack` repo: `cd ~/build/stack && ./deploy.sh --only lightning-mcp`
rebuilds the image, restarts `lightning-mcp.service` and waits for "(healthy)";
`./deploy.sh` deploys the whole stack. The unit and `npm-network` live there,
not here.

## Safety invariants — do not weaken these

- **Spending is off by default.** Spend tools live inside `if ENABLE_SPENDING:`
  so they are not registered when it is off. Keep that.
- **Two bearer tokens.** The middleware admits the read token for reads and
  requires the spend token for any `tools/call` naming a tool in
  `SPEND_TOOLS`. Enabling spending with no spend token is a startup error. If
  you add a spend tool, add its name to `SPEND_TOOLS` (and to
  `tests/test_schemas.py`).
- **Two runes.** `rpc.call(..., spend=True)` is what selects the spend rune;
  read tools must not pass it. The read rune is method-restricted on the node,
  so it cannot pay even if the token gate is bypassed. Do not "simplify" to a
  single rune.
- **Caps are mandatory when spending is on** (`MAX_PAYMENT_PER_CALL_SATS`,
  `MAX_PAYMENT_PER_DAY_SATS` both > 0 or import raises). No "unlimited".
- **Every payment funnels through `_run_payment`** — precheck, reserve for
  non-dry-runs, call with `spend=True`, audit, release on failure. Do not call
  `rpc.call("pay", …)` from a tool directly.
- **`pay` is dry-run by default.** `keysend` and `withdraw` have no dry run;
  that asymmetry is deliberate and documented, not an oversight.

## Facts that are easy to get wrong

- **clnrest contract**: `POST <base>/v1/<method>` with the params as the JSON
  body and the rune in a `rune` header. Errors come back with a non-2xx status
  and a JSON body carrying `code` (1502 = rune refusal) and `message`.
- **Rune restriction grammar** is `method^<prefix>` (a *prefix* match). The
  `$` is literal — `method^getinfo$` never matches. `createrune null '<restrictions>'`
  is how `stack/deploy/install.sh` creates a rune from scratch (passing `null` as the rune).
- The middleware **reads the POST body** to learn the tool name, then replays
  it via `_replay(body, original)`, which delegates to the original `receive`.
  Returning `http.disconnect` instead truncates the SSE response and the client
  sees `IncompleteRead`.
- TLS: clnrest's cert is self-signed; `rpc.py` builds an `ssl` context from
  `LIGHTNING_CA_CERT` or `LIGHTNING_TLS_INSECURE`.
- Tool bodies are synchronous (`def`); anyio runs them on worker threads.
- `tests/test_auth.py` drives the middleware as a pure ASGI app (no socket);
  `tests/test_rpc.py` asserts the rune header per call.
- `pytest` uses `pythonpath = ["."]`; do **not** `from conftest import ...` in a
  test (tests is a package) — use the fixtures.
- A new top-level module must go in the `Containerfile`'s `COPY` lines **and**
  pyright's `include`.
- Publishing: `PublishPort=8104:8000`. Open `8104/tcp` in firewalld.
- `stack/deploy/install.sh` writes `$DATA_DIR/config/lightning-mcp/{token,node,spend}.env`
  (mode 600), creates the runes on the node, and renders the unit. The env-file
  shapes are documented in `stack/deploy/examples/lightning-mcp/`.
