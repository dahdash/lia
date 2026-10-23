"""Background health watch: the automated repetitive op behind the sidebar.

Every MONITOR_INTERVAL seconds, READ-only: list apps, probe each site, read metrics
and scan the current release's recent logs for errors. Liara's API has no alert or
health-check endpoints, so Lia derives the alerts itself. An alert fires when a
condition starts (down / errors / high RAM / high CPU) and an "ok" alert when it clears.
"""

from __future__ import annotations

import asyncio
import time

from ..liara.client import LiaraError
from ..liara.tools import LiaraTools

MONITOR_INTERVAL = 60
RAM_WARN = 0.85   # share of the plan's RAM used by the busiest container
CPU_WARN = 0.90   # share of the plan's CPU
MAX_ALERTS = 30


def fa_num(n: float) -> str:
    return f"{n:,.0f}".translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


class Health:
    def __init__(self, tools: LiaraTools, interval: int = MONITOR_INTERVAL):
        self.tools = tools
        self.interval = interval
        self.apps: dict[str, dict] = {}
        self.alerts: list[dict] = []
        self.active: dict[str, dict[str, bool]] = {}  # app -> condition -> active
        self._next_id = 1

    def _alert(self, app: str | None, kind: str, level: str, text: str, ask: str | None = None) -> None:
        self.alerts.append({"id": self._next_id, "at": time.time(), "app": app, "kind": kind,
                            "level": level, "text": text, "ask": ask})
        self._next_id += 1
        self.alerts = self.alerts[-MAX_ALERTS:]

    def _track(self, app: str, kind: str, on: bool, text_on: str, text_off: str,
               level: str, ask: str) -> None:
        was = self.active.setdefault(app, {}).get(kind, False)
        if on and not was:
            self._alert(app, kind, level, text_on, ask)
        elif was and not on:
            self._alert(app, kind, "ok", text_off)
        self.active[app][kind] = on

    async def _plan_limits(self) -> dict[str, dict]:
        try:
            plans = (await self.tools.c.me())["plans"]["projects"]
        except (LiaraError, KeyError, TypeError):
            return {}
        return {k: {"ram_mb": p["RAM"]["amount"] * 1024, "cpu": p["CPU"]["amount"]}
                for k, p in plans.items() if isinstance(p, dict) and "RAM" in p and "CPU" in p}

    async def _error_scan(self, name: str) -> dict | None:
        minutes = max(2, self.interval // 60 + 1)
        try:
            logs = await self.tools.get_logs(name, minutes=minutes, errors_only=True, limit=3,
                                             current_release_only=True)
        except LiaraError:
            return None
        return {"count": logs["total_lines"], "top": (logs["exceptions"][:1] or [{}])[0].get("line"),
                "last": logs["lines"][-1] if logs["lines"] else None, "minutes": minutes}

    async def check_once(self) -> None:
        try:
            apps = await self.tools.list_apps()
        except LiaraError as e:
            self._alert(None, "api", "bad", f"خطای API لیارا هنگام پایش: {e}")
            return
        limits = await self._plan_limits()
        seen = set()
        for a in apps:
            name = a["name"]
            seen.add(name)
            running = a.get("status") != "OFF" and bool(a.get("scale"))
            probe = await self.tools.check_site(name) if running else None
            metrics = None
            try:
                metrics = await self.tools.get_metrics(name)
            except LiaraError:
                pass
            errors = await self._error_scan(name) if running else None

            lim = limits.get(a.get("plan") or "")
            # containers come oldest first; in a rollout only the newest `scale` ones stay
            n = max(1, a.get("scale") or 1)
            mem = max(((metrics or {}).get("memory_mb_per_container") or [0])[-n:])
            cpu = max(((metrics or {}).get("cpu_per_container") or [0])[-n:])
            ram_pct = round(100 * mem / lim["ram_mb"]) if lim and lim["ram_mb"] else None
            cpu_pct = round(100 * cpu / lim["cpu"]) if lim and lim["cpu"] else None
            healthy = bool(probe and probe.get("ok"))

            why = (probe.get("status") or probe.get("error")) if probe else "خاموش"
            self._track(name, "down", running and not healthy,
                        f"سایت `{name}` از دسترس خارج شد ({why}).",
                        f"سایت `{name}` دوباره در دسترس است.",
                        "bad", f"سایت {name} از دسترس خارج شده، علتش را پیدا کن")
            n_err = errors["count"] if errors else 0
            if errors is not None or not running:  # a failed log read neither raises nor clears it
                self._track(name, "errors", n_err > 0,
                            f"{fa_num(n_err)} خطا در لاگ‌های `{name}` در {fa_num(errors['minutes'] if errors else 0)} دقیقه‌ی اخیر"
                            + (f": `{errors['top'] or errors['last']}`" if errors and (errors['top'] or errors['last']) else "."),
                            f"خطای تازه‌ای در لاگ‌های `{name}` دیده نمی‌شود.",
                            "warn", f"در لاگ‌های {name} خطا دیده می‌شود، تحلیل کن و راه‌حل بده")
            self._track(name, "ram", ram_pct is not None and ram_pct >= RAM_WARN * 100,
                        f"مصرف رم `{name}` به {fa_num(ram_pct or 0)}٪ ظرفیت پلن رسیده است.",
                        f"مصرف رم `{name}` به حالت عادی برگشت ({fa_num(ram_pct or 0)}٪).",
                        "warn", f"سایت {name} کند شده و رمش بالاست، بررسی کن")
            self._track(name, "cpu", cpu_pct is not None and cpu_pct >= CPU_WARN * 100,
                        f"مصرف پردازنده‌ی `{name}` به {fa_num(cpu_pct or 0)}٪ ظرفیت پلن رسیده است.",
                        f"مصرف پردازنده‌ی `{name}` به حالت عادی برگشت.",
                        "warn", f"پردازنده‌ی {name} زیر فشار است، بررسی کن")

            self.apps[name] = {**a, "probe": probe, "metrics": metrics, "healthy": healthy,
                               "ram_pct": ram_pct, "cpu_pct": cpu_pct, "errors": n_err,
                               "issues": [k for k, v in self.active.get(name, {}).items() if v],
                               "checked_at": time.time()}
        for gone in set(self.apps) - seen:
            self.apps.pop(gone)
            self.active.pop(gone, None)

    async def run(self) -> None:
        while True:
            try:
                await self.check_once()
            except Exception as e:  # never let the monitor die
                self._alert(None, "monitor", "bad", f"خطای پایش: {e}")
            await asyncio.sleep(self.interval)
