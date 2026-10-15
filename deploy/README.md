# deploy/ — what this repo owns, and what it depends on

This repo owns exactly **one** unit — `lightning-mcp.container` — plus a shared
network file:

- `lightning-mcp.container` — the MCP server, `PublishPort=8104:8000`.
- `npm-network.network` — the podman network it shares with the daemons.
  `install.sh` links it **only if no other repo has already provided it**.

It does **not** own `lightningd` (or `tor`/`bitcoind` behind it). Those units
are owned by [`crypto-nodes`](https://github.com/regularnmae/crypto-nodes),
together with the rest of the crypto node stack — one file, one owner.

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
- TLS is self-signed: `install.sh` sets `LIGHTNING_TLS_INSECURE=1` for the
  podman network; point `LIGHTNING_CA_CERT` at the node's `cert.pem` to verify
  instead.
- The client carries **two runes**, a read rune and a read+spend rune, and uses
  the spend rune only for the spend tools. `install.sh` creates both on the
  node with `lightning-cli createrune null '<restrictions>'` (restriction
  syntax is `method^<prefix>`, a prefix match — the `$` is literal).

## Deploy

```sh
./deploy/install.sh     # token.env + node.env (creates the runes) + spend.env
./deploy.sh
sudo firewall-cmd --permanent --add-port=8104/tcp && sudo firewall-cmd --reload
```

Payments stay **off** until you edit `~/.config/lightning-mcp/spend.env`.
`install.sh` needs `lightningd` running to create the runes.
