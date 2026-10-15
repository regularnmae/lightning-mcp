import hmac
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from rpc import CLNRest, RPCError

logger = logging.getLogger("lightning-mcp")

_LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_LEVELS: tuple[_LogLevel, ...] = get_args(_LogLevel)


def _log_level() -> _LogLevel:
    requested = os.environ.get("LOG_LEVEL", "INFO").upper()
    return requested if requested in _LEVELS else "INFO"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r; using %s", name, raw, default)
        return default


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "") == "1"


# --- config ---------------------------------------------------------------
LIGHTNING_REST_URL = os.environ.get("LIGHTNING_REST_URL", "https://lightningd:3010")
LIGHTNING_RUNE = os.environ.get("LIGHTNING_RUNE", "")
LIGHTNING_SPEND_RUNE = os.environ.get("LIGHTNING_SPEND_RUNE", "")
LIGHTNING_CA_CERT = os.environ.get("LIGHTNING_CA_CERT", "")
# Self-signed clnrest cert: verify against the node's cert.pem, or skip
# verification over the internal podman network.
LIGHTNING_TLS_INSECURE = _env_flag("LIGHTNING_TLS_INSECURE")
RPC_TIMEOUT = _env_float("LIGHTNING_RPC_TIMEOUT", 60.0)

# Spending is OFF by default; the spend tools are not registered without it.
ENABLE_SPENDING = _env_flag("ENABLE_SPENDING")

# Caps are in satoshis. Enabling spending requires both positive: "unlimited"
# is not a default worth having on a mainnet node.
MAX_PAYMENT_PER_CALL_SATS = _env_float("MAX_PAYMENT_PER_CALL_SATS", 0.0)
MAX_PAYMENT_PER_DAY_SATS = _env_float("MAX_PAYMENT_PER_DAY_SATS", 0.0)

if ENABLE_SPENDING and (MAX_PAYMENT_PER_CALL_SATS <= 0 or MAX_PAYMENT_PER_DAY_SATS <= 0):
    raise RuntimeError(
        "ENABLE_SPENDING=1 requires MAX_PAYMENT_PER_CALL_SATS and "
        "MAX_PAYMENT_PER_DAY_SATS to be set to positive values."
    )

MCP_ALLOW_ANONYMOUS = _env_flag("MCP_ALLOW_ANONYMOUS")
HEALTH_PATH = "/health"

# The read token gates the endpoint; the spend token is additionally required
# for the write tools. With spending enabled a spend token must be set.
TOKEN_ENV = "LIGHTNING_MCP_TOKEN"
SPEND_TOKEN_ENV = "LIGHTNING_MCP_SPEND_TOKEN"

rpc = CLNRest(
    LIGHTNING_REST_URL,
    LIGHTNING_RUNE,
    LIGHTNING_SPEND_RUNE,
    timeout=RPC_TIMEOUT,
    ca_cert=LIGHTNING_CA_CERT,
    insecure=LIGHTNING_TLS_INSECURE,
)

# Every tool name that changes node state or moves funds.
SPEND_TOOLS = frozenset({"invoice", "pay", "keysend", "withdraw", "close"})


mcp = MCPServer(
    "Lightning Node",
    instructions=(
        "Access to this host's Core Lightning node over clnrest. Read tools "
        "report node, channel, peer and invoice state, and decode BOLT11 "
        "invoices. Payment tools (invoice, pay, keysend, withdraw, close) are "
        "present only when the operator enabled them, and are subject to "
        "per-payment and per-day caps. Prefer pay(..., dry_run=true) to "
        "preview a payment before committing it."
    ),
    log_level=_log_level(),
)

_READ_TOOLS = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_WRITE_TOOL = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


class SpendRefused(Exception):
    """A payment was rejected locally by a cap."""


class DailyBudget:
    """Tracks committed spend against a per-day ceiling, in satoshis.

    Only non-dry-run payments are reserved; the counter resets on a new UTC
    day. Guarded by a lock because tools run on worker threads.
    """

    def __init__(self, limit: float, clock: Callable[[], float] = time.time) -> None:
        self._limit = limit
        self._clock = clock
        self._lock = threading.Lock()
        self._day = ""
        self._spent = 0.0

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), tz=UTC).strftime("%Y-%m-%d")

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._spent = 0.0

    def reserve(self, amount: float) -> None:
        with self._lock:
            self._roll()
            if self._spent + amount > self._limit:
                raise SpendRefused(
                    f"daily cap of {self._limit} sats would be exceeded "
                    f"({self._spent} sats already committed today)"
                )
            self._spent += amount

    def release(self, amount: float) -> None:
        with self._lock:
            self._roll()
            self._spent = max(0.0, self._spent - amount)


budget = DailyBudget(MAX_PAYMENT_PER_DAY_SATS)


def _render(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, indent=2)


def _error_text(err: RPCError) -> str:
    if err.code is None:
        return err.message
    return f"{err.message} (RPC error {err.code})"


def _call(method: str, params: dict[str, Any] | None = None) -> str:
    """Run one read method and render the result, or a readable error."""
    try:
        return _render(rpc.call(method, params))
    except RPCError as err:
        logger.warning("%s failed: %s", method, err.message)
        return _error_text(err)
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in %s", method)
        return f"Unexpected error calling {method}: {err}"


def _audit(action: str, amount_sats: float, destination: str, detail: str) -> None:
    logger.info(
        "SPEND action=%s amount=%s sats destination=%s detail=%s",
        action,
        amount_sats,
        destination or "-",
        detail,
    )


def _precheck(amount_sats: float) -> str | None:
    if amount_sats <= 0:
        return "amount must be greater than zero"
    if amount_sats > MAX_PAYMENT_PER_CALL_SATS:
        return (
            f"amount {amount_sats} sats exceeds the per-call cap of "
            f"{MAX_PAYMENT_PER_CALL_SATS} sats"
        )
    return None


# --- read tools -----------------------------------------------------------


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def getinfo() -> str:
    """Return node information: id, alias, network, block height, peers."""
    return _call("getinfo")


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def listfunds() -> str:
    """Return on-chain outputs and channel funds."""
    return _call("listfunds")


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def listchannels() -> str:
    """Return the node's channels."""
    return _call("listchannels")


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def listpeers() -> str:
    """Return the node's peers."""
    return _call("listpeers")


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def listinvoices(label: str = "") -> str:
    """Return invoices; optionally only one by label.

    Args:
        label: If given, return only the invoice with this label.
    """
    params = {"label": label} if label else None
    return _call("listinvoices", params)


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def listpays(bolt11: str = "") -> str:
    """Return outgoing payments; optionally only one by bolt11.

    Args:
        bolt11: If given, return only the payment for this invoice.
    """
    params = {"bolt11": bolt11} if bolt11 else None
    return _call("listpays", params)


@mcp.tool(structured_output=False, annotations=_READ_TOOLS)
def decode(bolt11: str) -> str:
    """Decode a BOLT11 invoice without paying it.

    Args:
        bolt11: The BOLT11 invoice string.
    """
    return _call("decode", {"string": bolt11})


# --- spend tools (registered only when ENABLE_SPENDING=1) -----------------


def _call_spend(
    action: str, params: dict[str, Any], amount_sats: float = 0.0, destination: str = ""
) -> str:
    """A write that moves no funds directly (invoice, close): no caps, but the
    spend rune and the spend token are still required."""
    try:
        result = rpc.call(action, params, spend=True)
    except RPCError as err:
        _audit(action, amount_sats, destination, f"FAILED: {err.message}")
        return _error_text(err)
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in %s", action)
        return f"Unexpected error calling {action}: {err}"
    _audit(action, amount_sats, destination, "done")
    return _render(result)


def _run_payment(
    action: str, amount_sats: float, params: dict[str, Any], dry_run: bool, destination: str
) -> str:
    """Shared payment path: precheck caps, reserve the budget, call, audit."""
    refusal = _precheck(amount_sats)
    if refusal is not None:
        _audit(action, amount_sats, destination, f"REFUSED: {refusal}")
        return f"Refused: {refusal}"

    reserved = False
    if not dry_run:
        try:
            budget.reserve(amount_sats)
            reserved = True
        except SpendRefused as err:
            _audit(action, amount_sats, destination, f"REFUSED: {err}")
            return f"Refused: {err}"

    try:
        result = rpc.call(action, params, spend=True)
    except RPCError as err:
        if reserved:
            budget.release(amount_sats)
        _audit(action, amount_sats, destination, f"FAILED: {err.message}")
        return _error_text(err)
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        if reserved:
            budget.release(amount_sats)
        logger.exception("unexpected error in %s", action)
        return f"Unexpected error calling {action}: {err}"

    detail = "dry-run" if dry_run else "sent"
    _audit(action, amount_sats, destination, detail)
    return _render(result)


if ENABLE_SPENDING:

    @mcp.tool(
        structured_output=False,
        annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False),
    )
    def invoice(
        amount_sats: Annotated[int, Field(ge=0)],
        label: str,
        description: str = "",
        expiry: Annotated[int, Field(ge=1, le=86400 * 365)] = 3600,
    ) -> str:
        """Create a BOLT11 invoice to receive a payment.

        Moves no funds; gated behind the spend token anyway.

        Args:
            amount_sats: Amount to request, in satoshis (0 for an
                any-amount invoice).
            label: Unique label for the invoice.
            description: Description shown to the payer.
            expiry: Seconds until the invoice expires (default 3600).
        """
        params = {
            "amount_msat": amount_sats * 1000,
            "label": label,
            "description": description,
            "expiry": expiry,
        }
        return _call_spend("invoice", params, amount_sats, label)

    @mcp.tool(structured_output=False, annotations=_WRITE_TOOL)
    def pay(bolt11: str, dry_run: bool = True, max_fee_percent: float = 0.5) -> str:
        """Pay a BOLT11 invoice.

        Subject to the per-payment and per-day caps. With dry_run=True (the
        default) the payment is checked and simulated but nothing is sent.

        Args:
            bolt11: The BOLT11 invoice to pay.
            dry_run: Check without sending (default True).
            max_fee_percent: Maximum routing fee as a percentage of the
                amount (default 0.5).
        """
        try:
            decoded = rpc.call("decode", {"string": bolt11})
        except RPCError as err:
            return _error_text(err)
        amount_msat = decoded.get("amount_msat")
        amount_sats = amount_msat // 1000 if isinstance(amount_msat, int) else 0
        params: dict[str, Any] = {"bolt11": bolt11}
        if isinstance(amount_msat, int) and amount_msat > 0:
            params["maxfee"] = int(amount_msat * max_fee_percent / 100)
        if dry_run:
            params["dryrun"] = True
        return _run_payment(
            "pay", amount_sats, params, dry_run, str(decoded.get("payee", ""))
        )

    @mcp.tool(structured_output=False, annotations=_WRITE_TOOL)
    def keysend(destination: str, amount_sats: Annotated[int, Field(ge=1)]) -> str:
        """Send a spontaneous (keysend) payment to a node id.

        There is no dry-run for keysend: this sends immediately. Subject to
        the caps.

        Args:
            destination: Destination node id (hex).
            amount_sats: Amount to send, in satoshis.
        """
        params = {"destination": destination, "amount_msat": amount_sats * 1000}
        return _run_payment("keysend", amount_sats, params, False, destination)

    @mcp.tool(structured_output=False, annotations=_WRITE_TOOL)
    def withdraw(
        destination: str, amount_sats: Annotated[int, Field(ge=1)], feerate: str = "normal"
    ) -> str:
        """Send on-chain funds to a Bitcoin address.

        There is no dry-run for withdraw: this sends immediately. Subject to
        the caps. "Send all" is deliberately not supported.

        Args:
            destination: Destination Bitcoin address.
            amount_sats: Amount to send, in satoshis.
            feerate: On-chain fee rate, e.g. "normal" or "urgent".
        """
        params = {"destination": destination, "satoshi": amount_sats, "feerate": feerate}
        return _run_payment("withdraw", amount_sats, params, False, destination)

    @mcp.tool(structured_output=False, annotations=_WRITE_TOOL)
    def close(
        id: str, unilateral: bool = False
    ) -> str:
        """Close a channel, returning funds on-chain.

        Destructive and immediate; gated behind the spend token but not
        subject to the per-payment caps.

        Args:
            id: Channel id, short channel id, or peer id.
            unilateral: Force a unilateral close (default False).
        """
        return _call_spend("close", {"id": id, "unilateral": unilateral})


class BearerAuthMiddleware:
    """Pure-ASGI bearer auth plus the spend-token gate.

    Two tokens: the read token admits the endpoint; the spend token is
    additionally required for any `tools/call` naming a spend tool. So a
    client handed only the read token cannot move funds even when spending is
    enabled.

    A missing read token is a startup error. GET /health stays unauthenticated.
    """

    def __init__(self, app: Any, read_token: str | None, spend_token: str | None) -> None:
        self.app = app
        self.read_token = read_token or ""
        self.spend_token = spend_token or ""
        if not self.read_token:
            if not MCP_ALLOW_ANONYMOUS:
                raise RuntimeError(
                    f"{TOKEN_ENV} is not set, and this endpoint is published on "
                    "the LAN. Set the token, or set MCP_ALLOW_ANONYMOUS=1 to "
                    "serve without authentication."
                )
            logger.warning(
                "%s not set and MCP_ALLOW_ANONYMOUS=1: endpoint is UNAUTHENTICATED",
                TOKEN_ENV,
            )
        if ENABLE_SPENDING and not self.spend_token:
            raise RuntimeError(
                f"ENABLE_SPENDING=1 requires {SPEND_TOKEN_ENV} to be set, so the "
                "read token alone cannot call the spend tools."
            )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") == HEALTH_PATH:
            from starlette.responses import JSONResponse

            response = JSONResponse({"status": "ok"})
            await response(scope, receive, send)
            return

        presented = self._presented(scope)
        # Either token grants read access; the spend token is a superset.
        if self.read_token and not (
            self._matches(presented, self.read_token)
            or self._matches(presented, self.spend_token)
        ):
            await self._reject(send, 401, "unauthorized")
            return

        if scope.get("method") == "POST":
            body = await _read_body(receive)
            if self._is_privileged(body) and not self._matches(presented, self.spend_token):
                await self._reject(send, 403, "this token may not call spend tools")
                return
            receive = _replay(body, receive)

        await self.app(scope, receive, send)

    def _is_privileged(self, body: bytes) -> bool:
        try:
            payload = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return False
        messages = payload if isinstance(payload, list) else [payload]
        for message in messages:
            if not isinstance(message, dict) or message.get("method") != "tools/call":
                continue
            if (message.get("params") or {}).get("name") in SPEND_TOOLS:
                return True
        return False

    def _presented(self, scope: Any) -> str:
        headers = dict(scope.get("headers") or [])
        header = headers.get(b"authorization", b"").decode("latin-1").strip()
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer":
            return ""
        return presented.strip()

    @staticmethod
    def _matches(presented: str, token: str) -> bool:
        if not token:
            return False
        return hmac.compare_digest(presented.encode(), token.encode())

    async def _reject(self, send: Any, status: int, message: str) -> None:
        from starlette.responses import JSONResponse

        response = JSONResponse({"error": message}, status_code=status)
        await response({"type": "http"}, _empty_receive, send)


async def _empty_receive() -> dict[str, Any]:
    return {"type": "http.disconnect"}


async def _read_body(receive: Any) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay(body: bytes, original: Any) -> Any:
    """A receive() that hands the already-read body to the app once, then
    delegates to the original so a later await still sees the real
    disconnect."""
    replayed = False

    async def receive() -> dict[str, Any]:
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await original()

    return receive


if __name__ == "__main__":
    import uvicorn

    app = BearerAuthMiddleware(
        mcp.streamable_http_app(
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )
        ),
        os.environ.get(TOKEN_ENV),
        os.environ.get(SPEND_TOKEN_ENV),
    )

    uvicorn.run(
        app,
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8000")),
    )
