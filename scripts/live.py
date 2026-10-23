"""Drive Lia tools directly against Liara (dev/demo prep). Usage: .venv/bin/python scripts/live.py <step> [args], from the repo root."""
import asyncio, json, sys, time

from lia.config import get_settings
from lia.liara.tools import build_tools

APP = "lia-demo"


def show(label, obj):
    print(f"== {label}")
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str)[:6000])


async def main(step, *args):
    t = build_tools(get_settings())  # same projects dir and deploy log as the web server
    c = t.c
    try:
        if step == "networks":
            show("networks", await c.networks())
        elif step == "net-create":
            show("create_network", await c.create_network("lia-net"))
            show("networks", await c.networks())
        elif step == "create":
            show("create_app", await t.create_app(APP, "flask", "small-g2", "basic"))
            show("get_app", await c.get_app(APP))
        elif step == "deploy":
            path = args[0] if args else "shop"
            show("deploy", await t.deploy(APP, path, "flask", 80, f"deploy {path}"))
        elif step == "status":
            show("status", await t.get_app_status(APP))
        elif step == "logs":
            raw = await c._request("GET", f"/v2/projects/{APP}/logs", params={
                "start": int(time.time()) - 3600, "direction": "forward", "last_lines": "false"})
            show("raw v2 logs", raw)
            show("parsed", (await c.logs(APP))[-20:])
        elif step == "events":
            show("events", await c.events(APP))
        elif step == "metrics":
            show("summary", await c.metrics_summary(APP))
            show("memory", await c.metric_series(APP, "memory"))
        elif step == "releases":
            show("releases", await c.releases(APP))
        elif step == "diagnose":
            show("diagnose", await t.diagnose_app(APP))
        elif step == "site":
            show("site", await t.check_site(APP))
        elif step == "set-env":
            show("set_env", await t.set_env(APP, dict(a.split("=", 1) for a in args)))
            show("envs", (await c.get_app(APP)).get("envs"))
        elif step == "unset-env":
            show("unset_env", await t.unset_env(APP, list(args)))
        elif step == "rollback":
            show("rollback", await t.rollback(APP, args[0]))
        elif step == "restart":
            show("restart", await t.restart_app(APP))
        elif step == "history":
            show("deploys", t.deploys.history(APP))
        elif step == "delete":
            show("delete", await t.delete_app(APP))
        elif step == "net-delete":
            nets = await c.networks()
            for n in nets:
                if n.get("name") == "lia-net":
                    show("delete net", await c._request("DELETE", f"/v1/networks/{n['_id']}"))
        else:
            sys.exit(f"unknown step {step}")
    finally:
        await c.aclose()


asyncio.run(main(*sys.argv[1:]))
