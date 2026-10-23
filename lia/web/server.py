"""FastAPI server: Persian chat UI, NDJSON event stream, background health watch (health.py)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..agent.loop import Agent
from ..config import get_settings
from ..liara.tools import build_tools
from .health import Health

STATIC = Path(__file__).parent / "static"


settings = get_settings()
state: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    tools = build_tools(settings)
    state["tools"] = tools
    state["agent"] = Agent(tools, settings.llm_base_url, settings.llm_api_key, settings.llm_model)
    state["health"] = Health(tools)
    task = asyncio.create_task(state["health"].run())
    yield
    task.cancel()
    await tools.c.aclose()


app = FastAPI(title="Lia", lifespan=lifespan)
# local-only UI: refuse other Host headers so a web page can't reach it via DNS rebinding
# and approve sensitive actions on the user's behalf
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


class ChatIn(BaseModel):
    session: str
    message: str


class ConfirmIn(BaseModel):
    session: str
    id: str
    approve: bool


def ndjson(events: AsyncIterator[dict]) -> StreamingResponse:
    async def gen():
        try:
            async for ev in events:
                yield json.dumps(ev, ensure_ascii=False, default=str) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "text": f"خطای داخلی: {e}"}, ensure_ascii=False) + "\n"
        yield json.dumps({"type": "done"}) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.post("/api/chat")
async def chat(body: ChatIn):
    return ndjson(state["agent"].run(body.session, body.message))


@app.post("/api/confirm")
async def confirm(body: ConfirmIn):
    return ndjson(state["agent"].resume(body.session, body.id, body.approve))


@app.get("/api/status")
async def status():
    h: Health = state["health"]
    return {"apps": list(h.apps.values()), "alerts": h.alerts[-8:], "interval": h.interval, "model": settings.llm_model,
            "llm_ready": bool(settings.llm_api_key)}


@app.post("/api/status/refresh")
async def refresh():
    await state["health"].check_once()
    return await status()


def main() -> None:
    import uvicorn
    uvicorn.run("lia.web.server:app", host="127.0.0.1", port=8000)
