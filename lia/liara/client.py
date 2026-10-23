"""Thin async client for Liara's REST API.

Endpoints follow the official OpenAPI spec (github.com/liara-cloud/openapi) and
the official CLI (github.com/liara-cloud/cli); see docs/research/liara-api.md.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import time
from pathlib import Path
from typing import Any

import httpx

# Mirrors the CLI's default ignores plus Python/Node build artefacts.
ARCHIVE_IGNORES = {
    ".git", ".idea", ".vscode", ".next", "node_modules", "bower_components",
    ".DS_Store", "liara.json", ".venv", "venv", "__pycache__", ".env", ".cache",
}
MAX_SOURCE_SIZE = 256 * 1024 * 1024

FINAL_RELEASE_STATES = {"READY", "FAILED", "UNHEALTHY", "CANCELED"}
# app names / ids come from the LLM and go into URL paths: no "..", "?", "#" or "/"
ID_RE = re.compile(r"[A-Za-z0-9_-]+")


class LiaraError(Exception):
    """An API call failed; `status` is the HTTP code (0 for network errors)."""

    def __init__(self, message: str, status: int = 0, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _id(value: str) -> str:
    if not ID_RE.fullmatch(str(value)):
        raise LiaraError(f"invalid app name or id {value!r}", status=400)
    return value


class LiaraClient:
    def __init__(self, token: str, base_url: str = "https://api.liara.ir",
                 team_id: str | None = None):
        if not token:
            raise LiaraError("LIARA_API_KEY is not set")
        self.team_id = team_id
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            # like the CLI: every call targets the team when one is configured
            params={"teamID": team_id} if team_id else None,
            timeout=httpx.Timeout(30.0, read=60.0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = await self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise LiaraError(f"network error calling {method} {path}: {e}") from e
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = r.text[:500]
            msg = body.get("message") if isinstance(body, dict) else None
            raise LiaraError(
                f"{method} {path} -> HTTP {r.status_code}: {msg or body}",
                status=r.status_code,
                body=body,
            )
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}

    # ---- account -------------------------------------------------------
    async def me(self) -> dict:
        return await self._request("GET", "/v1/me")

    async def billing(self) -> dict:
        """`balance` and `minCreditAmount` (toman); creating apps needs balance >= minimum.
        `/v1/billing` always reports the personal account, so in team mode use `me.team`."""
        if self.team_id:
            return (await self.me())["team"]
        return (await self._request("GET", "/v1/billing"))["user"]

    async def project_plans(self) -> list[dict]:
        """Available app plans; `price` is hourly, `displayPrice` monthly (toman)."""
        plans = (await self.me())["plans"]["projects"]
        return [{"id": k, **v} for k, v in plans.items() if v.get("available")]

    async def bundle_plans(self, plan_id: str) -> dict[str, dict]:
        """Feature bundles for a plan. g2 plans have their own (free/basic/standard/pro)
        and the bundle's `price` is what Liara bills; older plans share `g1.default`."""
        bundles = (await self.me())["plans"]["projectBundlePlans"]
        return {k: v for k, v in bundles.get(plan_id, bundles.get("g1", {})).items()
                if v.get("available")}

    # ---- apps ----------------------------------------------------------
    async def list_apps(self) -> list[dict]:
        return (await self._request("GET", "/v1/projects"))["projects"]

    async def get_app(self, name: str) -> dict:
        return (await self._request("GET", f"/v1/projects/{_id(name)}"))["project"]

    async def create_app(self, name: str, platform: str, plan_id: str,
                         bundle_plan_id: str, network_id: str | None = None) -> dict:
        body = {"name": name, "platform": platform, "planID": plan_id,
                "bundlePlanID": bundle_plan_id}
        if network_id:  # g2 plans reject creation without one (`g2_plan_unavailable`)
            body["network"] = network_id
        return await self._request("POST", "/v1/projects", json=body)

    async def networks(self) -> list[dict]:
        return (await self._request("GET", "/v1/networks"))["networks"]

    async def create_network(self, name: str) -> dict:
        return await self._request("POST", "/v1/networks", json={"networkName": name})

    async def domains(self, project: str) -> list[dict]:
        return (await self._request("GET", "/v1/domains", params={"project": _id(project)}))["domains"]

    async def add_domain(self, project: str, domain: str) -> dict:
        return await self._request("POST", "/v1/domains",
                                   json={"name": domain, "project": _id(project), "type": "PROJECT"})

    async def check_domain(self, domain_id: str) -> dict:
        return (await self._request("GET", f"/v1/domains/{_id(domain_id)}/check"))["domain"]

    async def delete_domain(self, domain_id: str) -> Any:
        return await self._request("DELETE", f"/v1/domains/{_id(domain_id)}")

    async def delete_app(self, name: str) -> dict:
        return await self._request("DELETE", f"/v1/projects/{_id(name)}")

    async def scale(self, name: str, on: bool) -> dict:
        return await self._request("POST", f"/v1/projects/{_id(name)}/actions/scale",
                                   json={"scale": 1 if on else 0})

    async def restart(self, name: str) -> dict:
        return await self._request("POST", f"/v1/projects/{_id(name)}/actions/restart")

    async def resize(self, name: str, plan_id: str) -> dict:
        return await self._request("POST", f"/v1/projects/{_id(name)}/resize",
                                   json={"planID": plan_id})

    async def set_envs(self, name: str, variables: dict[str, str]) -> dict:
        """The endpoint replaces the whole list, so merge with current envs first."""
        current = {e["key"]: e["value"] for e in (await self.get_app(name)).get("envs", [])}
        current.update(variables)
        return await self._unset_or_set(name, current)

    async def unset_envs(self, name: str, keys: list[str]) -> dict:
        current = {e["key"]: e["value"] for e in (await self.get_app(name)).get("envs", [])}
        for k in keys:
            current.pop(k, None)
        return await self._unset_or_set(name, current)

    async def _unset_or_set(self, name: str, envs: dict[str, str]) -> dict:
        return await self._request("POST", "/v1/projects/update-envs", json={
            "project": name,
            "variables": [{"key": k, "value": v} for k, v in envs.items()],
        })

    # ---- observability -------------------------------------------------
    async def logs(self, name: str, since: int | None = None,
                   release_id: str | None = None, last_lines: bool = False) -> list[tuple[int, str]]:
        """Runtime logs as (unix_ts, line), oldest first."""
        params: dict[str, Any] = {
            "start": since or int(time.time()) - 3600,
            "direction": "forward",
            "last_lines": str(last_lines).lower(),
        }
        if release_id:
            params["releaseId"] = release_id
        data = await self._request("GET", f"/v2/projects/{_id(name)}/logs", params=params)
        lines: list[tuple[int, str]] = []
        for entry in data.get("data") or []:
            for value in entry.get("values", []):
                ts, line = value[0], value[1]
                try:  # each line is JSON: {"type": "stdout"|"stderr", "_entry": "..."}
                    line = json.loads(line).get("_entry", line)
                except (ValueError, AttributeError):
                    pass
                lines.append((int(str(ts)[:10]), line))
        lines.sort(key=lambda x: x[0])
        return lines

    async def events(self, name: str, since: int | None = None) -> Any:
        now = int(time.time())
        return await self._request("GET", f"/v1/projects/{_id(name)}/events",
                                   params={"start": since or now - 86400, "end": now})

    async def metrics_summary(self, name: str) -> dict:
        return await self._request("GET", f"/v1/projects/{_id(name)}/metrics/summary")

    async def metric_series(self, name: str, metric: str, since: int | None = None) -> Any:
        return await self._request(
            "GET", f"/v1/projects/{_id(name)}/metrics/{metric}",
            params={"since": since or int(time.time()) - 3600},
        )

    # ---- releases / deploy ---------------------------------------------
    async def releases(self, name: str, count: int = 10) -> dict:
        return await self._request("GET", f"/v1/projects/{_id(name)}/releases",
                                   params={"page": 1, "count": count})

    async def release(self, release_id: str) -> dict:
        return (await self._request("GET", f"/v1/releases/{_id(release_id)}"))["release"]

    async def build_logs(self, release_id: str, since: int = 0) -> list[str]:
        data = await self._request("GET", f"/v2/releases/{_id(release_id)}/build-logs",
                                   params={"since": since})
        return [o.get("line", "") for o in data.get("buildOutput", [])]

    async def upload_source(self, name: str, archive: bytes) -> str:
        if len(archive) > MAX_SOURCE_SIZE:
            raise LiaraError("source archive exceeds Liara's 256 MiB limit")
        data = await self._request(
            "POST", f"/v2/projects/{_id(name)}/sources",
            files={"file": ("source.tar.gz", archive, "application/gzip")},
            timeout=httpx.Timeout(30.0, write=300.0, read=300.0),
        )
        return data["sourceID"]

    async def create_release(self, name: str, source_id: str, platform: str,
                             port: int, message: str | None = None,
                             platform_config: dict | None = None) -> str:
        body: dict[str, Any] = {"sourceID": source_id, "type": platform, "port": port,
                                "platformConfig": platform_config or {}}
        if message:
            body["message"] = message
        data = await self._request("POST", f"/v2/projects/{_id(name)}/releases", json=body)
        return data["releaseID"]


def build_archive(path: str | Path) -> bytes:
    """tar.gz the project directory the way `liara deploy` does."""
    root = Path(path).resolve()
    if not root.is_dir():
        raise LiaraError(f"project directory not found: {root}")

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts
        if any(p in ARCHIVE_IGNORES for p in parts) or info.name.endswith((".pyc", ".log")):
            return None
        return info

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for child in sorted(root.iterdir()):
            tar.add(child, arcname=child.name, filter=keep)
    return buf.getvalue()
