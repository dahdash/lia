import asyncio
import io
import tarfile

import pytest

from lia.liara.client import LiaraError, build_archive
from lia.liara.tools import LiaraTools, Risk, mask_envs


class FakeClient:
    pass


@pytest.fixture
def tools(tmp_path):
    (tmp_path / "proj").mkdir()
    return LiaraTools(FakeClient(), tmp_path / "proj", tmp_path / "state")


def test_mask_envs():
    out = mask_envs([{"key": "DB_PASSWORD", "value": "x"}, {"key": "DEBUG", "value": "1"}])
    assert out == {"DB_PASSWORD": "***", "DEBUG": "1"}


def test_every_mutating_tool_needs_confirmation(tools):
    read_only = {"list_apps", "get_app_status", "list_plans", "get_logs", "get_events",
                 "get_metrics", "check_site", "list_releases", "diagnose_app", "api_limitations", "list_domains",
                 "detect_project"}
    for t in tools.tools.values():
        assert (t.risk == Risk.READ) == (t.name in read_only), t.name
        if t.risk != Risk.READ:
            assert t.summarize is not None, t.name
        assert t.schema()["function"]["name"] == t.name


def test_archive_skips_ignored(tmp_path):
    (tmp_path / "app.py").write_text("print(1)")
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "x").write_text("")
    names = tarfile.open(fileobj=io.BytesIO(build_archive(tmp_path))).getnames()
    assert names == ["app.py"]


def test_deploy_path_cannot_escape(tools):
    with pytest.raises(LiaraError):
        tools._resolve_project("../../etc")


def test_project_path_accepts_root_prefix_and_lists_projects(tools):
    (tools.projects_dir / "shop").mkdir()
    assert tools._resolve_project("proj/shop") == tools.projects_dir / "shop"
    with pytest.raises(LiaraError, match="Available projects: shop"):
        tools._resolve_project("blog")


def test_rollback_unknown_release_reports_limitation(tools):
    with pytest.raises(LiaraError, match="no rollback endpoint"):
        asyncio.run(tools.rollback("demo", "r1"))


def test_summary_text(tools):
    text = asyncio.run(tools.tools["restart_app"].summarize(app="demo"))
    assert "demo" in text


class PoorClient:
    async def billing(self):
        return {"balance": 0, "minCreditAmount": 20000}


def test_costly_ops_check_balance_first(tmp_path):
    t = LiaraTools(PoorClient(), tmp_path, tmp_path / "state")
    with pytest.raises(LiaraError, match="Not enough account balance") as e:
        asyncio.run(t.create_app("demo", "flask"))
    assert e.value.status == 402
    with pytest.raises(LiaraError, match="20,000"):
        asyncio.run(t.resize_app("demo", "small-g2"))


def test_team_id_sent_on_every_request():
    import httpx
    from lia.liara.client import LiaraClient
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json={"projects": [], "data": []})

    c = LiaraClient("tok", "https://api.liara.ir", team_id="t1")
    c._http._transport = httpx.MockTransport(handler)
    asyncio.run(c.list_apps())
    asyncio.run(c.logs("demo"))
    assert all(p.get("teamID") == "t1" for p in seen) and "start" in seen[1]


def test_v2_log_lines_are_unwrapped():
    import httpx
    from lia.liara.client import LiaraClient
    body = {"data": [{"metaData": {}, "values": [
        ["1792736164696317718", '{"type":"stderr","_entry":"ModuleNotFoundError: x"}'],
        ["1792736163922843548", '{"type":"stdout","_entry":"> [GUNICORN] Starting..."}']]}]}
    c = LiaraClient("tok")
    c._http._transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    assert asyncio.run(c.logs("demo")) == [(1792736163, "> [GUNICORN] Starting..."),
                                           (1792736164, "ModuleNotFoundError: x")]


class LogClient:
    def __init__(self):
        self.release_id = "unset"

    async def releases(self, name, count=10):
        return {"currentRelease": "rel-2"}

    async def logs(self, name, since=None, release_id=None):
        self.release_id = release_id
        return [(1, "  import pkg_resources"),
                (2, "gunicorn/util.py:25: UserWarning: pkg_resources is deprecated"),
                (3, "Traceback (most recent call last):"),
                (4, "KeyError: 'DISCOUNT_PERCENT'"),
                (5, "KeyError: 'DISCOUNT_PERCENT'")]


def test_logs_scoped_to_release_and_exceptions_extracted(tmp_path):
    c = LogClient()
    t = LiaraTools(c, tmp_path, tmp_path / "state")
    out = asyncio.run(t.get_logs("demo", errors_only=True, current_release_only=True))
    assert c.release_id == "rel-2"
    assert out["exceptions"] == [{"line": "KeyError: 'DISCOUNT_PERCENT'", "count": 2}]
    assert not any("Warning" in l for l in out["lines"])


def test_env_change_logged_and_shown_on_release(tools):
    class C:
        envs = [{"key": "API_KEY", "value": "old"}]
        rel = "r1"

        async def get_app(self, app):
            return {"envs": self.envs}

        async def set_envs(self, app, variables):
            self.rel = "r2"

        async def releases(self, app, limit=None):
            return {"currentRelease": self.rel, "releases": [
                {"_id": "r2", "tag": "v2", "type": "ENV_UPDATE", "message": None},
                {"_id": "r1", "tag": "v1", "type": "DEPLOY", "message": "deploy shop"}]}

    tools.c = C()

    async def no_wait(app, before):
        return {"releaseID": "r2", "state": "READY"}
    tools._wait_rollout = no_wait
    asyncio.run(tools.set_env("a", {"MEMORY_HOG_MB": 400, "API_KEY": "new"}))
    rels = asyncio.run(tools.list_releases("a"))["releases"]
    assert rels[0]["env_change"] == {"set": {"MEMORY_HOG_MB": "400", "API_KEY": "***"},
                                     "previous": {"MEMORY_HOG_MB": None, "API_KEY": "***"}}
    assert rels[1]["type"] == "DEPLOY" and "env_change" not in rels[1]


def test_change_hints():
    from lia.liara.tools import change_hints
    latest = {"tag": "v9", "type": "ENV_UPDATE", "created": "t", "env_change": "unknown"}
    oom = [{"action": "oom", "release": "v9"}, {"action": "start", "release": "v8"}]
    [h] = change_hints(latest, oom)
    assert "v9" in h and "unset_env" in h and "NOT help" in h
    assert change_hints(latest, oom + [{"action": "oom", "release": "v8"}]) == []
    assert change_hints(latest, [{"action": "start", "release": "v9"}]) == []


def test_metrics_ordered_oldest_first(tools):
    class C:
        async def metrics_summary(self, app):
            return {"memoryUsage": [{"applet": "new", "value": [100, str(80 * 2**20)]},
                                    {"applet": "old", "value": [100, str(436 * 2**20)]}]}

        async def metric_series(self, app, metric, since=None):
            return {"result": [{"applet": "new", "values": [[900, "1"]]},
                                {"applet": "old", "values": [[100, "1"]]}]}

    tools.c = C()
    assert asyncio.run(tools.get_metrics("a"))["memory_mb_per_container"] == [436.0, 80.0]


def test_llm_supplied_names_cannot_leave_the_api_or_the_app():
    from lia.liara.client import LiaraClient
    from lia.liara.tools import site_url
    c = LiaraClient("t")
    for bad in ("../me", "a?x=1", "a#b", "a/b"):
        with pytest.raises(LiaraError, match="invalid"):
            asyncio.run(c.get_app(bad))
    for bad in ("evil.com#", "a@b", "A B", ""):
        with pytest.raises(LiaraError, match="invalid"):
            site_url(bad)
    asyncio.run(c.aclose())



def test_check_site_path_stays_on_the_app():
    from lia.liara.tools import probe_url
    for path in ("@evil.com", "//evil.com", "/ok", "ok"):
        assert probe_url("shop", path).startswith("https://shop.liara.run/")


def test_domains_connect_with_dns_steps_and_reject_bad_names(tools):
    class C:
        doms = []

        async def domains(self, app):
            return self.doms

        async def add_domain(self, app, name):
            self.doms.append({"_id": "d1", "name": name, "status": "PENDING", "certificatesStatus": "DEACTIVE"})

        async def check_domain(self, domain_id):
            return {**self.doms[-1], "verificationCode": "v-123", "CNameRecord": "INVALID", "acmeRecord": "INVALID"}

        async def delete_domain(self, domain_id):
            self.deleted = domain_id

    tools.c = C()
    out = asyncio.run(tools.add_domain("shop", "https://Shop.Example.ir/"))
    assert out["added"] == "shop.example.ir" and out["domain"]["txt_verification_value"] == "v-123"
    assert out["domain"]["records_check"]["alias_or_cname"] == "INVALID"
    steps = " ".join(out["dns_steps"])
    assert "Liara console" in steps and "v-123" in steps and "nic.ir" in steps
    assert "nic.ir" not in " ".join(asyncio.run(tools.add_domain("shop", "shop.example.com"))["dns_steps"])
    assert "root domain" in " ".join(asyncio.run(tools.add_domain("shop", "example.ir"))["dns_steps"])
    assert asyncio.run(tools.remove_domain("shop", "shop.example.ir"))["removed"] == "shop.example.ir"
    assert tools.c.deleted == "d1"
    for bad in ("localhost", "a b.ir", "shop.liara.run", "../x.ir"):
        with pytest.raises(LiaraError, match="invalid domain"):
            asyncio.run(tools.add_domain("shop", bad))
