"""Unit tests for the clnrest client.

Drives `CLNRest` with a fake `urlopen`: the URL is `/v1/<method>`, the rune
header is the read rune for reads and the spend rune for `spend=True`, a
success returns the decoded object, an error body's code/message are carried,
and a transport failure is named.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error

import pytest

import rpc as rpc_module
from rpc import CLNRest, RPCError

URL = "https://lightningd:3010"


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _install(monkeypatch, *, body=None, error=None, captured=None):
    def fake_urlopen(request, timeout=None, context=None):
        if captured is not None:
            captured.append(request)
        if error is not None:
            raise error
        return _Response(json.dumps(body).encode())

    monkeypatch.setattr(rpc_module.urllib.request, "urlopen", fake_urlopen)


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "err", email.message.Message(), io.BytesIO(body))


def test_success_returns_the_decoded_object(monkeypatch) -> None:
    _install(monkeypatch, body={"id": "abc", "blockheight": 5})
    assert CLNRest(URL, "READ", insecure=True).call("getinfo") == {
        "id": "abc",
        "blockheight": 5,
    }


def test_read_uses_the_read_rune_and_spend_uses_the_spend_rune(monkeypatch) -> None:
    captured: list = []
    _install(monkeypatch, body={"ok": True}, captured=captured)
    client = CLNRest(URL, "READ", "SPEND", insecure=True)

    client.call("getinfo")
    assert captured[-1].get_header("Rune") == "READ"
    assert captured[-1].full_url == f"{URL}/v1/getinfo"

    client.call("pay", {}, spend=True)
    assert captured[-1].get_header("Rune") == "SPEND"


def test_spend_falls_back_to_the_read_rune_when_unset(monkeypatch) -> None:
    captured: list = []
    _install(monkeypatch, body={"ok": True}, captured=captured)
    CLNRest(URL, "READ", insecure=True).call("pay", {}, spend=True)
    assert captured[-1].get_header("Rune") == "READ"


def test_rune_refusal_carries_the_code(monkeypatch) -> None:
    _install(
        monkeypatch,
        error=_http_error(401, b'{"code": 1502, "message": "Not permitted"}'),
    )
    with pytest.raises(RPCError) as excinfo:
        CLNRest(URL, "READ", insecure=True).call("pay")
    assert excinfo.value.code == 1502
    assert excinfo.value.message == "Not permitted"


def test_unreachable_node_is_named(monkeypatch) -> None:
    _install(monkeypatch, error=urllib.error.URLError("connection refused"))
    with pytest.raises(RPCError) as excinfo:
        CLNRest(URL, "READ", insecure=True).call("getinfo")
    assert "cannot reach the lightning node" in excinfo.value.message
