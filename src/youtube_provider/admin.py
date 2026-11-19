"""Local administration app. It has a separate listener from SongProvider."""

from __future__ import annotations

import collections
import os
import time
from html import escape
from importlib.resources import files
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError

from .model import Profile, parse_metadata
from .service import ProviderService
from .updater import Updater


HASHER = PasswordHasher()


def create_admin(service: ProviderService, updater: Updater, *, api_port: int = 3030,
                 cookie_secure: bool = False) -> FastAPI:
    app = FastAPI(title="YouTube Provider Administration", docs_url=None, redoc_url=None, openapi_url=None)
    attempts: dict[str, collections.deque[float]] = {}
    static = files("youtube_provider").joinpath("static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'"
        return response

    def denied(status: int = 401, message: str = "Authentication required") -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    def same_origin(request: Request) -> bool:
        # The browser knows the public origin even when a local forwarding proxy
        # rewrites Host or terminates HTTPS before the request reaches Uvicorn.
        fetch_site = request.headers.get("sec-fetch-site", "").lower()
        if fetch_site == "same-origin":
            return True
        if fetch_site in ("same-site", "cross-site"):
            return False
        origin = request.headers.get("origin")
        return not origin or origin.rstrip("/") == str(request.base_url).rstrip("/")

    def session(request: Request, *, mutate: bool = False):
        token = request.cookies.get("admin_session", "")
        csrf = service.store.session(token) if token else None
        if not csrf:
            return None, denied()
        if mutate and request.headers.get("x-csrf-token") != csrf:
            return None, denied(403, "Invalid CSRF token")
        if mutate and not same_origin(request):
            return None, denied(403, "Cross-origin request blocked")
        return csrf, None

    def signed_in():
        token, _ = service.store.create_session()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("admin_session", token, httponly=True, secure=cookie_secure,
                            samesite="strict", max_age=8 * 3600, path="/")
        return response

    def setup_form(error: str = ""):
        page = static.joinpath("setup.html").read_text(encoding="utf-8")
        if error:
            page = page.replace("<!-- validation -->", f'<p role="alert">{escape(error)}</p>')
        return HTMLResponse(page, status_code=400 if error else 200)

    @app.post("/setup")
    async def setup(request: Request):
        if not same_origin(request):
            return HTMLResponse("Invalid request", status_code=403)
        if service.store.setting("admin_password_hash") is not None:
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        password = form.get("password")
        if not isinstance(password, str) or len(password) < 12:
            return setup_form("Use at least 12 characters.")
        if password != form.get("confirm"):
            return setup_form("Passwords do not match. Enter them again.")
        if not service.store.initialize_admin_password(HASHER.hash(password)):
            return RedirectResponse("/login", status_code=303)
        return signed_in()

    @app.get("/login")
    async def login_page():
        if service.store.setting("admin_password_hash") is None:
            return setup_form()
        return HTMLResponse("""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Provider login</title><link rel="stylesheet" href="/static/admin.css"><main><h1>YouTube Provider</h1><form method="post" action="/login"><label>Admin password <input type="password" name="password" required autofocus></label><button>Sign in</button></form></main></html>""")

    @app.post("/login")
    async def login(request: Request):
        if not same_origin(request):
            return HTMLResponse("Invalid request", status_code=403)
        client = request.client.host if request.client else "unknown"
        recent = attempts.setdefault(client, collections.deque())
        while recent and recent[0] < time.monotonic() - 300:
            recent.popleft()
        if len(recent) >= 5:
            return HTMLResponse("Too many login attempts; try again later", status_code=429)
        form = await request.form()
        password = str(form.get("password", ""))
        password_hash = service.store.setting("admin_password_hash")
        valid = False
        if password_hash:
            try:
                valid = HASHER.verify(password_hash, password)
            except (VerifyMismatchError, ValueError):
                pass
        if not valid:
            recent.append(time.monotonic())
            return HTMLResponse("Invalid password", status_code=401)
        recent.clear()
        return signed_in()

    @app.get("/")
    async def index(request: Request):
        _, error = session(request)
        if error:
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(static.joinpath("admin.html").read_text(encoding="utf-8"))

    @app.get("/static/admin.js")
    async def javascript(request: Request):
        _, error = session(request)
        return error or Response(static.joinpath("admin.js").read_text(encoding="utf-8"), media_type="text/javascript")

    @app.get("/static/admin.css")
    async def stylesheet():
        return Response(static.joinpath("admin.css").read_text(encoding="utf-8"), media_type="text/css")

    @app.get("/admin/api/state")
    async def state(request: Request):
        csrf, error = session(request)
        if error:
            return error
        profiles = service.store.profiles()
        return {"csrf": csrf,
                "api_base_url": service.public_base_url or f"http://127.0.0.1:{api_port}",
                "profiles": [profile.model_dump() for profile in profiles],
                "keys": {profile.slug: service.store.keys(profile.slug) for profile in profiles},
                "update": {"status": updater.status, "message": updater.message,
                           "channel": updater.channel, "interval_hours": updater.interval_hours,
                           "version": service.store.setting("last_update_version")}}

    @app.get("/admin/api/directories")
    async def directories(request: Request):
        _, error = session(request)
        if error:
            return error
        raw_path = request.query_params.get("path", str(service.store.data_dir))
        if len(raw_path) > 4096 or not Path(raw_path).is_absolute():
            return denied(400, "Directory path must be absolute")
        try:
            directory = Path(raw_path).resolve(strict=True)
            if not directory.is_dir():
                return denied(400, "Path is not a directory")
            with os.scandir(directory) as scan:
                children = sorted((entry.name, str(Path(entry.path).resolve()))
                                  for entry in scan if entry.is_dir())
        except FileNotFoundError:
            return denied(404, "Directory does not exist")
        except PermissionError:
            return denied(403, "Directory is not accessible")
        except (OSError, ValueError):
            return denied(400, "Cannot list directory")
        return {"path": str(directory),
                "parent": str(directory.parent) if directory.parent != directory else None,
                "writable": os.access(directory, os.W_OK | os.X_OK),
                "directories": [{"name": name, "path": path} for name, path in children[:500]],
                "truncated": len(children) > 500}

    @app.post("/admin/api/profiles")
    async def create_profile(request: Request):
        _, error = session(request, mutate=True)
        if error:
            return error
        try:
            profile = Profile.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            return denied(400, str(exc)[:2000])
        if service.store.profile(profile.slug):
            return denied(409, "Profile already exists")
        service.store.save_profile(profile)
        return {"profile": profile.model_dump()}

    @app.put("/admin/api/profiles/{slug}")
    async def update_profile(slug: str, request: Request):
        _, error = session(request, mutate=True)
        if error:
            return error
        if not service.store.profile(slug):
            return denied(404, "Unknown profile")
        try:
            profile = Profile.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            return denied(400, str(exc)[:2000])
        if profile.slug != slug:
            return denied(400, "Profile slug cannot be changed")
        service.store.save_profile(profile)
        return {"profile": profile.model_dump()}

    @app.delete("/admin/api/profiles/{slug}")
    async def delete_profile(slug: str, request: Request):
        _, error = session(request, mutate=True)
        if error:
            return error
        if service.store.profile(slug) and len(service.store.profiles()) == 1:
            return denied(409, "Keep at least one profile")
        if any(key[0] == slug for key in service.jobs):
            return denied(409, "Profile has active downloads")
        try:
            deleted = service.store.delete_profile(slug)
        except ValueError as exc:
            return denied(409, str(exc))
        return {} if deleted else denied(404, "Unknown profile")

    @app.post("/admin/api/preview")
    async def preview(request: Request):
        _, error = session(request, mutate=True)
        if error:
            return error
        try:
            data = await request.json()
            profile = Profile.model_validate(data["profile"])
            raw_title = str(data["title"])[:1024]
            channel_id = str(data.get("channel_id") or "")
        except (ValidationError, KeyError, ValueError, TypeError) as exc:
            return denied(400, str(exc)[:2000])
        channel = next((item for item in profile.channels if item.id == channel_id), None)
        artist, title = parse_metadata(raw_title, channel)
        return {"artist": artist, "title": title}

    @app.post("/admin/api/profiles/{slug}/keys")
    async def create_key(slug: str, request: Request):
        _, error = session(request, mutate=True)
        if error:
            return error
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("request body must be an object")
            label = str(data["label"])
            output_dir = data.get("output_dir")
            if output_dir is not None and not isinstance(output_dir, str):
                raise ValueError("output_dir must be a path string or null")
            key_id, secret = service.store.add_key(slug, label, output_dir)
        except (ValueError, KeyError, TypeError) as exc:
            return denied(400, str(exc)[:1000])
        return {"id": key_id, "secret": secret}  # shown only once

    @app.put("/admin/api/profiles/{slug}/keys/{key_id}")
    async def update_key(slug: str, key_id: str, request: Request):
        _, error = session(request, mutate=True)
        if error:
            return error
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("request body must be an object")
            output_dir = data["output_dir"]
            if output_dir is not None and not isinstance(output_dir, str):
                raise ValueError("output_dir must be a path string or null")
            changed = service.store.set_key_output_dir(slug, key_id, output_dir)
        except (ValueError, KeyError, TypeError) as exc:
            return denied(400, str(exc)[:1000])
        return {} if changed else denied(404, "Unknown active key")

    @app.delete("/admin/api/profiles/{slug}/keys/{key_id}")
    async def revoke_key(slug: str, key_id: str, request: Request):
        _, error = session(request, mutate=True)
        if error:
            return error
        return {} if service.store.revoke_key(slug, key_id) else denied(404, "Unknown key")

    @app.post("/admin/api/updates/config")
    async def update_config(request: Request):
        _, error = session(request, mutate=True)
        if error:
            return error
        try:
            data = await request.json()
            updater.configure(str(data["channel"]), int(data["interval_hours"]))
        except (ValueError, KeyError, TypeError) as exc:
            return denied(400, str(exc)[:1000])
        return {"channel": updater.channel, "interval_hours": updater.interval_hours}

    @app.post("/admin/api/updates/run")
    async def run_update(request: Request):
        _, error = session(request, mutate=True)
        if error:
            return error
        return JSONResponse({"started": updater.trigger()}, status_code=202)

    @app.post("/admin/api/password")
    async def change_password(request: Request):
        _, error = session(request, mutate=True)
        if error:
            return error
        try:
            data = await request.json()
            current, new = str(data["current"]), str(data["new"])
            password_hash = service.store.setting("admin_password_hash")
            if not password_hash or not HASHER.verify(password_hash, current):
                return denied(403, "Current password is incorrect")
            if len(new) < 12:
                return denied(400, "New password must be at least 12 characters")
        except (KeyError, TypeError, VerifyMismatchError):
            return denied(403, "Current password is incorrect")
        service.store.set_setting("admin_password_hash", HASHER.hash(new))
        service.store.delete_all_sessions()
        return {}

    @app.post("/admin/api/logout")
    async def logout(request: Request):
        _, error = session(request, mutate=True)
        if error:
            return error
        service.store.delete_session(request.cookies["admin_session"])
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("admin_session", path="/")
        return response

    return app
