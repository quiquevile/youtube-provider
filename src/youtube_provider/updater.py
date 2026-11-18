"""Atomic, versioned yt-dlp package updates for newly started workers."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from .storage import Store


class Updater:
    def __init__(self, store: Store):
        self.store = store
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.status: str = "idle"
        self.message: str = ""
        self._scheduler: asyncio.Task | None = None

    @property
    def channel(self) -> str:
        return self.store.setting("update_channel", "stable") or "stable"

    @property
    def interval_hours(self) -> int:
        try:
            return int(self.store.setting("update_interval_hours", "0") or "0")
        except ValueError:
            return 0

    def configure(self, channel: str, interval_hours: int) -> None:
        if channel not in ("stable", "nightly"):
            raise ValueError("channel must be stable or nightly")
        if interval_hours < 0 or interval_hours > 24 * 30:
            raise ValueError("interval must be between 0 and 720 hours")
        self.store.set_setting("update_channel", channel)
        self.store.set_setting("update_interval_hours", str(interval_hours))

    def trigger(self) -> bool:
        if self.task and not self.task.done():
            return False
        self.task = asyncio.create_task(self.update())
        return True

    async def update(self) -> None:
        async with self.lock:
            self.status = "running"
            self.message = "Installing a verified yt-dlp worker package"
            versions = self.store.data_dir / "yt-dlp-versions"
            versions.mkdir(parents=True, exist_ok=True)
            staging = versions / (".staging-" + uuid.uuid4().hex)
            staging.mkdir()
            try:
                command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                           "--no-input", "--target", str(staging), "yt-dlp", "yt-dlp-ejs"]
                if self.channel == "nightly":
                    command.append("--pre")
                code, output = await self._command(command, timeout=300)
                if code:
                    raise RuntimeError(output[-1000:] or "package installation failed")

                env = os.environ.copy()
                env["PYTHONPATH"] = str(staging) + os.pathsep + env.get("PYTHONPATH", "")
                code, output = await self._command(
                    [sys.executable, "-c", "import yt_dlp, yt_dlp_ejs; print(yt_dlp.version.__version__)"],
                    timeout=30, env=env)
                if code:
                    raise RuntimeError("new yt-dlp package failed its import check")
                version = output.strip()
                if not re.fullmatch(r"[0-9A-Za-z.+_-]{1,64}", version):
                    raise RuntimeError("invalid yt-dlp version from package")
                target = versions / version
                if target.exists():
                    shutil.rmtree(staging)
                else:
                    staging.rename(target)
                # New workers read this pointer. Running processes keep their old imports.
                self.store.set_setting("worker_package_dir", str(target))
                self.store.set_setting("last_update_at", str(time.time()))
                self.store.set_setting("last_update_version", version)
                self.status = "ok"
                self.message = f"yt-dlp {version} is active for new jobs"
            except Exception as exc:
                self.status = "error"
                self.message = str(exc)[:1024]
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    async def _command(self, command: list[str], *, timeout: int,
                       env: dict[str, str] | None = None) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.STDOUT, env=env)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError("yt-dlp update timed out") from None
        return process.returncode or 0, output.decode(errors="replace")

    def start_scheduler(self) -> None:
        if not self._scheduler:
            self._scheduler = asyncio.create_task(self._schedule())

    async def stop_scheduler(self) -> None:
        if self._scheduler:
            self._scheduler.cancel()
            await asyncio.gather(self._scheduler, return_exceptions=True)
            self._scheduler = None
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _schedule(self) -> None:
        while True:
            interval = self.interval_hours
            if interval:
                last = float(self.store.setting("last_update_at", "0") or "0")
                if time.time() - last >= interval * 3600 and not self.lock.locked():
                    self.trigger()
            await asyncio.sleep(60)
