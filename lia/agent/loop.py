"""Tool-calling agent over any OpenAI-compatible provider (swappable via LLM_* env vars).

`run()` is an async generator of UI events. When the model calls a tool that
is not READ, the loop stops with a `confirm` event; `resume()` continues after
the user approves or declines.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from ..liara.client import LiaraError
from ..liara.tools import LiaraTools, Risk

MAX_STEPS = 12
MAX_RESULT_CHARS = 6000

SYSTEM_PROMPT = """You are «لیایار» (LiaYar), an assistant that deploys, operates, monitors and troubleshoots websites on the Liara cloud platform for non-technical users.

Language & tone:
- Always answer in simple, friendly Persian. Avoid jargon; when you must use a technical term, explain it in a few words.
- Keep answers short. Use short bullet lists. Put app names, plans and values in `backticks`.
- Never show raw API codes or English status words to the user; translate them: ACTIVE/running → «روشن و در حال اجرا», OFF/stopped → «خاموش», DEPLOYING/rollout → «در حال استقرار», READY/SUCCEEDED → «موفق», FAILED → «ناموفق», CANCELED → «لغوشده», oom → «کمبود حافظه (رم)». Say HTTP 200 as «سایت پاسخ می‌دهد» and 5xx as «سایت خطا می‌دهد» (the code may follow in parentheses).

Operating rules:
- Use tools to get real data. Never invent app names, statuses, logs, prices or results.
- Read-only tools run immediately. Tools marked [sensitive] or [costly] are shown to the user for confirmation automatically — just call them when they are the right next step; do NOT ask "should I?" in text first. Before calling a [costly] tool, briefly tell the user the price (use list_plans).
- If the user didn't say which app and there is more than one, call list_apps and ask.
- If a request needs something Liara's API cannot do, call api_limitations and say clearly that it is not possible via the API, then offer the closest real alternative.

Domains: list_domains shows connected domains; add_domain connects a domain the user ALREADY owns. Lia cannot buy domains or edit DNS records - explain the dns_steps in plain Persian (which records, that exact names/values are on the domain page in the Liara console because the API does not return them - say this, for .ir that the nameservers are changed at nic.ir, that it can take hours) and offer to check again later with list_domains.

Deploying a project:
0. If the user asks how to put a site on Liara, briefly compare the ways: (a) Lia uploads the project folder and deploys it for them - simplest, recommended; (b) the official `liara` CLI (`liara deploy`) for developers; (c) the Liara console. Then continue with (a) unless they prefer another.
1. detect_project → tell the user the detected platform, port and suggested plan with its price.
2. If the app does not exist, create_app (costly). Then deploy. Report the URL and whether the site responds.

Troubleshooting ("my site is down/slow/broken"):
1. Call diagnose_app first (its error_logs cover only the running release; error_logs.exceptions is the strongest evidence). Use get_logs / get_events for more detail if needed. Ignore deprecation warnings (UserWarning, DeprecationWarning) - they are not errors.
2. Always (even if you call a fix tool in the same turn) give the user a short diagnosis: the symptoms you saw, then the likely root causes RANKED by likelihood, each with a confidence (زیاد / متوسط / کم) and the evidence (quote the key log line or metric).
3. Propose the best fix (and one alternative), explaining the effect and risk in plain words, then call the tool for it.
4. After any fix, ALWAYS verify: call check_site (and get_logs / get_metrics when relevant) and tell the user clearly whether the problem is solved. For RAM/CPU/oom problems a 200 is not enough: call get_metrics and report the new container's memory (the LAST value) against the plan's RAM. If not, continue with the next most likely cause.

Recent change first: diagnose_app.current_release is the last change to the app (type DEPLOY = new code, ENV_UPDATE = env vars changed; env_change shows which). If the trouble (errors, oom events of that release tag) started with it, that change is the most likely root cause - say so with the evidence, and prefer undoing it over anything else:
  - DEPLOY → rollback to the previous good release.
  - ENV_UPDATE → unset_env / set_env back to the previous value. NOT rollback: env vars are not part of a release, so a rollback keeps the bad value. If env_change is unknown (changed outside Lia), look at status.envs for the variable that best explains the symptom, name it with your reasoning, and propose removing/restoring it.
Free first: rank fixes that cost nothing (undo a change, fix config, restart) above paid ones (resize_app); offer a bigger plan as the alternative, with its price.

Improvement advice: when asked how to make the site faster or more stable (or after a fix, in one line), give 2-3 concrete suggestions backed by the data you read - e.g. RAM/CPU headroom vs the plan, check_site latency (ms), restarts/oom events, errors in logs - and say which ones Lia can do (with confirmation) and which the user must do in their code.

Signals: slow site → check_site latency (ms) plus CPU/RAM near the plan limit, oom/restart events and error logs; name the most likely of: RAM, CPU limit, application errors, or something else. HTTP 5xx in the probe + exceptions in logs → code/config error (missing env var, bad release). Memory near the plan limit, OOM/killed events, restarts → not enough RAM (suggest resize_app to a bigger plan, or fixing a memory leak). A release in FAILED state → build error (read build_log_tail). A missing env var can be fixed with set_env if the user knows the value; a broken new release can be fixed with rollback to the last good release in rollback_candidates.
"""


@dataclass
class Pending:
    id: str
    tool_call: Any  # the model's tool call awaiting confirmation
    remaining: list  # tool calls of the same turn queued after it


@dataclass
class Session:
    messages: list[dict] = field(default_factory=lambda: [{"role": "system", "content": SYSTEM_PROMPT}])
    pending: Pending | None = None


class Agent:
    def __init__(self, tools: LiaraTools, base_url: str | None, api_key: str | None, model: str):
        self.tools = tools
        self.model = model
        self.llm = AsyncOpenAI(base_url=base_url, api_key=api_key or "missing") if api_key else None
        self.sessions: dict[str, Session] = {}

    def session(self, sid: str) -> Session:
        return self.sessions.setdefault(sid, Session())

    async def run(self, sid: str, user_text: str) -> AsyncIterator[dict]:
        s = self.session(sid)
        if s.pending:
            # A new message implicitly declines the pending action.
            async for ev in self._finish_pending(s, approved=False, note="user sent a new message instead"):
                yield ev
        s.messages.append({"role": "user", "content": user_text})
        async for ev in self._loop(s):
            yield ev

    async def resume(self, sid: str, action_id: str, approved: bool) -> AsyncIterator[dict]:
        s = self.session(sid)
        if not s.pending or s.pending.id != action_id:
            yield {"type": "error", "text": "این درخواست تأیید دیگر معتبر نیست."}
            return
        async for ev in self._finish_pending(s, approved):
            yield ev
        if not s.pending:
            async for ev in self._loop(s):
                yield ev

    async def _finish_pending(self, s: Session, approved: bool, note: str = "") -> AsyncIterator[dict]:
        p, s.pending = s.pending, None
        if approved:
            async for ev in self._execute(s, p.tool_call):
                yield ev
        else:
            s.messages.append(self._tool_msg(p.tool_call, {
                "declined_by_user": True, "executed": False, "note": note,
                "next": "Nothing was done and nothing was charged. Do NOT call any other sensitive or "
                        "costly tool now (steps that depended on this one cannot work). Tell the user "
                        "briefly that it was not done and ask what they want instead."}))
            yield {"type": "declined", "id": p.id, "tool": p.tool_call.function.name}
            # the rest of that turn may depend on the declined step (or be another confirm card)
            for call in p.remaining:
                s.messages.append(self._tool_msg(call, {"skipped": True, "executed": False,
                                                        "reason": "an earlier action was declined"}))
            return
        async for ev in self._run_calls(s, p.remaining):
            yield ev

    async def _loop(self, s: Session) -> AsyncIterator[dict]:
        if not self.llm:
            yield {"type": "error", "text": "کلید مدل زبانی تنظیم نشده است (LLM_API_KEY در فایل .env)."}
            return
        self._answer_orphans(s)
        for _ in range(MAX_STEPS):
            yield {"type": "thinking"}
            try:
                resp = await self.llm.chat.completions.create(
                    model=self.model, messages=s.messages,
                    tools=[t.schema() for t in self.tools.tools.values()], temperature=0.2)
            except Exception as e:  # provider errors must reach the user
                yield {"type": "error", "text": f"خطا در ارتباط با مدل زبانی: {e}"}
                return
            msg = resp.choices[0].message
            s.messages.append({**msg.model_dump(exclude_none=True), "content": msg.content or ""})
            if msg.content:
                yield {"type": "message", "text": msg.content}
            if not msg.tool_calls:
                return
            async for ev in self._run_calls(s, list(msg.tool_calls)):
                yield ev
            if s.pending:
                return
        yield {"type": "error", "text": "تعداد مراحل از حد مجاز گذشت؛ لطفاً درخواست را ساده‌تر بیان کنید."}

    async def _run_calls(self, s: Session, calls: list) -> AsyncIterator[dict]:
        for i, call in enumerate(calls):
            tool = self.tools.tools.get(call.function.name)
            if tool and tool.risk != Risk.READ:
                args = self._args(call)
                try:
                    summary = await tool.summarize(**args)
                except Exception as e:
                    summary = f"{tool.label}: {args} ({e})"
                s.pending = Pending(uuid.uuid4().hex[:8], call, calls[i + 1:])
                yield {"type": "confirm", "id": s.pending.id, "tool": tool.name, "label": tool.label,
                       "risk": tool.risk.value, "summary": summary, "args": args}
                return
            async for ev in self._execute(s, call):
                yield ev

    async def _execute(self, s: Session, call: Any) -> AsyncIterator[dict]:
        tool = self.tools.tools.get(call.function.name)
        args = self._args(call)
        if not tool:
            s.messages.append(self._tool_msg(call, {"error": f"unknown tool {call.function.name}"}))
            return
        yield {"type": "tool_start", "id": call.id, "tool": tool.name, "label": tool.label,
               "risk": tool.risk.value, "args": args}
        try:
            result = await tool.handler(**args)
            ok = True
        except LiaraError as e:
            result, ok = {"error": str(e), "http_status": e.status}, False
        except TypeError as e:
            result, ok = {"error": f"bad arguments: {e}"}, False
        except Exception as e:  # an unanswered tool call would break every later LLM request
            result, ok = {"error": f"{type(e).__name__}: {e}"}, False
        s.messages.append(self._tool_msg(call, result))
        yield {"type": "tool_result", "id": call.id, "tool": tool.name, "label": tool.label,
               "ok": ok, "result": result}

    @staticmethod
    def _answer_orphans(s: Session) -> None:
        """A stream cut mid-turn (tab closed) leaves tool calls without results; the provider
        rejects such a history, so answer them as interrupted before the next request."""
        answered = {m.get("tool_call_id") for m in s.messages if m.get("role") == "tool"}
        for i in range(len(s.messages) - 1, -1, -1):
            m = s.messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                missing = [c for c in m["tool_calls"] if c["id"] not in answered]
                if missing:
                    pos = i + 1
                    while pos < len(s.messages) and s.messages[pos].get("role") == "tool":
                        pos += 1
                    s.messages[pos:pos] = [{"role": "tool", "tool_call_id": c["id"], "content": json.dumps(
                        {"executed": False, "error": "interrupted before it ran"})} for c in missing]
                break

    @staticmethod
    def _args(call: Any) -> dict:
        try:
            return json.loads(call.function.arguments or "{}")
        except ValueError:
            return {}

    @staticmethod
    def _tool_msg(call: Any, result: Any) -> dict:
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "…(truncated)"
        return {"role": "tool", "tool_call_id": call.id, "content": text}
