"""Karaoke Eternal SongProvider v0 wire contract."""

from __future__ import annotations

import asyncio
import json
import math
import re

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from .service import ProviderService


def _error(status: int, message: str) -> PlainTextResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return PlainTextResponse(message, status_code=status, headers=headers)


async def _body(request: Request) -> dict | None:
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        return None
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def create_api(service: ProviderService) -> FastAPI:
    app = FastAPI(title="YouTube SongProvider", docs_url=None, redoc_url=None, openapi_url=None)

    def auth(slug: str, request: Request):
        profile = service.store.profile(slug)
        if not profile:
            return None, None, _error(404, "Unknown profile")
        header = request.headers.get("authorization", "")
        match = re.fullmatch(r"Bearer ([^\s]+)", header, re.IGNORECASE)
        if not match:
            return None, None, _error(401, "Invalid credentials")
        key_id = service.store.authenticate_key(slug, match[1])
        if not key_id:
            return None, None, _error(401, "Invalid credentials")
        return profile, key_id, None

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/icons/youtube.svg")
    async def icon():
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect x="3" y="10" width="58" height="44" rx="12" fill="#e21b23"/><path d="M26 21v22l18-11z" fill="white"/></svg>'
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/profiles/{slug}/api")
    @app.get("/profiles/{slug}/api/")
    async def discovery(slug: str):
        profile = service.store.profile(slug)
        if not profile:
            return _error(404, "Unknown profile")
        return {"name": profile.name, "auth": {"versions": [0]}, "types": {"song": {"versions": [0]}}}

    @app.get("/profiles/{slug}/api/auth/v0")
    async def auth_check(slug: str, request: Request):
        _, _, error = auth(slug, request)
        return error or Response(status_code=204)

    @app.post("/profiles/{slug}/api/song/v0/search")
    async def search(slug: str, request: Request):
        profile, key_id, error = auth(slug, request)
        if error:
            return error
        body = await _body(request)
        query = body.get("query") if body else None
        if not isinstance(query, str) or not query.strip():
            return _error(400, "query must be a nonempty string")
        if len(query.encode()) > 512:
            return _error(400, "query is too long")
        if not service.allowed(slug, key_id, "search", 10):
            return _error(429, "Search rate limit exceeded")
        icon_url = service.icon_url(str(request.base_url))
        return StreamingResponse(service.search(profile, query, icon_url), media_type="application/x-ndjson",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    @app.post("/profiles/{slug}/api/song/v0/items/{item_id}/fetch")
    async def fetch(slug: str, item_id: str, request: Request):
        profile, key_id, error = auth(slug, request)
        if error:
            return error
        body = await _body(request)
        stamp = body.get("timestamp") if body else None
        if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                or not math.isfinite(stamp) or stamp < 0):
            return _error(400, "timestamp must be a nonnegative finite number")
        meta = body.get("meta") if body else None
        if body and "meta" in body:
            if not isinstance(meta, dict) or len(json.dumps(meta, ensure_ascii=False).encode()) > 4096:
                return _error(400, "meta must be an object of at most 4 KiB")
        item = service.store.item(slug, item_id)
        if not item:
            return _error(404, "Unknown item")
        if not service.allowed(slug, key_id, "fetch", 20):
            return _error(429, "Fetch rate limit exceeded")
        service.fetch(profile, key_id, item, stamp, meta)
        return Response(status_code=202)

    @app.post("/profiles/{slug}/api/song/v0/items/{item_id}/cancel")
    async def cancel(slug: str, item_id: str, request: Request):
        _, key_id, error = auth(slug, request)
        if error:
            return error
        if not service.cancel(slug, key_id, item_id):
            return _error(404, "Unknown download")
        return Response(status_code=202)

    @app.get("/profiles/{slug}/api/song/v0/status")
    async def status(slug: str, request: Request):
        _, key_id, error = auth(slug, request)
        if error:
            return error
        try:
            since = float(request.query_params["since"])
            if not math.isfinite(since):
                since = None
        except (KeyError, ValueError, OverflowError):
            since = None
        if len(service.listeners.get((slug, key_id), ())) >= 4:
            return _error(429, "Too many status streams")
        queue = service.subscribe(slug, key_id)

        async def events():
            try:
                yield b": connected\n\n"
                for state in service.store.downloads(slug, key_id, since):
                    yield ("data: " + json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()
                while True:
                    if not service.store.key_active(slug, key_id):
                        break
                    try:
                        state = await asyncio.wait_for(queue.get(), timeout=20)
                    except TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    if since is None or state["timestamp"] >= since:
                        yield ("data: " + json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()
            finally:
                service.unsubscribe(slug, key_id, queue)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    return app
