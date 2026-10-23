import json

import pytest

pytest.importorskip("mcp")

from mcp import Client

from lia.liara.client import LiaraError
from lia.liara.tools import LiaraTools, Risk
from lia.mcp_server import ConfirmGate, build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeClient:
    def __init__(self):
        self.restarted = []

    async def list_apps(self):
        return [{"project_id": "shop", "type": "flask", "status": "RUNNING", "planID": "small-g2", "scale": 1}]

    async def restart(self, app):
        if app == "missing":
            raise LiaraError("app not found", status=404)
        self.restarted.append(app)


@pytest.fixture
def setup(tmp_path):
    fake = FakeClient()
    tools = LiaraTools(fake, tmp_path, tmp_path / "state")
    return fake, tools, build_server(tools)


def payload(res):
    return json.loads(res.content[0].text)


async def test_lists_every_tool_with_tiers(setup):
    _, tools, server = setup
    async with Client(server) as c:
        listed = {t.name: t for t in (await c.list_tools()).tools}
    assert set(listed) == set(tools.tools)
    for name, t in tools.tools.items():
        m = listed[name]
        assert m.annotations.read_only_hint == (t.risk == Risk.READ)
        assert ("confirm_token" in m.input_schema["properties"]) == (t.risk != Risk.READ)


async def test_read_tool_runs_directly(setup):
    _, _, server = setup
    async with Client(server) as c:
        res = await c.call_tool("list_apps", {})
    assert not res.is_error
    assert payload(res)["result"][0]["name"] == "shop"


async def test_sensitive_tool_needs_matching_token(setup):
    fake, _, server = setup
    async with Client(server) as c:
        first = payload(await c.call_tool("restart_app", {"app": "shop"}))
        assert first["status"] == "confirmation_required" and fake.restarted == []
        assert "shop" in first["summary"]
        token = first["confirm_token"]

        # token for `shop` cannot restart a different app
        wrong = await c.call_tool("restart_app", {"app": "other", "confirm_token": token})
        assert wrong.is_error and fake.restarted == []

        # the failed attempt consumed it; ask again
        token = payload(await c.call_tool("restart_app", {"app": "shop"}))["confirm_token"]
        ok = await c.call_tool("restart_app", {"app": "shop", "confirm_token": token})
        assert not ok.is_error and fake.restarted == ["shop"]

        # one-time use
        again = await c.call_tool("restart_app", {"app": "shop", "confirm_token": token})
        assert again.is_error and fake.restarted == ["shop"]


async def test_liara_errors_are_reported(setup):
    _, _, server = setup
    async with Client(server) as c:
        token = payload(await c.call_tool("restart_app", {"app": "missing"}))["confirm_token"]
        res = await c.call_tool("restart_app", {"app": "missing", "confirm_token": token})
    assert res.is_error
    assert payload(res) == {"status": "failed", "tool": "restart_app", "error": "app not found",
                            "http_status": 404}


def test_token_expires():
    gate = ConfirmGate(ttl=-1)
    token = gate.issue("restart_app", {"app": "shop"})
    assert not gate.redeem(token, "restart_app", {"app": "shop"})
