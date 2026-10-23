"""Thin MCP server over the same Liara tool layer as the web UI.

READ tools run directly. SENSITIVE/COSTLY tools are two-step: the first call returns a Persian
summary and a one-time `confirm_token`; the action runs only when the same tool is called again
with the same arguments plus that token, after the user has approved it.

Run over stdio: `lia-mcp` (needs the `[mcp]` extra and the same `.env` as the web UI).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

import mcp_types as types
from mcp.server import Server

from .config import get_settings
from .liara.client import LiaraError
from .liara.tools import LiaraTools, Risk, Tool, build_tools

TOKEN_TTL = 300  # seconds a confirmation stays valid

INSTRUCTIONS = (
    "Lia manages websites on Liara (liara.ir). Read-only tools run directly. Tools marked "
    "[sensitive] or [costly] first return a summary and a confirm_token: show the summary to the "
    "user, and only after they explicitly approve, call the same tool again with identical "
    "arguments plus confirm_token. Never approve on the user's behalf. When something is not "
    "possible through Liara's API, call api_limitations and tell the user."
)


class ConfirmGate:
    """One-time tokens bound to (tool, arguments); a token cannot approve a different action."""

    def __init__(self, ttl: int = TOKEN_TTL):
        self.ttl = ttl
        self._pending: dict[str, tuple[str, float]] = {}

    @staticmethod
    def _key(tool: str, args: dict) -> str:
        return hashlib.sha256(json.dumps([tool, args], sort_keys=True, default=str).encode()).hexdigest()

    def issue(self, tool: str, args: dict) -> str:
        now = time.monotonic()
        self._pending = {t: v for t, v in self._pending.items() if v[1] > now}
        token = secrets.token_urlsafe(9)
        self._pending[token] = (self._key(tool, args), now + self.ttl)
        return token

    def redeem(self, token: str, tool: str, args: dict) -> bool:
        entry = self._pending.pop(token, None)
        return bool(entry and entry[1] > time.monotonic() and entry[0] == self._key(tool, args))


def _schema(tool: Tool) -> dict:
    props = dict(tool.params)
    if tool.risk != Risk.READ:
        props["confirm_token"] = {
            "type": "string",
            "description": "Token from the first call; pass it only after the user approved the summary."}
    return {"type": "object", "properties": props, "required": tool.required}


def _mcp_tool(tool: Tool) -> types.Tool:
    note = "" if tool.risk == Risk.READ else (
        " Requires user confirmation: the first call returns a summary and confirm_token.")
    return types.Tool(
        name=tool.name, title=tool.label,
        description=f"[{tool.risk.value}] {tool.description}{note}",
        input_schema=_schema(tool),
        annotations=types.ToolAnnotations(
            title=tool.label, read_only_hint=tool.risk == Risk.READ,
            destructive_hint=tool.name in ("delete_app", "stop_app", "unset_env"),
            open_world_hint=True))


def _result(data: Any, error: bool = False) -> types.CallToolResult:
    text = json.dumps(data, ensure_ascii=False, default=str, indent=1)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=error)


async def call(tools: LiaraTools, gate: ConfirmGate, name: str, args: dict) -> types.CallToolResult:
    """Risk-tier enforcement for MCP; same tiers and summaries as the web agent."""
    tool = tools.tools.get(name)
    if not tool:
        return _result({"error": f"unknown tool `{name}`"}, error=True)
    args = dict(args)
    token = args.pop("confirm_token", None)
    if tool.risk != Risk.READ:
        if not token:
            try:
                summary = await tool.summarize(**args)
            except Exception as e:
                return _result({"error": f"{type(e).__name__}: {e}"}, error=True)
            return _result({
                "status": "confirmation_required", "tool": name, "risk": tool.risk.value,
                "summary": summary, "confirm_token": gate.issue(name, args),
                "expires_in_s": gate.ttl,
                "next": "Show the summary to the user. Only if they approve, call this tool again "
                        "with the same arguments plus confirm_token."})
        if not gate.redeem(token, name, args):
            return _result({"error": "invalid, expired or already used confirm_token, or the "
                                     "arguments changed; call again without a token for a new "
                                     "confirmation"}, error=True)
    try:
        return _result({"status": "done", "tool": name, "result": await tool.handler(**args)})
    except LiaraError as e:
        return _result({"status": "failed", "tool": name, "error": str(e),
                        "http_status": e.status or None}, error=True)
    except Exception as e:  # bad/missing arguments or an unexpected API shape
        return _result({"status": "failed", "tool": name, "error": f"{type(e).__name__}: {e}"}, error=True)


def build_server(tools: LiaraTools, gate: ConfirmGate | None = None) -> Server:
    gate = gate or ConfirmGate()

    async def list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[_mcp_tool(t) for t in tools.tools.values()])

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await call(tools, gate, params.name, params.arguments or {})

    return Server("lia", version="0.1.0", title="Lia — Liara assistant", instructions=INSTRUCTIONS,
                  on_list_tools=list_tools, on_call_tool=call_tool)


def main() -> None:
    import anyio
    from mcp.server.stdio import stdio_server

    settings = get_settings()

    async def run() -> None:
        tools = build_tools(settings)
        server = build_server(tools)
        try:
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())
        finally:
            await tools.c.aclose()

    anyio.run(run)


if __name__ == "__main__":
    main()
