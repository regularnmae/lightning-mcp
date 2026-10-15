"""Minimal client for Core Lightning's clnrest HTTP API.

Stdlib-only (plus `ssl`): this server POSTs JSON to one daemon on the podman
network, so it needs no HTTP framework and no TLS library beyond the standard
one. The smaller closure is less to audit for a server that can move funds.

clnrest is a thin REST skin over `lightning-cli`: `POST /v1/<method>` with the
method's parameters as the JSON body, authorised by a **rune** in the `rune`
header. A rune can be restricted to a set of methods, so a read-only rune
physically cannot call `pay` -- this client carries two runes, a read rune and
a spend rune, and uses the spend rune only for the write methods.

TLS: clnrest serves a self-signed certificate. Point `ca_cert` at the node's
`cert.pem` to verify it, or set `insecure` to skip verification (acceptable
only because the endpoint is reached over the podman network).
"""

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

DEFAULT_TIMEOUT = 60.0


class RPCError(Exception):
    """A failed RPC call.

    `message` is CLN's human-readable text; `code` is CLN's numeric error code
    when supplied (1502 for a rune refusal), or None for a transport failure.
    """

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _ssl_context(ca_cert: str, insecure: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if ca_cert:
        context.load_verify_locations(cafile=ca_cert)
    elif insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class CLNRest:
    """A client bound to one clnrest endpoint."""

    def __init__(
        self,
        base_url: str,
        rune: str,
        spend_rune: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        ca_cert: str = "",
        insecure: bool = False,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._rune = rune
        self._spend_rune = spend_rune
        self._timeout = timeout
        self._context = _ssl_context(ca_cert, insecure)

    def call(self, method: str, params: dict[str, Any] | None = None, spend: bool = False) -> Any:
        """Invoke `method`, authorised by the read rune or the spend rune."""
        rune = self._spend_rune if spend and self._spend_rune else self._rune
        payload = json.dumps(params or {}).encode()
        headers = {"Content-Type": "application/json", "rune": rune}
        request = urllib.request.Request(
            f"{self._base}/v1/{method}", data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout, context=self._context
            ) as response:
                parsed = _decode(response.read())
        except urllib.error.HTTPError as err:
            parsed = _decode(err.read())
            if parsed is not None:
                code = parsed.get("code")
                raise RPCError(
                    str(parsed.get("message") or parsed.get("error") or f"HTTP {err.code}"),
                    code if isinstance(code, int) else err.code,
                ) from err
            raise RPCError(f"HTTP {err.code} from the lightning node") from err
        except urllib.error.URLError as err:
            raise RPCError(
                f"cannot reach the lightning node at {self._base}: {err.reason}"
            ) from err

        if parsed is None:
            raise RPCError("the lightning node returned a non-JSON body")
        return parsed


def _decode(raw: bytes) -> dict[str, Any] | None:
    """Parse a JSON object from a response body, or None if it is not one."""
    try:
        parsed = json.loads(raw.decode(errors="replace"))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
