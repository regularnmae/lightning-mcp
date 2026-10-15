#!/usr/bin/env bash
# Install the quadlet unit for the Core Lightning MCP server.
#
# Symlinks this repo's deploy/ unit into the user quadlet directory and
# reloads systemd, so the checkout is the single source of truth. Also seeds
# the env files under ~/.config/lightning-mcp/ (mode 600), creating a
# least-privilege read rune and a read+spend rune on the node via
# `lightning-cli createrune` when it can.
#
# Idempotent: re-running refreshes the symlink and leaves existing env files
# untouched. No secret is committed or printed.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$REPO_DIR/deploy"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
QUADLET_DIR="$CONFIG_HOME/containers/systemd"
ENV_DIR="$CONFIG_HOME/lightning-mcp"
TOKEN_FILE="$ENV_DIR/token.env"
NODE_FILE="$ENV_DIR/node.env"
SPEND_FILE="$ENV_DIR/spend.env"

UNITS=(lightning-mcp.container)

# Method allowlists. Rune restrictions use `method^<prefix>` (prefix match);
# the read rune cannot name a payment method at all, so the node itself
# refuses reads to spend, independently of this server's own token gate.
READ_METHODS='"method^getinfo","method^listfunds","method^listchannels","method^listpeers","method^listinvoices","method^listpays","method^decode"'
SPEND_METHODS="$READ_METHODS,\"method^invoice\",\"method^pay\",\"method^keysend\",\"method^withdraw\",\"method^close\""

log() { printf '==> %s\n' "$*"; }

rand_hex() {
    openssl rand -hex 32 2>/dev/null && return 0
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

write_secret() {
    local path="$1"
    local old_umask
    old_umask=$(umask)
    umask 077
    cat > "$path"
    umask "$old_umask"
    chmod 600 "$path"
}

# A rune the node will actually accept. `checkrune` is the authoritative
# check: createrune happily returns a rune whose unique_id the node has
# blacklisted, and that only shows up when a tool call fails with 1503.
rune_usable() {
    [ -n "$1" ] || return 1
    podman exec lightningd lightning-cli -k checkrune rune="$1" method=getinfo \
        >/dev/null 2>&1
}

# Create a rune and prove it usable, or echo nothing. createrune mints a
# fresh unique_id on every call, and a node may have blacklisted an id range
# (the node this repo was first deployed against had ids 0..3 blacklisted),
# so a brand-new rune can come back already blacklisted. Create another (next
# id) until one passes. Every restriction here is `method^...`, so `getinfo`
# is always in range and is the probe.
create_rune() {
    local restrictions="$1" rune=""
    for _ in $(seq 1 12); do
        rune=$(podman exec lightningd lightning-cli createrune null "$restrictions" 2>/dev/null \
            | grep -o '"rune": *"[^"]*"' | head -n1 | sed 's/.*"\([^"]*\)"$/\1/' || true)
        [ -n "$rune" ] || return 0
        if rune_usable "$rune"; then
            printf '%s' "$rune"
            return 0
        fi
        printf '==> created rune %s... was refused (blacklisted id?); retrying\n' \
            "${rune:0:8}" >&2
    done
    printf '%s' "$rune"
}

mkdir -p "$ENV_DIR"

# 1. tokens ---------------------------------------------------------------
if [ -s "$TOKEN_FILE" ]; then
    log "token env present: $TOKEN_FILE"
else
    read_token=""
    if command -v podman >/dev/null 2>&1; then
        read_token=$(podman exec lightning-mcp printenv LIGHTNING_MCP_TOKEN 2>/dev/null || true)
    fi
    if [ -n "$read_token" ]; then
        log "reused the existing read token"
    else
        read_token=$(rand_hex)
        log "generated a new read token -- update clients to match"
    fi
    spend_token=$(rand_hex)
    write_secret "$TOKEN_FILE" <<EOF
# Read token: admits the endpoint, cannot call spend tools.
LIGHTNING_MCP_TOKEN=$read_token
# Spend token: additionally required for invoice / pay / keysend / withdraw / close.
LIGHTNING_MCP_SPEND_TOKEN=$spend_token
EOF
    log "wrote $TOKEN_FILE (mode 600) with a read and a spend token"
fi

# 2. runes + TLS ----------------------------------------------------------
read_rune=""
spend_rune=""
tls_insecure=""
ca_cert=""
if [ -s "$NODE_FILE" ]; then
    read_rune=$(sed -n 's/^LIGHTNING_RUNE=//p' "$NODE_FILE" | head -n1)
    spend_rune=$(sed -n 's/^LIGHTNING_SPEND_RUNE=//p' "$NODE_FILE" | head -n1)
    tls_insecure=$(sed -n 's/^LIGHTNING_TLS_INSECURE=//p' "$NODE_FILE" | head -n1)
    ca_cert=$(sed -n 's/^LIGHTNING_CA_CERT=//p' "$NODE_FILE" | head -n1)
fi

# Re-running is a no-op only while the stored runes are usable. A rune can be
# silently unusable -- a blacklisted unique_id, which is exactly what the
# first deploy hit -- so check rather than trust the file. If the node cannot
# be reached the refresh finds nothing and the existing values are kept.
node_needs_write=0
[ -s "$NODE_FILE" ] || node_needs_write=1
if command -v podman >/dev/null 2>&1 \
    && ! { rune_usable "$read_rune" && rune_usable "$spend_rune"; }; then
    log "creating runes on the node"
    new_read=$(create_rune "[[$READ_METHODS]]")
    new_spend=$(create_rune "[[$SPEND_METHODS]]")
    if [ -n "$new_read" ] && [ -n "$new_spend" ]; then
        read_rune="$new_read"
        spend_rune="$new_spend"
        node_needs_write=1
        log "created a usable read rune and a read+spend rune"
    else
        log "could not refresh the runes (is lightningd up?); keeping any existing ones"
    fi
fi

if [ "$node_needs_write" = 1 ]; then
    if [ -n "$read_rune" ]; then
        log "runes ready"
    else
        log "no runes available; fill in $NODE_FILE by hand"
    fi
    [ -n "$tls_insecure" ] || tls_insecure=1
    write_secret "$NODE_FILE" <<EOF
# Runes created by deploy/install.sh. The read rune names only read methods;
# the spend rune adds the payment methods. The MCP uses the spend rune only
# for spend tools.
LIGHTNING_RUNE=$read_rune
LIGHTNING_SPEND_RUNE=$spend_rune
# clnrest serves a self-signed certificate. 1 skips verification, which is
# acceptable over the podman network; set LIGHTNING_CA_CERT to the node's
# cert.pem and this to 0 for a stricter setup.
LIGHTNING_TLS_INSECURE=$tls_insecure
EOF
    if [ -n "$ca_cert" ]; then
        printf 'LIGHTNING_CA_CERT=%s\n' "$ca_cert" >> "$NODE_FILE"
    fi
    log "wrote $NODE_FILE (mode 600)"
else
    log "node env present and usable: $NODE_FILE"
fi

# 3. spending policy ------------------------------------------------------
if [ -s "$SPEND_FILE" ]; then
    log "spending policy present: $SPEND_FILE"
else
    write_secret "$SPEND_FILE" <<EOF
# Spending (payments) is OFF until you set ENABLE_SPENDING=1. With it on,
# both caps below must be positive (in satoshis) or the server refuses to
# start. MAINNET: this is real money.
ENABLE_SPENDING=0
MAX_PAYMENT_PER_CALL_SATS=10000
MAX_PAYMENT_PER_DAY_SATS=100000
EOF
    log "wrote $SPEND_FILE (mode 600) with ENABLE_SPENDING=0"
fi

# 4. symlink units into the quadlet search path ---------------------------
mkdir -p "$QUADLET_DIR"

NETWORK_TARGET="$QUADLET_DIR/npm-network.network"
if [ ! -e "$NETWORK_TARGET" ]; then
    ln -sfn "$DEPLOY_DIR/npm-network.network" "$NETWORK_TARGET"
    log "linked npm-network.network"
else
    log "npm-network.network already present"
fi

for unit in "${UNITS[@]}"; do
    target="$QUADLET_DIR/$unit"
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        mv -f "$target" "$target.bak"
        log "backed up existing $unit -> $unit.bak"
    fi
    ln -sfn "$DEPLOY_DIR/$unit" "$target"
    log "linked $unit"
done

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
    log "systemd --user reloaded"
fi

cat <<EOF

Done. Spending is DISABLED until you edit $SPEND_FILE.

Next:
  ./deploy.sh                                 # install, build, restart, wait healthy
  # or, units only:
  systemctl --user start lightning-mcp.service

Open the published port on the LAN:
  sudo firewall-cmd --permanent --add-port=8104/tcp && sudo firewall-cmd --reload

Clients send a bearer token from
  $TOKEN_FILE
as \`Authorization: Bearer <token>\`, pointed at http://<host>:8104/mcp.
The read token can call read tools; only the spend token can move funds.
EOF
