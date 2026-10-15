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

create_rune() {
    # $1: a JSON restrictions string. Echoes the rune, or nothing on failure.
    podman exec lightningd lightning-cli createrune null "$1" 2>/dev/null \
        | grep -o '"rune": *"[^"]*"' | head -n1 | sed 's/.*"\([^"]*\)"$/\1/' || true
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
if [ -s "$NODE_FILE" ]; then
    log "node env present: $NODE_FILE"
else
    read_rune=""
    spend_rune=""
    if command -v podman >/dev/null 2>&1; then
        read_rune=$(create_rune "[[$READ_METHODS]]")
        spend_rune=$(create_rune "[[$SPEND_METHODS]]")
    fi
    if [ -n "$read_rune" ]; then
        log "created a read rune and a read+spend rune on the node"
    else
        log "could not create runes; fill in $NODE_FILE by hand"
    fi
    write_secret "$NODE_FILE" <<EOF
# Runes created by deploy/install.sh. The read rune names only read methods;
# the spend rune adds the payment methods. The MCP uses the spend rune only
# for spend tools.
LIGHTNING_RUNE=$read_rune
LIGHTNING_SPEND_RUNE=$spend_rune
# clnrest serves a self-signed certificate. 1 skips verification, which is
# acceptable over the podman network; set LIGHTNING_CA_CERT to the node's
# cert.pem and this to 0 for a stricter setup.
LIGHTNING_TLS_INSECURE=1
EOF
    log "wrote $NODE_FILE (mode 600)"
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
