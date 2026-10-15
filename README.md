# lightning-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server for a Core
Lightning node (`clnrest`). It is **read-only by default**: payments are off
until an operator enables them, and even then they are capped and gated behind
a second token and a restricted rune.

## Read tools

| Tool | What it does |
| --- | --- |
| `getinfo` | Node id, alias, network, block height, peers |
| `listfunds` | On-chain outputs and channel funds |
| `listchannels` | The node's channels |
| `listpeers` | The node's peers |
| `listinvoices` | Invoices (optionally by label) |
| `listpays` | Outgoing payments (optionally by bolt11) |
| `decode` | Decode a BOLT11 invoice without paying it |

## Spend tools (only when `ENABLE_SPENDING=1`)

`invoice`, `pay`, `keysend`, `withdraw`, `close`. When spending is disabled
these are **not registered at all**.

- `invoice` creates a BOLT11 invoice (moves nothing).
- `pay` supports `dry_run=True` (default): it checks and simulates the payment
  without sending it.
- `keysend` and `withdraw` have **no dry run** — they send immediately, subject
  to the caps.
- `close` is destructive and immediate.

## Two layers of least privilege

This server can move real sats, so access is constrained twice:

**1. Bearer tokens (this server).** `LIGHTNING_MCP_TOKEN` (read) admits the
endpoint; `LIGHTNING_MCP_SPEND_TOKEN` is *additionally* required for any spend
tool. With `ENABLE_SPENDING=1` a spend token is mandatory or the server refuses
to start.

**2. Runes (the node itself).** clnrest authorises every call with a rune, and
a rune can be restricted to a set of methods. The client carries two:
`LIGHTNING_RUNE`, restricted to the read methods, and `LIGHTNING_SPEND_RUNE`,
which adds the payment methods. Read tools use the read rune; spend tools use
the spend rune. Even if the server's own token gate were bypassed, the read
rune **cannot** call `pay` — the node refuses it with error 1502.
`stack` creates both runes with `lightning-cli createrune`.

Plus the usual caps: `MAX_PAYMENT_PER_CALL_SATS` and
`MAX_PAYMENT_PER_DAY_SATS` are both required and positive when spending is on,
and the daily budget is tracked in process per UTC day (only committed
payments count). Every spend attempt is logged.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `LIGHTNING_REST_URL` | `https://lightningd:3010` | clnrest base URL |
| `LIGHTNING_RUNE` | – | Read-scoped rune |
| `LIGHTNING_SPEND_RUNE` | – | Read+spend rune |
| `LIGHTNING_TLS_INSECURE` | – | `1` skips TLS verification (self-signed cert) |
| `LIGHTNING_CA_CERT` | – | Path to the node's `cert.pem`, to verify instead |
| `ENABLE_SPENDING` | – | `1` registers the payment tools |
| `MAX_PAYMENT_PER_CALL_SATS` | `0` | Per-payment cap in sats (required > 0 if spending on) |
| `MAX_PAYMENT_PER_DAY_SATS` | `0` | Per-day cap in sats (required > 0 if spending on) |
| `LIGHTNING_MCP_TOKEN` | – | Read token. **Required** unless `MCP_ALLOW_ANONYMOUS=1` |
| `LIGHTNING_MCP_SPEND_TOKEN` | – | Spend token; mandatory when spending is on |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | Listen address |
| `LOG_LEVEL` | `INFO` | Log level |

clnrest serves a self-signed certificate; `LIGHTNING_TLS_INSECURE=1` is the
default in the seeded `node.env` because the endpoint is reached over the
podman network. Set `LIGHTNING_CA_CERT` to the node's `cert.pem` and clear the
flag for verification.

## Running it

```sh
uv sync
LIGHTNING_MCP_TOKEN=read LIGHTNING_RUNE=<read-rune> uv run python server.py
```

## Development

```sh
uv run pytest -q                # tests, no network
uv run ruff check .             # lint
uv run pyright                  # typecheck
uv run python scripts/smoke.py  # boots the real app on loopback
```

`tests/test_auth.py` drives the token/spend gate as a pure ASGI app;
`tests/test_rpc.py` pins the rune selection (read rune for reads, spend rune
for `spend=True`); `tests/test_spend.py` covers caps, budget and dry runs.

## Deploying

The runtime for this server — its quadlet unit and env wiring — is owned by the
separate [`stack`](https://github.com/regularnmae/stack) repo, not here. `stack`
supplies both tokens, creates the two runes **on the node** with `createrune`,
seeds the TLS setting and the spend policy, publishes the host port and provides
the healthcheck; see that repo for the unit and env-file locations. Spending
stays off until you flip `ENABLE_SPENDING` in the spend env `stack` seeds (shape:
`stack/deploy/examples/lightning-mcp/`).

```sh
cd ~/build/stack
./deploy.sh --only lightning-mcp   # build the image, restart, wait for "(healthy)"
./deploy.sh                        # the whole stack
```
