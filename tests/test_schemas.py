"""What the server advertises in `tools/list`.

With spending disabled (the default) only the read tools are registered --
there is no way to reach a payment tool at all.
"""

from __future__ import annotations

import anyio
import pytest
from mcp import Client

import server

READ_TOOLS = {
    "getinfo",
    "listfunds",
    "listchannels",
    "listpeers",
    "listinvoices",
    "listpays",
    "decode",
}

SPEND_TOOLS = {"invoice", "pay", "keysend", "withdraw", "close"}


@pytest.fixture
def advertised() -> dict:
    async def _list():
        async with Client(server.mcp) as c:
            return await c.list_tools()

    listing = anyio.run(_list)
    return {tool.name: tool for tool in listing.tools}


def test_only_read_tools_are_registered_by_default(advertised: dict) -> None:
    assert set(advertised) == READ_TOOLS
    assert SPEND_TOOLS & set(advertised) == set()


@pytest.mark.parametrize("name", sorted(READ_TOOLS))
def test_text_tools_publish_no_output_schema(advertised: dict, name: str) -> None:
    assert advertised[name].output_schema is None, (
        f"{name} publishes an output schema; pass structured_output=False"
    )


def test_every_default_tool_is_read_only(advertised: dict) -> None:
    for name, tool in advertised.items():
        assert tool.annotations is not None, f"{name} publishes no annotations"
        assert tool.annotations.read_only_hint is True, f"{name} is not marked read-only"


def test_spend_tool_names_match_the_gate() -> None:
    assert server.SPEND_TOOLS == SPEND_TOOLS
