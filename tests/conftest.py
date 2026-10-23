import asyncio

import pytest


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    """Tools wait for Liara rollouts with asyncio.sleep; skip the waiting in tests."""
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **kw):
        await real_sleep(0)

    monkeypatch.setattr("lia.liara.tools.asyncio.sleep", fast_sleep)

    async def fake_probe(self, app, path="/"):
        return {"url": f"https://{app}.liara.run{path}", "status": 200, "ok": True}

    monkeypatch.setattr("lia.liara.tools.LiaraTools.check_site", fake_probe)
