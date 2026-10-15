# What this repo owns, and what it depends on

This repo owns only the **application** — its Python code, tests, CI and
`Containerfile`. It ships no unit and no env file. The runtime is owned by
[`stack`](https://github.com/regularnmae/stack), which holds every quadlet unit
and the `npm-network.network` the MCP servers share, and renders the
`~/.config/<name>/*.env` files. See `stack/deploy/` and
`stack/deploy/examples/lightning-mcp/`.

It does **not** own `lightningd` (or `tor`/`bitcoind` behind it). Those units
are owned by `stack`, together with the rest of the crypto node stack — one
file, one owner.

## Upstream contract

The server assumes a Core Lightning node exposing clnrest on the podman network:

| | |
| --- | --- |
| Container / address | `lightningd:3010` (clnrest, HTTPS, self-signed) |
| Network | `npm-network` (no host publish; 9735 is Tor-only) |
| Auth | a **rune** in the `rune` header |

Details that shape the client:

- clnrest is `POST /v1/<method>` with the method's parameters as the JSON body.
- Errors come back non-2xx with `{code, message}`; a rune refusal is `1502`.
- TLS is self-signed: `stack/deploy/install.sh` sets `LIGHTNING_TLS_INSECURE=1` for the
  podman network; point `LIGHTNING_CA_CERT` at the node's `cert.pem` to verify
  instead.
- The client carries **two runes**, a read rune and a read+spend rune, and uses
  the spend rune only for the spend tools. `stack/deploy/install.sh` creates both on the
  node with `lightning-cli createrune null '<restrictions>'` (restriction
  syntax is `method^<prefix>`, a prefix match — the `$` is literal).

## Deploy

```sh
cd ~/build/stack
./deploy.sh --only lightning-mcp   # build the image, restart, wait for "(healthy)"
```

Payments stay **off** until you edit `~/.config/lightning-mcp/spend.env`.
`stack/deploy/install.sh` needs `lightningd` running to create the runes, and
`./deploy.sh` opens `8104/tcp` in firewalld.
