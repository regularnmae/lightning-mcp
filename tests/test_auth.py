"""The bearer-token / spend-token gate, driven as a pure ASGI app.

No socket: a fake inner app records whether it was reached and what body it
saw, so these tests pin that /health is open, that the read token admits reads
but not spends, and that a spend call needs the spend token.
"""

from __future__ import annotations

import json

import pytest

import server
from server import BearerAuthMiddleware

READ = "read-token"
SPEND = "spend-token"

SPEND_CALL = json.dumps(
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "pay"}}
).encode()
READ_CALL = json.dumps(
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "getinfo"}}
).encode()


class Inner:
    """A stand-in for the streamable-HTTP app."""

    def __init__(self) -> None:
        self.called = False
        self.body: bytes | None = None

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        message = await receive()
        self.body = message.get("body")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def scope(path: str = "/mcp", method: str = "POST", token: str | None = None) -> dict:
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return {"type": "http", "path": path, "method": method, "headers": headers}


async def drive(middleware, sc: dict, body: bytes = b"") -> int | None:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await middleware(sc, receive, send)
    return next((m["status"] for m in sent if m["type"] == "http.response.start"), None)


@pytest.fixture
def mw() -> tuple[BearerAuthMiddleware, Inner]:
    inner = Inner()
    return BearerAuthMiddleware(inner, READ, SPEND), inner


@pytest.mark.anyio
async def test_health_is_open(mw) -> None:
    middleware, inner = mw
    assert await drive(middleware, scope(path="/health", method="GET")) == 200
    assert inner.called is False


@pytest.mark.anyio
async def test_missing_token_is_401(mw) -> None:
    middleware, inner = mw
    assert await drive(middleware, scope()) == 401
    assert inner.called is False


@pytest.mark.anyio
async def test_wrong_token_is_401(mw) -> None:
    middleware, inner = mw
    assert await drive(middleware, scope(token="nope")) == 401
    assert inner.called is False


@pytest.mark.anyio
async def test_read_token_admits_reads_and_replays_the_body(mw) -> None:
    middleware, inner = mw
    assert await drive(middleware, scope(token=READ), READ_CALL) == 200
    assert inner.called is True
    assert inner.body == READ_CALL


@pytest.mark.anyio
async def test_read_token_cannot_pay(mw) -> None:
    middleware, inner = mw
    assert await drive(middleware, scope(token=READ), SPEND_CALL) == 403
    assert inner.called is False


@pytest.mark.anyio
async def test_spend_token_may_pay(mw) -> None:
    middleware, inner = mw
    assert await drive(middleware, scope(token=SPEND), SPEND_CALL) == 200
    assert inner.called is True


def test_enabling_spending_requires_a_spend_token(monkeypatch) -> None:
    monkeypatch.setattr(server, "ENABLE_SPENDING", True)
    with pytest.raises(RuntimeError):
        BearerAuthMiddleware(Inner(), READ, None)


@pytest.fixture(autouse=True)
def _clear_ready_cache():
    """The readiness answer is a module global; start every test cold."""
    server._ready_cache = None
    server._ready_lock = None
    yield
    server._ready_cache = None
    server._ready_lock = None


async def drive_json(middleware, sc: dict) -> tuple[int | None, dict]:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await middleware(sc, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body) if body else {}


@pytest.mark.anyio
async def test_ready_reports_the_node_state(mw, monkeypatch) -> None:
    middleware, inner = mw
    monkeypatch.setattr(
        server.rpc,
        "call",
        lambda method, params=None: {
            "blockheight": 975_153,
            "num_peers": 0,
            "warning_bitcoind_sync": "still in ibd",
        },
    )
    status, body = await drive_json(middleware, scope(path="/ready", method="GET"))
    assert status == 200
    assert body == {
        "status": "ready",
        "blockheight": 975_153,
        "num_peers": 0,
        "warning": "still in ibd",
    }
    assert inner.called is False


@pytest.mark.anyio
async def test_ready_is_unready_when_the_node_does_not_answer(mw, monkeypatch) -> None:
    from rpc import RPCError

    middleware, _inner = mw

    def _fail(method, params=None):
        raise RPCError("cannot reach the lightning node")

    monkeypatch.setattr(server.rpc, "call", _fail)
    status, body = await drive_json(middleware, scope(path="/ready", method="GET"))
    assert status == 503
    assert body["status"] == "unready"
    assert "cannot reach" in body["error"]


@pytest.mark.anyio
async def test_ready_needs_no_token(mw, monkeypatch) -> None:
    middleware, _inner = mw
    monkeypatch.setattr(server.rpc, "call", lambda method, params=None: {"num_peers": 1})
    assert (await drive_json(middleware, scope(path="/ready", method="GET")))[0] == 200


@pytest.mark.anyio
async def test_ready_is_cached_between_requests(mw, monkeypatch) -> None:
    middleware, _inner = mw
    calls = []

    def _info(method, params=None):
        calls.append(method)
        return {"num_peers": 0}

    monkeypatch.setattr(server.rpc, "call", _info)
    await drive_json(middleware, scope(path="/ready", method="GET"))
    await drive_json(middleware, scope(path="/ready", method="GET"))
    assert calls == ["getinfo"]


@pytest.mark.anyio
async def test_a_similar_path_is_not_ready(mw) -> None:
    middleware, inner = mw
    assert await drive(middleware, scope(path="/readyz", method="GET")) == 401
    assert inner.called is False
