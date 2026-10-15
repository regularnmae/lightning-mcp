"""Read-tool behaviour against a scripted node."""

from __future__ import annotations

import json

import pytest

from rpc import RPCError


@pytest.mark.anyio
async def test_getinfo_uses_the_read_rune(call, fake_rpc) -> None:
    fake_rpc.answers["getinfo"] = {"id": "abc", "blockheight": 971537}
    text = await call("getinfo")
    assert json.loads(text)["blockheight"] == 971537
    # spend=False means the read rune.
    assert fake_rpc.calls == [("getinfo", None, False)]


@pytest.mark.anyio
async def test_listinvoices_only_sends_label_when_given(call, fake_rpc) -> None:
    await call("listinvoices")
    await call("listinvoices", label="lbl")
    assert fake_rpc.calls == [
        ("listinvoices", None, False),
        ("listinvoices", {"label": "lbl"}, False),
    ]


@pytest.mark.anyio
async def test_decode_forwards_the_invoice(call, fake_rpc) -> None:
    await call("decode", bolt11="lnbc1x")
    assert fake_rpc.calls == [("decode", {"string": "lnbc1x"}, False)]


@pytest.mark.anyio
async def test_rune_refusal_is_surfaced_with_its_code(call, fake_rpc) -> None:
    fake_rpc.answers["getinfo"] = RPCError("Not permitted", 1502)
    text = await call("getinfo")
    assert "Not permitted" in text
    assert "1502" in text
