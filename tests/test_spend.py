"""The payment path: caps, the budget, dry runs, and the spend rune.

`_run_payment` is the one function the money-moving tools funnel through, so
it is tested directly with a fake node and an injected budget.
"""

from __future__ import annotations

import server
from server import DailyBudget, _precheck, _run_payment


def test_precheck_enforces_caps(monkeypatch) -> None:
    monkeypatch.setattr(server, "MAX_PAYMENT_PER_CALL_SATS", 1000.0)
    assert _precheck(0) is not None
    assert _precheck(1001) is not None
    assert _precheck(1000) is None


def test_refusal_never_reaches_the_node(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        server.rpc, "call", lambda m, p=None, spend=False: calls.append(m) or {}
    )
    monkeypatch.setattr(server, "MAX_PAYMENT_PER_CALL_SATS", 100.0)
    out = _run_payment("keysend", 500, {"destination": "x"}, False, "x")
    assert out.startswith("Refused:")
    assert calls == []


def test_dry_run_skips_the_budget(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        server.rpc,
        "call",
        lambda m, p=None, spend=False: calls.append((m, spend)) or {"payment_preimage": "aa"},
    )
    monkeypatch.setattr(server, "MAX_PAYMENT_PER_CALL_SATS", 1e9)
    # A zero daily budget must still admit a dry run.
    monkeypatch.setattr(server, "budget", DailyBudget(0.0))
    out = _run_payment("pay", 1000, {"bolt11": "x", "dryrun": True}, True, "node")
    assert "aa" in out
    assert calls == [("pay", True)]  # spend rune used


def test_committed_payment_consumes_the_daily_budget(monkeypatch) -> None:
    monkeypatch.setattr(server.rpc, "call", lambda m, p=None, spend=False: {"ok": 1})
    monkeypatch.setattr(server, "MAX_PAYMENT_PER_CALL_SATS", 1e9)
    monkeypatch.setattr(server, "budget", DailyBudget(1000.0))
    assert not _run_payment("keysend", 1000, {}, False, "x").startswith("Refused:")
    assert _run_payment("keysend", 1, {}, False, "x").startswith("Refused:")


def test_failed_payment_releases_the_reservation(monkeypatch) -> None:
    from rpc import RPCError

    def boom(m, p=None, spend=False):
        raise RPCError("no route", 205)

    monkeypatch.setattr(server.rpc, "call", boom)
    monkeypatch.setattr(server, "MAX_PAYMENT_PER_CALL_SATS", 1e9)
    monkeypatch.setattr(server, "budget", DailyBudget(1000.0))
    assert "no route" in _run_payment("keysend", 1000, {}, False, "x")
    # Released, so a second identical payment gets through to the node again.
    assert "no route" in _run_payment("keysend", 1000, {}, False, "x")
