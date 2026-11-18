"""Provider orchestration, worker isolation and per-consumer event delivery."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from .model import Profile
from .storage import Store


VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
JobKey = tuple[str, str, str]


def video_id_from_query(query: str) -> str | None:
    try:
        url = urlparse(query.strip())
    except ValueError:
        return None
    if url.scheme not in ("http", "https"):
        return None
    host = (url.hostname or "").lower()
    if host == "youtu.be":
        candidate = url.path.strip("/")
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        candidate = (parse_qs(url.query).get("v") or [""])[0] if url.path == "/watch" else (
            url.path.split("/")[2] if url.path.startswith(("/shorts/", "/embed/")) and len(url.path.split("/")) > 2 else "")
    else:
        return None
    return candidate if VIDEO_ID.fullmatch(candidate) else None


def safe_filename(artist: str, title: str) -> str:
    name = f"{artist} - {title}" if artist else title
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f\x7f]", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")[:180].strip(" .") or "video"
    return f"{name}.mp4"


def public_item(item: dict) -> dict:
    fields = ("id", "title", "artist", "source", "iconUrl", "duration")
    return {key: item[key] for key in fields if key in item}


def ndjson_lines(items: list[dict]) -> list[bytes]:
    """Keep each UTF-8 encoded JSON array line within the protocol's 64 KiB cap."""
    lines: list[bytes] = []
    batch: list[dict] = []
    for item in items:
        candidate = batch + [public_item(item)]
        encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > 65536:
            if batch:
                lines.append(json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
            batch = [public_item(item)]
            if len(json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode() + b"\n") > 65536:
                batch = []
        else:
            batch = candidate
    if batch:
        lines.append(json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
    return lines


class WorkerError(RuntimeError):
    pass


class ProviderService:
    def __init__(self, store: Store, *, public_base_url: str | None = None, concurrency: int = 2):
        self.store = store
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self.jobs: dict[JobKey, asyncio.Task] = {}
        self.processes: dict[JobKey, asyncio.subprocess.Process] = {}
        self.active_states: dict[JobKey, dict] = {}
        self.listeners: dict[tuple[str, str], set[asyncio.Queue]] = {}
        self.capacity = asyncio.Semaphore(concurrency)
        self.search_capacity = asyncio.Semaphore(4)
        self.requests: dict[tuple[str, str, str], list[float]] = {}

    def allowed(self, profile: str, key_id: str, action: str, limit: int) -> bool:
        now = time.monotonic()
        bucket = (profile, key_id, action)
        recent = [stamp for stamp in self.requests.get(bucket, []) if stamp > now - 60]
        if len(recent) >= limit:
            self.requests[bucket] = recent
            return False
        recent.append(now)
        self.requests[bucket] = recent
        return True

    def icon_url(self, request_base: str) -> str:
        return f"{self.public_base_url or request_base.rstrip('/')}/icons/youtube.svg"

    def subscribe(self, profile: str, key_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.listeners.setdefault((profile, key_id), set()).add(queue)
        return queue

    def unsubscribe(self, profile: str, key_id: str, queue: asyncio.Queue) -> None:
        listeners = self.listeners.get((profile, key_id))
        if listeners:
            listeners.discard(queue)

    def publish(self, profile: str, key_id: str, state: dict) -> None:
        self.store.put_download(profile, key_id, state)
        for queue in tuple(self.listeners.get((profile, key_id), ())):
            try:
                queue.put_nowait(dict(state))
            except asyncio.QueueFull:
                self.unsubscribe(profile, key_id, queue)

    def worker_env(self) -> dict[str, str]:
        env = os.environ.copy()
        target = self.store.setting("worker_package_dir")
        if target and Path(target).is_dir():
            env["PYTHONPATH"] = target + os.pathsep + env.get("PYTHONPATH", "")
        env["YTDLP_NO_PLUGINS"] = "1"
        return env

    async def run_worker(self, payload: dict, on_event: Callable[[dict], Awaitable[None]],
                         *, timeout: int, job_key: JobKey | None = None) -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "youtube_provider.worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self.worker_env(), start_new_session=True,
        )
        if job_key:
            self.processes[job_key] = process
        try:
            assert process.stdin and process.stdout and process.stderr
            stderr_task = asyncio.create_task(process.stderr.read())
            process.stdin.write(json.dumps(payload).encode() + b"\n")
            await process.stdin.drain()
            process.stdin.close()

            async def consume() -> None:
                while line := await process.stdout.readline():
                    if len(line) > 2_000_000:
                        raise WorkerError("worker event is too large")
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise WorkerError("invalid worker event") from exc
                    if event.get("event") == "error":
                        raise WorkerError(str(event.get("message", "worker failed"))[:1024])
                    await on_event(event)
                code = await process.wait()
                if code:
                    raise WorkerError("yt-dlp worker failed")

            await asyncio.wait_for(consume(), timeout)
        except BaseException:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            raise
        finally:
            if job_key:
                self.processes.pop(job_key, None)
            await stderr_task

    async def search_source(self, profile: Profile, query: str, channel_id: str | None,
                            icon_url: str, video_id: str | None = None) -> list[dict]:
        found: list[dict] = []

        async def collect(event: dict) -> None:
            if event.get("event") != "results" or not isinstance(event.get("items"), list):
                return
            for raw in event["items"]:
                if not isinstance(raw, dict) or not VIDEO_ID.fullmatch(str(raw.get("id", ""))):
                    continue
                item = {**raw, "iconUrl": icon_url}
                if item.get("duration") is None:
                    item.pop("duration", None)
                self.store.put_item(profile.slug, item)
                found.append(item)

        async with self.search_capacity:
            await self.run_worker({"action": "search", "profile": profile.model_dump(), "query": query,
                                   "channel_id": channel_id, "video_id": video_id}, collect, timeout=90)
        return found

    async def search(self, profile: Profile, query: str, icon_url: str):
        direct_id = video_id_from_query(query)
        channels = [None] if direct_id or not profile.channels else [channel.id for channel in profile.channels]
        queue: asyncio.Queue = asyncio.Queue()
        seen: set[str] = set()

        async def one(channel_id: str | None) -> None:
            try:
                items = await self.search_source(profile, query, channel_id, icon_url, direct_id)
                await queue.put(("items", items))
            except Exception as exc:
                await queue.put(("error", exc))
            finally:
                await queue.put(("done", None))

        tasks = [asyncio.create_task(one(channel_id)) for channel_id in channels]
        errors = 0
        completed = 0
        try:
            while completed < len(tasks):
                kind, payload = await queue.get()
                if kind == "done":
                    completed += 1
                elif kind == "error":
                    errors += 1
                else:
                    unique = []
                    for item in payload:
                        if item["id"] not in seen:
                            seen.add(item["id"])
                            unique.append(item)
                    for line in ndjson_lines(unique):
                        yield line
            if errors:
                raise WorkerError("YouTube search was incomplete")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def fetch(self, profile: Profile, key_id: str, item: dict, timestamp: float, meta: dict | None) -> None:
        job_key = (profile.slug, key_id, item["id"])
        state = self.active_states.get(job_key) or self.store.download(*job_key)
        running = job_key in self.jobs and not self.jobs[job_key].done()
        if state and (running or (state["status"] == "complete" and Path(state.get("path", "")).is_file())):
            state["timestamp"] = timestamp
            if meta is None:
                state.pop("meta", None)
            else:
                state["meta"] = meta
            self.publish(profile.slug, key_id, state)
            return
        state = {**public_item(item), "status": "pending", "progress": 0.0, "timestamp": timestamp}
        if meta is not None:
            state["meta"] = meta
        self.publish(profile.slug, key_id, state)
        self.active_states[job_key] = state
        task = asyncio.create_task(self._download(profile, key_id, item, state))
        self.jobs[job_key] = task

    async def _download(self, profile: Profile, key_id: str, item: dict, state: dict) -> None:
        job_key = (profile.slug, key_id, item["id"])
        filename = safe_filename(item["artist"], item["title"])
        output_dir = Path(self.store.key_output_dir(profile.slug, key_id) or profile.output_dir)
        stage = output_dir / ".partial" / key_id / item["id"]
        final_path: Path | None = None
        try:
            async with self.capacity:
                if state["status"] == "canceled":
                    return
                final_path = self.store.reserve_output_path(profile.slug, key_id, item["id"],
                                                            output_dir, filename)
                if final_path.is_file():
                    state.update(status="complete", progress=1.0, path=str(final_path))
                    self.publish(profile.slug, key_id, state)
                    return

                async def update(event: dict) -> None:
                    if state["status"] == "canceled":
                        return
                    if event.get("event") == "selected":
                        state["status"] = "downloading"
                    elif event.get("event") == "progress":
                        state["status"] = "downloading"
                        value = event.get("progress")
                        if isinstance(value, (int, float)) and math.isfinite(value):
                            state["progress"] = max(state["progress"], min(0.99, max(0, value)))
                        eta = event.get("eta")
                        if isinstance(eta, (int, float)) and math.isfinite(eta) and eta >= 0:
                            state["eta"] = eta
                    elif event.get("event") == "complete":
                        if Path(event["path"]).is_file():
                            state.update(status="complete", progress=1.0, path=event["path"])
                            state.pop("eta", None)
                    self.publish(profile.slug, key_id, state)

                worker_profile = profile.model_copy(update={"output_dir": str(output_dir)})
                await self.run_worker({"action": "download", "profile": worker_profile.model_dump(),
                                       "video_id": item["id"], "final_path": str(final_path),
                                       "stage_dir": str(stage)}, update, timeout=7200, job_key=job_key)
                if state["status"] != "complete":
                    raise WorkerError("worker finished without a complete file")
        except asyncio.CancelledError:
            state["status"] = "canceled"
            state.pop("eta", None)
            self.publish(profile.slug, key_id, state)
        except Exception as exc:
            state["status"] = "error"
            state["error"] = str(exc)[:1024]
            state.pop("eta", None)
            self.publish(profile.slug, key_id, state)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            if final_path is not None and state["status"] != "complete" and not final_path.exists():
                self.store.release_output_path(profile.slug, key_id, item["id"], final_path)
            self.jobs.pop(job_key, None)
            self.active_states.pop(job_key, None)

    def cancel(self, profile: str, key_id: str, item_id: str) -> bool:
        job_key = (profile, key_id, item_id)
        state = self.active_states.get(job_key) or self.store.download(profile, key_id, item_id)
        if not state:
            return False
        if state["status"] in ("pending", "downloading"):
            state["status"] = "canceled"
            state.pop("eta", None)
            job = self.jobs.get(job_key)
            if job:
                job.cancel()
        self.publish(profile, key_id, state)
        return True

    async def stop(self) -> None:
        tasks = tuple(self.jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
