import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from lia.agent.loop import Agent
from lia.liara.tools import LiaraTools


class FakeClient:
    def __init__(self):
        self.restarted = []

    async def list_apps(self):
        return [{"project_id": "shop", "type": "flask", "status": "RUNNING", "planID": "small-g2", "scale": 1}]

    async def restart(self, name):
        self.restarted.append(name)
        return {}


def call(id, name, **args):
    return NS(id=id, type="function", function=NS(name=name, arguments=json.dumps(args)))


def reply(content=None, calls=None):
    msg = NS(content=content, tool_calls=calls,
             model_dump=lambda **_: {"role": "assistant", "content": content,
                                     **({"tool_calls": [{"id": c.id, "type": "function", "function": vars(c.function)}
                                                        for c in calls]} if calls else {})})
    return NS(choices=[NS(message=msg)])


class FakeLLM:
    """Replays scripted replies and records the messages it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []
        self.chat = NS(completions=NS(create=self.create))

    async def create(self, messages, **_):
        self.seen.append(list(messages))
        return self.replies.pop(0)


def collect(gen):
    async def go():
        return [ev async for ev in gen]
    return asyncio.run(go())


@pytest.fixture
def setup(tmp_path):
    client = FakeClient()
    agent = Agent(LiaraTools(client, tmp_path, tmp_path / "state"), None, "k", "m")
    return agent, client


def test_read_tools_run_and_sensitive_waits_for_confirmation(setup):
    agent, client = setup
    agent.llm = FakeLLM([
        reply(calls=[call("c1", "list_apps")]),
        reply("ری‌استارت می‌کنم.", [call("c2", "restart_app", app="shop")]),
        reply("انجام شد."),
    ])
    evs = collect(agent.run("s", "shop را ری‌استارت کن"))
    types = [e["type"] for e in evs]
    assert "tool_result" in types and types[-1] == "confirm"
    assert client.restarted == []  # nothing mutating ran before approval

    confirm = evs[-1]
    evs = collect(agent.resume("s", confirm["id"], approved=True))
    assert client.restarted == ["shop"]
    assert [e["type"] for e in evs][-1] == "message"


def test_decline_does_not_execute(setup):
    agent, client = setup
    agent.llm = FakeLLM([reply(calls=[call("c1", "restart_app", app="shop")]), reply("باشه.")])
    confirm = collect(agent.run("s", "ری‌استارت"))[-1]
    evs = collect(agent.resume("s", confirm["id"], approved=False))
    assert client.restarted == []
    assert evs[0]["type"] == "declined"
    assert '"declined_by_user": true' in agent.llm.seen[-1][-1]["content"]


def test_new_message_implicitly_declines(setup):
    agent, client = setup
    agent.llm = FakeLLM([reply(calls=[call("c1", "restart_app", app="shop")]), reply("باشه.")])
    confirm = collect(agent.run("s", "ری‌استارت"))[-1]
    collect(agent.run("s", "نه، ولش کن"))
    assert client.restarted == []
    assert collect(agent.resume("s", confirm["id"], approved=True))[0]["type"] == "error"


def test_tool_crash_still_answers_the_call(setup):
    agent, client = setup

    async def boom():
        raise KeyError("projects")
    agent.tools.tools["list_apps"].handler = boom
    agent.llm = FakeLLM([reply(calls=[call("c1", "list_apps")]), reply("خطا رخ داد.")])
    evs = collect(agent.run("s", "اپ‌هایم؟"))
    result = next(e for e in evs if e["type"] == "tool_result")
    assert not result["ok"] and "KeyError" in result["result"]["error"]
    assert agent.llm.seen[-1][-1]["role"] == "tool"  # history stays valid for the provider


def test_interrupted_turn_is_repaired_before_next_request(setup):
    agent, _ = setup
    s = agent.session("s")
    s.messages += [{"role": "user", "content": "x"},
                   {"role": "assistant", "content": "", "tool_calls": [
                       {"id": "c1", "type": "function", "function": {"name": "list_apps", "arguments": "{}"}}]}]
    agent.llm = FakeLLM([reply("سلام")])
    collect(agent.run("s", "دوباره"))
    sent = agent.llm.seen[0]
    assert [m["role"] for m in sent[-3:]] == ["assistant", "tool", "user"]
    assert sent[-2]["tool_call_id"] == "c1"


def test_decline_skips_the_rest_of_the_turn(setup):
    agent, client = setup
    agent.llm = FakeLLM([reply(calls=[call("c1", "restart_app", app="shop"),
                                      call("c2", "restart_app", app="shop")]), reply("باشه.")])
    confirm = collect(agent.run("s", "ری‌استارت"))[-1]
    evs = collect(agent.resume("s", confirm["id"], approved=False))
    assert [e["type"] for e in evs].count("confirm") == 0  # no second card right after a decline
    assert client.restarted == []
    assert '"skipped": true' in agent.llm.seen[-1][-1]["content"]
