"""Liara operations exposed to the agent, each with a risk tier.

Tiers:
  READ      - read-only, runs automatically
  SENSITIVE - changes state; runs only after the user confirms
  COSTLY    - creates/changes billed resources; confirmation shows the price
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .client import FINAL_RELEASE_STATES, LiaraClient, LiaraError, build_archive


class Risk(str, Enum):
    READ = "read"
    SENSITIVE = "sensitive"
    COSTLY = "costly"


@dataclass
class Tool:
    name: str
    description: str  # for the LLM (English)
    label: str  # for the user (Persian)
    risk: Risk
    params: dict  # JSON schema "properties"
    required: list[str]
    handler: Callable[..., Awaitable[Any]]
    summarize: Callable[..., Awaitable[str]] | None = None  # Persian confirmation text

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"[{self.risk.value}] {self.description}",
                "parameters": {"type": "object", "properties": self.params,
                               "required": self.required},
            },
        }


# Things Liara's API cannot do; the agent must say so instead of faking them.
API_LIMITATIONS = [
    "No dedicated rollback endpoint: Lia rolls back by re-releasing a source it uploaded "
    "earlier itself; releases deployed by other tools cannot be rolled back via API.",
    "No horizontal scaling: an app can only be turned on (1) or off (0).",
    "No HTTP request metrics or error rates: only CPU, memory, network and disk; 5xx errors "
    "are detected from logs and by probing the site.",
    "No shell/exec via REST (Liara has a WebSocket console; Lia does not run commands in containers).",
    "No alert/notification, health-check or cron configuration endpoints.",
    "No server-side log search; logs are fetched by time window and filtered by Lia.",
    "Domain DNS targets: the API reports whether each DNS record of a connected domain is set and the TXT "
    "verification value, but not the ALIAS/CNAME target values; Lia points the user to the domain page in the "
    "Liara console for those.",
    "No domain registration: buying a domain (e.g. .ir at IRNIC, nic.ir) and editing its DNS records "
    "at the registrar/DNS provider are done by the user; Lia only connects an owned domain to an app "
    "and reports its DNS/SSL status.",
]

SECRET_KEY_RE = re.compile(r"(PASS|SECRET|TOKEN|KEY|PRIVATE|CREDENTIAL|DSN|DATABASE_URL)", re.I)
ERROR_RE = re.compile(
    r"(error|exception|traceback|fatal|critical|panic|killed|oom|out of memory|"
    r"\b5\d\d\b|refused|timeout|cannot|failed|unhandled)", re.I)
# Final line of a Python traceback / generic "SomethingError: msg" line.
EXCEPTION_RE = re.compile(r"^\s*([\w.]+(?:Error|Exception|Exit|Interrupt))(?::\s*(.*))?$")


def mask_envs(envs: list[dict]) -> dict[str, str]:
    out = {}
    for e in envs:
        v = str(e.get("value", ""))
        out[e["key"]] = "***" if SECRET_KEY_RE.search(e["key"]) and v else v
    return out


def clean_domain(domain: str) -> str:
    d = str(domain).strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/.")
    if not DOMAIN_RE.fullmatch(d) or d.endswith(".liara.run"):
        raise LiaraError(f"invalid domain {domain!r} (expected something like shop.example.ir)", status=400)
    return d


def domain_info(d: dict) -> dict:
    """List entry or /check result. The API reports whether each DNS record is set (CNameRecord /
    acmeRecord are check results like INVALID, not targets) and the TXT verification code; it does
    not return the ALIAS/CNAME target values."""
    out = {"name": d.get("name"), "status": d.get("status"), "ssl": d.get("certificatesStatus"),
           "redirect_to": d.get("redirectTo") or None}
    if "CNameRecord" in d or "acmeRecord" in d:
        out["records_check"] = {"alias_or_cname": d.get("CNameRecord"), "ssl_acme_cname": d.get("acmeRecord")}
    if d.get("verificationCode"):
        out["txt_verification_value"] = d["verificationCode"]
    return out


def dns_steps(domain: str, verification: str | None) -> list[str]:
    apex = domain.count(".") == 1 or (domain.count(".") == 2 and domain.endswith((".co.ir", ".ac.ir", ".org.ir", ".gov.ir")))
    steps = ["Liara needs three records at the domain's DNS provider. Their exact names and values are shown "
             "on this domain's page in the Liara console (Domains); Liara's API does not return the targets.",
             f"1) Pointing record for {domain} to Liara's server: "
             + ("it is a root domain, so use ALIAS/ANAME/CNAME-flattening (Liara DNS and Cloudflare support it; "
                f"otherwise connect www.{domain} and redirect the root to it)." if apex else "a CNAME record."),
             "2) TXT record that proves ownership"
             + (f", value: {verification}." if verification else "."),
             "3) CNAME record for SSL (DNS-01 challenge) so Liara can issue and renew the certificate."]
    if domain.endswith(".ir"):
        steps.append("IRNIC (nic.ir) does not hold DNS records for .ir domains: log in at nic.ir with the domain "
                     "holder's NIC handle, open the domain, and set its DNS servers (nameservers) to those of "
                     "the DNS provider where the records above are added. Nameserver changes at IRNIC can take "
                     "a few hours to apply.")
    else:
        steps.append("Add the records in the DNS panel of the domain's registrar or DNS provider; changes can "
                     "take a few minutes to a few hours.")
    steps.append("Then ask Lia to check the domain again: status becomes ACTIVE and SSL is issued automatically.")
    return steps


BAD_EVENTS = {"oom", "die", "kill"}


def change_hints(latest: dict | None, events: Any) -> list[str]:
    """Spell out what a small model tends to miss: did the trouble start with the last change?"""
    if not latest or not isinstance(events, list):
        return []
    bad = [e for e in events if isinstance(e, dict) and e.get("action") in BAD_EVENTS]
    if not bad or any(e.get("release") != latest["tag"] for e in bad):
        return []
    kinds = ", ".join(sorted({e["action"] for e in bad}))
    hint = (f"All {len(bad)} crash events ({kinds}) belong to the current release {latest['tag']} "
            f"({latest['type']}, {latest['created']}), the latest change; earlier releases have none. "
            "That change is the most likely root cause: undo it first (free), a bigger plan is only the alternative.")
    if latest["type"] == "ENV_UPDATE":
        change = latest.get("env_change")
        what = (f"Variables set: {change['set']}, previous values: {change['previous']}."
                if isinstance(change, dict) else
                "It was changed outside Lia, so the API does not say which variable; pick the one in "
                "status.envs that best explains the symptom (e.g. a memory/cache/worker size for oom).")
        hint += (f" It changed env vars only: {what} Fix with unset_env or set_env to the previous value; "
                 "rollback would NOT help (env vars are not part of a release).")
    elif latest["type"] == "DEPLOY":
        hint += " It deployed new code: fix with rollback to the previous good release."
    return [hint]


APP_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
DOMAIN_RE = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


def site_url(name: str) -> str:
    if not APP_NAME_RE.fullmatch(name or ""):  # keeps probes on *.liara.run
        raise LiaraError(f"invalid app name {name!r}", status=400)
    return f"https://{name}.liara.run"


def probe_url(name: str, path: str = "/") -> str:
    return site_url(name) + "/" + path.lstrip("/")  # "@host" or "//host" must not leave the app


def platform_config(platform: str) -> dict:
    # CLI default versions (utils/get-supported-versions.ts)
    if platform in ("flask", "django"):
        return {"pythonVersion": "3.12"}
    if platform == "python":
        return {"version": "3.12"}
    return {}


class DeployLog:
    """Remembers source IDs Lia uploaded, so it can roll back (no API for that)."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def add(self, app: str, entry: dict) -> None:
        data = self._load()
        data.setdefault(app, []).append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def history(self, app: str) -> list[dict]:
        return self._load().get(app, [])


def build_tools(settings) -> "LiaraTools":
    """The one bootstrap shared by the web UI, the MCP server and scripts; close with `tools.c.aclose()`."""
    client = LiaraClient(settings.liara_api_key, settings.liara_api_base, settings.liara_team_id)
    return LiaraTools(client, settings.projects_dir, settings.state_dir)


class LiaraTools:
    def __init__(self, client: LiaraClient, projects_dir: Path, state_dir: Path):
        self.c = client
        self.projects_dir = projects_dir.resolve()
        self.deploys = DeployLog(state_dir / "deploys.json")
        # Liara keeps no history of env values; remember the changes Lia made (secrets masked)
        self.env_changes = DeployLog(state_dir / "env_changes.json")
        self.tools: dict[str, Tool] = {t.name: t for t in self._define()}

    # ---- helpers ------------------------------------------------------
    async def _plan(self, plan_id: str) -> dict | None:
        return next((p for p in await self.c.project_plans() if p["id"] == plan_id), None)

    async def _bundle(self, plan_id: str, bundle_id: str | None = None) -> tuple[str, dict]:
        """Pick the feature bundle: the requested one, else the cheapest paid one with rollback."""
        bundles = await self.c.bundle_plans(plan_id)
        if bundle_id:
            if bundle_id not in bundles:
                raise LiaraError(f"bundle `{bundle_id}` is not available for plan `{plan_id}`; "
                                 f"available: {', '.join(bundles)}")
            return bundle_id, bundles[bundle_id]
        for k in ("basic", "default"):
            if k in bundles:
                return k, bundles[k]
        if not bundles:
            raise LiaraError(f"no feature bundle is available for plan `{plan_id}`")
        k = min(bundles, key=lambda b: bundles[b].get("price", 0))
        return k, bundles[k]

    def _resolve_project(self, path: str) -> Path:
        # users often include the projects folder itself ("demo/shop" when the root is demo/)
        prefix = self.projects_dir.name + "/"
        if not (self.projects_dir / path).exists() and path.strip("./").startswith(prefix):
            path = path.strip("./")[len(prefix):]
        p = (self.projects_dir / path).resolve()
        if self.projects_dir not in p.parents and p != self.projects_dir:
            raise LiaraError(f"deploy path must be inside {self.projects_dir}")
        if not p.is_dir():
            available = sorted(d.name for d in self.projects_dir.iterdir()
                               if d.is_dir() and not d.name.startswith("."))
            raise LiaraError(f"project directory not found: {path}. "
                             f"Available projects: {', '.join(available) or 'none'}")
        return p

    # ---- READ ---------------------------------------------------------
    async def list_apps(self) -> Any:
        apps = await self.c.list_apps()
        return [{"name": a.get("project_id") or a.get("name"), "platform": a.get("type"),
                 "status": a.get("status"), "plan": a.get("planID"), "scale": a.get("scale")}
                for a in apps]

    async def get_app_status(self, app: str) -> Any:
        p = await self.c.get_app(app)
        port = p.get("port")
        if port is None:  # the project has no port field; each release carries it
            try:
                rel = await self.c.releases(app, 1)
                cur = next((r for r in rel.get("releases", []) if r.get("_id") == rel.get("currentRelease")), None)
                port = (cur or {}).get("port")
            except (LiaraError, AttributeError):
                pass
        return {
            "name": app, "url": site_url(app), "platform": p.get("type"),
            "status": p.get("status"), "scale": p.get("scale"), "plan": p.get("planID"),
            "port": port, "created": p.get("created_at"),
            "envs": mask_envs(p.get("envs", [])),
        }

    async def list_plans(self) -> Any:
        """g2 plans; billed price = plan price + the feature bundle's price (totals below)."""
        plans = (await self.c.me())["plans"]
        return [{"id": k, "ram_gb": p["RAM"]["amount"], "cpu": p["CPU"]["amount"],
                 "bundles": {b: {"total_toman_per_hour": round(p["price"] + v.get("price", 0), 2),
                                 "total_toman_per_month": int((p.get("displayPrice") or p["price"] * 720)
                                                              + v.get("displayPrice", 0)),
                                 "rollback": v.get("rollback"), "build_timeout_min": v.get("buildTimeout"),
                                 "log_retention_days": v.get("maxLogsRetention")}
                             for b, v in plans["projectBundlePlans"].get(k, {}).items() if v.get("available")}}
                for k, p in plans["projects"].items() if k.endswith("-g2") and p.get("available")]

    async def get_logs(self, app: str, minutes: int = 60, errors_only: bool = False,
                       limit: int = 80, current_release_only: bool = False) -> Any:
        release_id = None
        if current_release_only:
            release_id = (await self.c.releases(app, 1)).get("currentRelease")
        lines = await self.c.logs(app, since=int(time.time()) - minutes * 60, release_id=release_id)
        exceptions: dict[str, int] = {}
        for _, s in lines:
            if m := EXCEPTION_RE.match(s):
                exceptions[s.strip()] = exceptions.get(s.strip(), 0) + 1
        if errors_only:
            lines = [l for l in lines if ERROR_RE.search(l[1]) and "Warning" not in l[1]]
        return {"release_id": release_id, "total_lines": len(lines),
                "exceptions": [{"line": k, "count": v} for k, v in
                               sorted(exceptions.items(), key=lambda kv: -kv[1])],
                "lines": [f"{time.strftime('%H:%M:%S', time.localtime(t))} {s}"
                          for t, s in lines[-limit:]]}

    async def get_events(self, app: str, hours: int = 24) -> Any:
        data = await self.c.events(app, since=int(time.time()) - hours * 3600)
        events = (data.get("data") or data.get("events") or []) if isinstance(data, dict) else data
        if not isinstance(events, list):
            return events
        # container lifecycle (create/start/die/stop/oom/...) per release tag
        return [{"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(e["time"]))),
                 "action": e.get("action") or e.get("status"), "release": e.get("tag")}
                if isinstance(e, dict) and str(e.get("time", "")).isdigit() else e
                for e in events[:40]]

    async def get_metrics(self, app: str) -> Any:
        data, started = await asyncio.gather(self.c.metrics_summary(app), self._applet_starts(app))
        if not isinstance(data, dict):
            return data

        def series(key, scale=1.0, nd=1):  # one value per container (applet), oldest first
            rows = [v for v in data.get(key) or [] if isinstance(v, dict) and len(v.get("value", [])) == 2]
            rows.sort(key=lambda v: started.get(v.get("applet"), 0))
            return [round(float(v["value"][1]) * scale, nd) for v in rows]
        return {"memory_mb_per_container": series("memoryUsage", 1 / 2**20),
                "cpu_per_container": series("cpuUsage", 1, 3),
                "disk_per_container": series("disksUsage", 1, 3),
                "note": "containers are ordered oldest to newest; during a rollout the old container "
                        "keeps running for a few minutes, so the LAST value is the new release"}

    async def _applet_starts(self, app: str) -> dict[str, int]:
        """First memory point of each container in the last hour = when it started (roughly)."""
        try:
            d = await self.c.metric_series(app, "memory")
            return {r["applet"]: r["values"][0][0] for r in d["result"] if r.get("values")}
        except (LiaraError, KeyError, TypeError, AttributeError, IndexError):
            return {}

    async def check_site(self, app: str, path: str = "/") -> Any:
        url = probe_url(app, path)
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as h:
                r = await h.get(url)
            return {"url": url, "status": r.status_code, "ok": r.status_code < 500,
                    "ms": int((time.monotonic() - t0) * 1000), "body_start": r.text[:300]}
        except httpx.HTTPError as e:
            return {"url": url, "ok": False, "error": f"{type(e).__name__}: {e}"}

    def _release_info(self, app: str, r: dict, env_log: dict) -> dict:
        # type: DEPLOY (new code) or ENV_UPDATE (env vars changed, same code)
        info = {"id": r["_id"], "tag": r.get("tag"), "type": r.get("type"), "state": r.get("state"),
                "created": r.get("createdAt"),
                "message": r.get("message") or (r.get("gitInfo") or {}).get("message")}
        if r.get("type") == "ENV_UPDATE":
            change = env_log.get(r["_id"])
            info["env_change"] = ({"set": change["set"], "previous": change["previous"]} if change else
                                  "not made by Lia; Liara's API does not say which variables changed")
        return info

    async def list_releases(self, app: str) -> Any:
        data = await self.c.releases(app)
        env_log = {e.get("releaseID"): e for e in self.env_changes.history(app)}
        return {"current": data.get("currentRelease"),
                "releases": [self._release_info(app, r, env_log) for r in data.get("releases", [])],
                "rollback_candidates": self.deploys.history(app)}

    async def diagnose_app(self, app: str) -> Any:
        """Collect every signal at once; the LLM ranks root causes from it."""
        async def safe(coro):
            try:
                return await coro
            except LiaraError as e:
                return {"error": str(e)}
        status, site, logs, events, metrics, releases = await asyncio.gather(
            safe(self.get_app_status(app)), safe(self.check_site(app)),
            safe(self.get_logs(app, minutes=60, errors_only=True, limit=40, current_release_only=True)),
            safe(self.get_events(app, hours=6)), safe(self.get_metrics(app)),
            safe(self.list_releases(app)))
        plan = None
        if isinstance(status, dict) and status.get("plan"):
            p = await safe(self._plan(status["plan"]))
            plan = {"id": status["plan"], "ram_gb": p["RAM"]["amount"]} if isinstance(p, dict) and "RAM" in p else None
        latest = None  # the last change made to the app: compare with when the trouble started
        if isinstance(releases, dict):
            latest = next((r for r in releases.get("releases", []) if r["id"] == releases.get("current")), None)
        return {"hints": change_hints(latest, events), "status": status, "site_probe": site,
                "error_logs": logs, "events": events, "metrics": metrics, "plan": plan,
                "current_release": latest, "releases": releases}

    async def detect_project(self, path: str) -> Any:
        """Guess platform and port like `liara deploy` does (CLI utils/detect-platform.ts)."""
        root = self._resolve_project(path)
        files = {f.name for f in root.iterdir()}
        read = lambda n: (root / n).read_text(errors="ignore").lower() if n in files else ""
        reqs = read("requirements.txt") + read("Pipfile")
        pkg = read("package.json")
        if "Dockerfile" in files:
            platform = "docker"
        elif "manage.py" in files or "django" in reqs:
            platform = "django"
        elif "flask" in reqs:
            platform = "flask"
        elif reqs:
            platform = "python"
        elif "composer.json" in files:
            platform = "laravel" if "laravel" in read("composer.json") else "php"
        elif pkg:
            platform = next((p for p in ("next", "angular", "vue", "react") if f'"{p}' in pkg), "node")
        elif "go.mod" in files:
            platform = "go"
        else:
            platform = "static"
        port = {"next": 3000, "node": 3000, "go": 8080, "docker": 3000}.get(platform, 80)
        try:
            price = await self._price_text("small-g2")
        except Exception:  # price is informative only; detection must still work
            price = None
        return {"path": path, "platform": platform, "port": port,
                "files": sorted(f for f in files if not f.startswith((".", "__")))[:30],
                "suggested_plan": "small-g2", "suggested_plan_price": price,
                "next": "Tell the user platform, port and this price, then call create_app "
                        "(if the app does not exist yet) - the user confirms the cost on its card."}

    async def api_limitations(self) -> Any:
        return API_LIMITATIONS

    async def list_domains(self, app: str) -> Any:
        doms = await self.c.domains(app)
        return {"app": app, "default_url": site_url(app),
                "domains": [domain_info(await self._checked(d)) for d in doms],
                "note": "Only domains already connected to this app. Liara serves a domain after its "
                        "DNS records are set (records_check no longer INVALID) and status is ACTIVE; SSL is issued after that."}

    async def _checked(self, d: dict) -> dict:
        try:
            return await self.c.check_domain(d["_id"])
        except LiaraError:
            return d

    # ---- SENSITIVE ----------------------------------------------------
    async def add_domain(self, app: str, domain: str) -> Any:
        domain = clean_domain(domain)
        await self.c.add_domain(app, domain)
        found = next((d for d in await self.c.domains(app) if d.get("name") == domain), None)
        if found:
            found = await self._checked(found)
        return {"added": domain, "app": app, "domain": domain_info(found) if found else None,
                "dns_steps": dns_steps(domain, (found or {}).get("verificationCode"))}

    async def remove_domain(self, app: str, domain: str) -> Any:
        domain = clean_domain(domain)
        found = next((d for d in await self.c.domains(app) if d.get("name") == domain), None)
        if not found:
            raise LiaraError(f"domain {domain} is not connected to app {app}", status=404)
        await self.c.delete_domain(found["_id"])
        return {"removed": domain, "app": app}

    async def restart_app(self, app: str) -> Any:
        await self.c.restart(app)
        await asyncio.sleep(20)  # container restart; probe after it is back
        return {"restarted": app, "site_probe": await self._probe_until_up(app)}

    async def stop_app(self, app: str) -> Any:
        await self.c.scale(app, False)
        return {"stopped": app}

    async def start_app(self, app: str) -> Any:
        await self.c.scale(app, True)
        return {"started": app}

    async def set_env(self, app: str, variables: dict) -> Any:
        variables = {k: str(v) for k, v in variables.items()}
        old = await self._envs(app)
        before = await self._current_release(app)
        await self.c.set_envs(app, variables)
        result = await self._wait_rollout(app, before)
        self._log_env_change(app, result, old, variables)
        return {"updated_keys": list(variables), **result}

    async def unset_env(self, app: str, keys: list[str]) -> Any:
        old = await self._envs(app)
        before = await self._current_release(app)
        await self.c.unset_envs(app, keys)
        result = await self._wait_rollout(app, before)
        self._log_env_change(app, result, old, {k: None for k in keys})
        return {"removed_keys": keys, **result}

    async def _envs(self, app: str) -> dict[str, str]:
        try:
            return mask_envs((await self.c.get_app(app)).get("envs", []))
        except LiaraError:
            return {}

    def _log_env_change(self, app: str, result: dict, old: dict, new: dict[str, str | None]) -> None:
        masked = {k: ("***" if v and SECRET_KEY_RE.search(k) else v) for k, v in new.items()}
        self.env_changes.add(app, {"releaseID": result.get("releaseID"), "set": masked,
                                   "previous": {k: old.get(k) for k in new},
                                   "at": time.strftime("%Y-%m-%d %H:%M:%S")})

    async def _current_release(self, app: str) -> str | None:
        return (await self.c.releases(app, 1)).get("currentRelease")

    async def _wait_rollout(self, app: str, before: str | None) -> dict:
        """Env changes make Liara roll out a new release; wait for it so the probe sees new code."""
        for _ in range(15):
            await asyncio.sleep(4)
            current = await self._current_release(app)
            if current and current != before:
                return await self._wait_release(app, current)
        return {"note": "no new release seen yet; the app restarts to apply the change",
                "site_probe": await self.check_site(app)}

    async def deploy(self, app: str, path: str, platform: str, port: int,
                     message: str = "") -> Any:
        archive = build_archive(self._resolve_project(path))
        source_id = await self.c.upload_source(app, archive)
        release_id = await self.c.create_release(app, source_id, platform, port, message or None,
                                                 platform_config(platform))
        self.deploys.add(app, {"sourceID": source_id, "releaseID": release_id, "path": path,
                               "platform": platform, "port": port, "message": message,
                               "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        return await self._wait_release(app, release_id)

    async def rollback(self, app: str, release_id: str) -> Any:
        entry = next((d for d in self.deploys.history(app) if d["releaseID"] == release_id), None)
        if not entry:
            raise LiaraError("Liara's API has no rollback endpoint and this release was not "
                             "deployed by Lia, so its source is unknown. Redeploy that code instead.")
        new_id = await self.c.create_release(app, entry["sourceID"], entry["platform"],
                                             entry["port"], f"rollback to {release_id}",
                                             platform_config(entry["platform"]))
        self.deploys.add(app, {**entry, "releaseID": new_id, "message": f"rollback to {release_id}",
                               "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        return await self._wait_release(app, new_id)

    async def _wait_release(self, app: str, release_id: str, timeout: int = 600) -> dict:
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            state = (await self.c.release(release_id)).get("state")
            if state in FINAL_RELEASE_STATES:
                break
            await asyncio.sleep(4)
        result: dict[str, Any] = {"releaseID": release_id, "state": state, "url": site_url(app)}
        if state != "READY":
            try:
                result["build_log_tail"] = (await self.c.build_logs(release_id))[-30:]
            except LiaraError as e:
                result["build_log_error"] = str(e)
        else:
            # READY means built and scheduled, not serving: the new container writes its first
            # log line ~10s later and the old one keeps answering ~10s after that (measured)
            started = int(time.time()) - 120
            for _ in range(20):
                try:
                    if await self.c.logs(app, since=started, release_id=release_id):
                        break
                except LiaraError:
                    pass
                await asyncio.sleep(3)
            await asyncio.sleep(15)
            result["site_probe"] = await self._probe_until_up(app)
        return result

    async def _probe_until_up(self, app: str) -> dict:
        for _ in range(12):
            probe = await self.check_site(app)
            if probe.get("status") not in (None, 502, 503, 504):
                break
            await asyncio.sleep(5)
        return probe

    async def delete_app(self, app: str) -> Any:
        await self.c.delete_app(app)
        return {"deleted": app}

    # ---- COSTLY -------------------------------------------------------
    async def _require_balance(self) -> None:
        """Liara answers billable calls with a bare `402 Not enough balance`; explain it first."""
        b = await self.c.billing()
        balance, minimum = b.get("balance", 0), b.get("minCreditAmount", 0)
        if balance < minimum:
            raise LiaraError(f"Not enough account balance: {int(balance):,} toman, Liara requires at "
                             f"least {int(minimum):,} toman for billable operations. Top up the "
                             "account in the Liara console (Billing) and try again.", status=402)

    async def create_app(self, app: str, platform: str, plan_id: str = "small-g2",
                         bundle_id: str | None = None) -> Any:
        await self._require_balance()
        bundle_id, _ = await self._bundle(plan_id, bundle_id)
        network = await self._network()
        await self.c.create_app(app, platform, plan_id, bundle_id, network["_id"])
        return {"created": app, "plan": plan_id, "bundle": bundle_id, "network": network.get("name"),
                "url": site_url(app)}

    async def _network(self) -> dict:
        nets = await self.c.networks()
        if not nets:
            raise LiaraError("Liara requires every app to be in a private network and this account "
                             "has none. Create one first in the Liara console (Networks) or with "
                             "`liara network create`; Lia does not create networks itself.")
        return nets[0]

    async def resize_app(self, app: str, plan_id: str) -> Any:
        await self._require_balance()
        await self.c.resize(app, plan_id)
        return {"resized": app, "plan": plan_id}

    # ---- confirmation texts (Persian) ---------------------------------
    async def _price_text(self, plan_id: str, bundle_id: str | None = None) -> str:
        p = await self._plan(plan_id)
        if not p:
            return f"پلن `{plan_id}` (قیمت در دسترس نیست)"
        try:
            bundle_id, b = await self._bundle(plan_id, bundle_id)
        except LiaraError:
            bundle_id, b = None, p
        price = p["price"] + (b.get("price", 0) if b is not p else 0)
        monthly = (p.get("displayPrice") or p["price"] * 720) + \
            (b.get("displayPrice", 0) if b is not p else 0)
        extra = f" + بسته‌ی امکانات `{bundle_id}`" if bundle_id else ""
        text = (f"پلن `{plan_id}` — {p['RAM']['amount']} گیگ رم، {p['CPU']['amount']} هسته{extra} — "
                f"حدود {int(price):,} تومان در ساعت ({int(monthly):,} تومان در ماه)")
        try:
            bal = await self.c.billing()
            text += f". موجودی حساب: {int(bal.get('balance', 0)):,} تومان"
            if bal.get("balance", 0) < bal.get("minCreditAmount", 0):
                text += f" — کمتر از حداقل {int(bal['minCreditAmount']):,} تومان؛ لیارا این کار را رد می‌کند"
        except LiaraError:
            pass
        return text

    async def _sum_create(self, app: str, platform: str, plan_id: str = "small-g2",
                          bundle_id: str | None = None) -> str:
        return (f"ساخت اپ جدید `{app}` با پلتفرم `{platform}` روی "
                f"{await self._price_text(plan_id, bundle_id)}. این کار هزینه دارد.")

    async def _sum_resize(self, app: str, plan_id: str) -> str:
        cur = (await self.c.get_app(app)).get("planID")
        return f"تغییر پلن `{app}` از `{cur}` به {await self._price_text(plan_id)}. هزینه‌ی ماهانه تغییر می‌کند."

    def _define(self) -> list[Tool]:
        S = {"type": "string"}
        app = {"app": {"type": "string", "description": "Liara app name (id)"}}

        def txt(fmt):
            async def f(**kw):
                return fmt.format(**{k: (", ".join(v) if isinstance(v, list) else
                                         ", ".join(v) if isinstance(v, dict) else v)
                                     for k, v in kw.items()})
            return f

        R, SE, C = Risk.READ, Risk.SENSITIVE, Risk.COSTLY
        return [
            Tool("list_apps", "List the user's Liara apps.", "فهرست اپ‌ها", R, {}, [], self.list_apps),
            Tool("get_app_status", "App details: status, plan, scale, port, env vars (secrets masked).",
                 "وضعیت اپ", R, app, ["app"], self.get_app_status),
            Tool("list_plans", "Available app plans with RAM/CPU and price in toman.",
                 "فهرست پلن‌ها", R, {}, [], self.list_plans),
            Tool("get_logs", "Runtime logs of the app for the last N minutes; errors_only filters error-like lines. `exceptions` lists distinct exception lines (most frequent first) - usually the root cause.",
                 "خواندن لاگ‌ها", R,
                 {**app, "minutes": {"type": "integer", "default": 60},
                  "errors_only": {"type": "boolean", "default": False},
                  "limit": {"type": "integer", "default": 80},
                  "current_release_only": {"type": "boolean", "default": False,
                                           "description": "Only logs of the running release."}},
                 ["app"], self.get_logs),
            Tool("get_events", "Platform events (deploys, restarts, crashes, OOM kills) for the last N hours.",
                 "رویدادهای اپ", R, {**app, "hours": {"type": "integer", "default": 24}}, ["app"], self.get_events),
            Tool("get_metrics", "Current CPU, memory and disk usage summary.", "مصرف منابع", R, app, ["app"], self.get_metrics),
            Tool("check_site", "Probe the app's public URL and return HTTP status and latency.",
                 "بررسی دسترس‌پذیری سایت", R, {**app, "path": {"type": "string", "default": "/"}}, ["app"], self.check_site),
            Tool("list_releases", "Release history and releases Lia can roll back to.",
                 "تاریخچه‌ی انتشارها", R, app, ["app"], self.list_releases),
            Tool("diagnose_app", "Collect status, site probe, error logs, events, metrics and releases in one call. "
                 "Use first when the user reports a problem.", "عیب‌یابی جامع", R, app, ["app"], self.diagnose_app),
            Tool("detect_project", "Inspect a local project directory (relative to the projects dir) and suggest "
                 "Liara platform, port and plan.", "تشخیص نوع پروژه", R, {"path": S}, ["path"], self.detect_project),
            Tool("list_domains", "Custom domains connected to the app with status, SSL and whether each DNS record is set.",
                 "دامنه‌های اپ", R, app, ["app"], self.list_domains),
            Tool("api_limitations", "What Liara's API cannot do. Use when a request may be unsupported.",
                 "محدودیت‌های API لیارا", R, {}, [], self.api_limitations),

            Tool("restart_app", "Restart the app.", "ری‌استارت اپ", SE, app, ["app"], self.restart_app,
                 txt("ری‌استارت اپ `{app}` — سایت چند ثانیه در دسترس نخواهد بود.")),
            Tool("stop_app", "Turn the app off (scale 0).", "خاموش کردن اپ", SE, app, ["app"], self.stop_app,
                 txt("خاموش کردن اپ `{app}` — سایت از دسترس خارج می‌شود.")),
            Tool("start_app", "Turn the app on (scale 1).", "روشن کردن اپ", SE, app, ["app"], self.start_app,
                 txt("روشن کردن اپ `{app}`.")),
            Tool("set_env", "Set or change environment variables (merged with existing ones; app restarts).",
                 "تنظیم متغیر محیطی", SE, {**app, "variables": {"type": "object", "additionalProperties": S}},
                 ["app", "variables"], self.set_env, txt("تغییر متغیرهای محیطی `{variables}` در اپ `{app}` — اپ ری‌استارت می‌شود.")),
            Tool("unset_env", "Remove environment variables (app restarts).", "حذف متغیر محیطی", SE,
                 {**app, "keys": {"type": "array", "items": S}}, ["app", "keys"], self.unset_env,
                 txt("حذف متغیرهای محیطی `{keys}` از اپ `{app}` — اپ ری‌استارت می‌شود.")),
            Tool("deploy", "Upload a project directory (relative to the projects dir) and release it; waits "
                 "for the build and probes the site.", "دیپلوی", SE,
                 {**app, "path": S, "platform": {"type": "string", "description": "flask, django, node, docker, static, ..."},
                  "port": {"type": "integer"}, "message": S}, ["app", "path", "platform", "port"], self.deploy,
                 txt("دیپلوی پوشه‌ی `{path}` روی اپ `{app}` (پلتفرم `{platform}`، پورت {port}).")),
            Tool("rollback", "Re-release an earlier source Lia deployed (release_id from list_releases.rollback_candidates).",
                 "بازگشت به نسخه‌ی قبلی", SE, {**app, "release_id": S}, ["app", "release_id"], self.rollback,
                 txt("بازگرداندن اپ `{app}` به نسخه‌ی `{release_id}`.")),
            Tool("add_domain", "Connect a domain the user already owns to the app; returns the DNS records the user must set.",
                 "اتصال دامنه", SE, {**app, "domain": {"type": "string", "description": "e.g. shop.example.ir"}},
                 ["app", "domain"], self.add_domain,
                 txt("اتصال دامنه‌ی `{domain}` به اپ `{app}` — بعد از آن باید رکورد DNS دامنه را تنظیم کنید.")),
            Tool("remove_domain", "Disconnect a custom domain from the app.", "قطع دامنه", SE,
                 {**app, "domain": S}, ["app", "domain"], self.remove_domain,
                 txt("قطع دامنه‌ی `{domain}` از اپ `{app}` — سایت دیگر روی این دامنه باز نمی‌شود.")),
            Tool("delete_app", "Permanently delete an app.", "حذف اپ", SE, app, ["app"], self.delete_app,
                 txt("⚠️ حذف دائمی اپ `{app}` — قابل بازگشت نیست.")),

            Tool("create_app", "Create a new app (billed hourly).", "ساخت اپ", C,
                 {**app, "platform": S, "plan_id": {"type": "string", "default": "small-g2"},
                  "bundle_id": {"type": "string", "description": "feature bundle (basic/standard/pro); default: basic"}},
                 ["app", "platform"], self.create_app, self._sum_create),
            Tool("resize_app", "Change the app's plan (RAM/CPU; billing changes).", "تغییر پلن", C,
                 {**app, "plan_id": S}, ["app", "plan_id"], self.resize_app, self._sum_resize),
        ]
