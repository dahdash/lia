import pytest

from lia.liara.client import LiaraError
from lia.web.health import Health

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeClient:
    async def me(self):
        return {"plans": {"projects": {"small-g2": {"RAM": {"amount": 0.5}, "CPU": {"amount": 0.5}}}}}


class FakeTools:
    """Only the READ calls the health watch uses; state is flipped per test step."""

    def __init__(self):
        self.c = FakeClient()
        self.up, self.mem_mb, self.cpu, self.err_lines = True, 100.0, 0.05, []
        self.calls = []

    async def list_apps(self):
        return [{"name": "shop", "platform": "flask", "status": "RUNNING", "plan": "small-g2", "scale": 1}]

    async def check_site(self, app, path="/"):
        return {"ok": self.up, "status": 200 if self.up else 500, "ms": 40}

    async def get_metrics(self, app):
        return {"memory_mb_per_container": [self.mem_mb], "cpu_per_container": [self.cpu]}

    async def get_logs(self, app, **kw):
        self.calls.append(kw)
        return {"total_lines": len(self.err_lines), "exceptions": [],
                "lines": [f"12:00:00 {l}" for l in self.err_lines]}


async def test_healthy_app_raises_no_alerts():
    h = Health(FakeTools())
    await h.check_once()
    app = h.apps["shop"]
    assert h.alerts == [] and app["healthy"] and app["issues"] == []
    assert app["ram_pct"] == 20 and app["cpu_pct"] == 10


async def test_error_log_scan_is_read_only_on_current_release():
    t = FakeTools()
    await Health(t).check_once()
    assert t.calls == [{"minutes": 2, "errors_only": True, "limit": 3, "current_release_only": True}]


async def test_alert_fires_once_per_condition_and_clears():
    t = FakeTools()
    h = Health(t)
    await h.check_once()
    t.up, t.mem_mb, t.err_lines = False, 480.0, ["KeyError: 'DB_URL'"]
    await h.check_once()
    await h.check_once()  # still bad: no duplicate alerts
    kinds = [(a["kind"], a["level"]) for a in h.alerts]
    assert kinds == [("down", "bad"), ("errors", "warn"), ("ram", "warn")]
    assert all(a["ask"] for a in h.alerts) and "DB_URL" in h.alerts[1]["text"]
    assert set(h.apps["shop"]["issues"]) == {"down", "errors", "ram"}

    t.up, t.mem_mb, t.err_lines = True, 100.0, []
    await h.check_once()
    assert [(a["kind"], a["level"]) for a in h.alerts[3:]] == [("down", "ok"), ("errors", "ok"), ("ram", "ok")]
    assert h.apps["shop"]["issues"] == []


async def test_already_broken_app_alerts_on_first_check():
    t = FakeTools()
    t.up = False
    h = Health(t)
    await h.check_once()
    assert [a["kind"] for a in h.alerts] == ["down"]


async def test_api_failure_is_reported_not_raised():
    t = FakeTools()

    async def boom():
        raise LiaraError("401 unauthorized")
    t.list_apps = boom
    h = Health(t)
    await h.check_once()
    assert h.alerts[0]["kind"] == "api" and "401" in h.alerts[0]["text"]
