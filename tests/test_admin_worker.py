import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from youtube_provider.admin import HASHER, create_admin
from youtube_provider.model import default_profile
from youtube_provider.service import ProviderService
from youtube_provider.storage import Store
from youtube_provider.updater import Updater
from youtube_provider import worker


def test_admin_requires_login_and_csrf_then_manages_profile_and_key(tmp_path: Path):
    store = Store(tmp_path)
    store.set_setting("admin_password_hash", HASHER.hash("long-test-password"))
    service = ProviderService(store)
    updater = Updater(store)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_admin(service, updater)),
                                    base_url="http://localhost") as client:
            assert (await client.get("/admin/api/state")).status_code == 401
            assert (await client.post("/login", data={"password": "wrong"})).status_code == 401
            login = await client.post("/login", data={"password": "long-test-password"})
            assert login.status_code == 303
            assert login.cookies.get("admin_session")
            state = (await client.get("/admin/api/state")).json()
            assert state["api_base_url"] == "http://127.0.0.1:3030"
            assert len(state["profiles"][0]["channels"]) == 6
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/static/admin.js")).status_code == 200
            profile = state["profiles"][0]
            assert (await client.put("/admin/api/profiles/karaoke", json=profile)).status_code == 403
            headers = {"X-CSRF-Token": state["csrf"]}
            bad = {**profile, "channels": [{"id": "@fake", "name": "Fake"}]}
            assert (await client.put("/admin/api/profiles/karaoke", headers=headers, json=bad)).status_code == 400
            preview = await client.post("/admin/api/preview", headers=headers, json={
                "profile": profile, "title": "ABBA - Dancing Queen (Karaoke Version)",
                "channel_id": profile["channels"][1]["id"]})
            assert preview.json() == {"artist": "ABBA", "title": "Dancing Queen"}
            reordered = {**profile, "channels": list(reversed(profile["channels"]))}
            assert (await client.put("/admin/api/profiles/karaoke", headers=headers, json=reordered)).status_code == 200
            assert store.profile("karaoke").channels[0].id == profile["channels"][-1]["id"]
            created = await client.post("/admin/api/profiles/karaoke/keys", headers=headers, json={"label": "test client"})
            assert created.status_code == 200
            secret = created.json()["secret"]
            assert store.authenticate_key("karaoke", secret) == created.json()["id"]
            assert secret not in json.dumps((await client.get("/admin/api/state")).json())
            folder_url = f"/admin/api/profiles/karaoke/keys/{created.json()['id']}"
            assert (await client.put(folder_url, json={"output_dir": str(tmp_path / "client")})).status_code == 403
            assert (await client.put(folder_url, headers=headers, json={"output_dir": "relative"})).status_code == 400
            assert (await client.put(folder_url, headers=headers,
                                     json={"output_dir": str(tmp_path / "client")})).status_code == 200
            assert store.key_output_dir("karaoke", created.json()["id"]) == str(tmp_path / "client")
            keys = (await client.get("/admin/api/state")).json()["keys"]["karaoke"]
            assert next(key for key in keys if key["id"] == created.json()["id"])["output_dir"] == str(tmp_path / "client")
            assert (await client.put(folder_url, headers=headers, json={"output_dir": None})).status_code == 200
            assert store.key_output_dir("karaoke", created.json()["id"]) is None
            revoked = await client.delete(f"/admin/api/profiles/karaoke/keys/{created.json()['id']}", headers=headers)
            assert revoked.status_code == 200
            assert store.authenticate_key("karaoke", secret) is None
            assert (await client.post("/admin/api/profiles/karaoke/keys", headers=headers,
                                      json={"label": "invalid folder", "output_dir": "relative"})).status_code == 400
            new_key = await client.post("/admin/api/profiles/karaoke/keys", headers=headers,
                                        json={"label": "other client", "output_dir": str(tmp_path / "other")})
            assert new_key.status_code == 200
            assert store.key_output_dir("karaoke", new_key.json()["id"]) == str(tmp_path / "other")
            assert (await client.post("/admin/api/updates/config", headers=headers,
                                      json={"channel": "nightly", "interval_hours": 24})).status_code == 200
            assert updater.channel == "nightly" and updater.interval_hours == 24
            started = []
            updater.trigger = lambda: (started.append(True) or True)
            assert (await client.post("/admin/api/updates/run", headers=headers)).status_code == 202
            assert started == [True]
            new_profile = {**profile, "slug": "music", "name": "Music Videos", "channels": []}
            assert (await client.post("/admin/api/profiles", headers=headers, json=new_profile)).status_code == 200
            assert (await client.post("/admin/api/profiles", headers=headers, json=new_profile)).status_code == 409
            new_profile["search_terms"] = ["official"]
            assert (await client.put("/admin/api/profiles/music", headers=headers, json=new_profile)).status_code == 200
            assert store.profile("music").search_terms == ["official"]
            assert (await client.delete("/admin/api/profiles/music", headers=headers)).status_code == 200
            assert store.profile("music") is None
            assert (await client.delete("/admin/api/profiles/karaoke", headers=headers)).status_code == 409
            assert (await client.post("/admin/api/password", headers=headers,
                                      json={"current": "wrong", "new": "new-long-password"})).status_code == 403
            assert (await client.post("/admin/api/password", headers=headers,
                                      json={"current": "long-test-password", "new": "new-long-password"})).status_code == 200
            assert (await client.get("/admin/api/state")).status_code == 401

    asyncio.run(exercise())
    store.close()


def test_admin_login_works_through_local_port_forward_and_rejects_cross_site(tmp_path: Path):
    store = Store(tmp_path)
    store.set_setting("admin_password_hash", HASHER.hash("long-test-password"))
    service = ProviderService(store)
    updater = Updater(store)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_admin(service, updater)),
                                    base_url="http://127.0.0.1:3031") as client:
            forwarded = {"Origin": "https://forwarded.example", "Sec-Fetch-Site": "same-origin"}
            login = await client.post("/login", headers=forwarded,
                                      data={"password": "long-test-password"})
            assert login.status_code == 303
            state = (await client.get("/admin/api/state")).json()
            assert (await client.post("/admin/api/preview", headers={
                **forwarded, "X-CSRF-Token": state["csrf"]}, json={
                    "profile": state["profiles"][0], "title": "ABBA - Dancing Queen"})).status_code == 200
            blocked = await client.post("/login", headers={
                "Origin": "https://foreign.example", "Sec-Fetch-Site": "cross-site"},
                data={"password": "long-test-password"})
            assert blocked.status_code == 403
            assert (await client.post("/admin/api/preview", headers={
                "Origin": "https://foreign.example", "Sec-Fetch-Site": "cross-site",
                "X-CSRF-Token": state["csrf"]}, json={
                    "profile": state["profiles"][0], "title": "ABBA - Dancing Queen"})).status_code == 403

    asyncio.run(exercise())
    store.close()


def test_worker_stdout_is_reserved_for_json_events():
    script = (
        "import os, subprocess, sys\n"
        "from youtube_provider.worker import emit, isolate_protocol_output\n"
        "isolate_protocol_output()\n"
        "print('library diagnostic')\n"
        "os.write(1, b'native diagnostic\\n')\n"
        "subprocess.run([sys.executable, '-c', \"print('child diagnostic')\"], check=True)\n"
        "emit({'event': 'progress', 'progress': 0.5})\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"event": "progress", "progress": 0.5}
    assert result.stdout.count("\n") == 1
    assert "library diagnostic" in result.stderr
    assert "native diagnostic" in result.stderr
    assert "child diagnostic" in result.stderr


def test_worker_rejects_channel_lookalikes_and_uses_channel_rule(tmp_path, monkeypatch):
    profile = default_profile(str(tmp_path / "downloads"))
    allowed = profile.channels[0].id
    emitted = []

    class FakeYdl:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, query, download=False):
            if query.startswith("ytsearch"):
                return {"entries": [
                    {"id": "dQw4w9WgXcQ", "title": "Song - Artist | Karaoke Version | KaraFun"},
                    {"id": "AAAAAAAAAAA", "title": "Song - Artist | Karaoke Version | KaraFun"},
                ]}
            if query.endswith("dQw4w9WgXcQ"):
                return {"id": "dQw4w9WgXcQ", "title": "Song - Artist | Karaoke Version | KaraFun", "channel_id": allowed}
            return {"id": "AAAAAAAAAAA", "title": "Song - Artist | Karaoke Version | KaraFun",
                    "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx"}

    monkeypatch.setattr(worker.yt_dlp, "YoutubeDL", FakeYdl)
    monkeypatch.setattr(worker, "emit", emitted.append)
    worker.search({"profile": profile.model_dump(), "query": "Song", "channel_id": allowed})
    assert len(emitted[0]["items"]) == 1
    assert emitted[0]["items"][0]["artist"] == "Artist"


def test_updater_switches_only_after_verification(tmp_path):
    store = Store(tmp_path)
    updater = Updater(store)

    async def fake_command(command, *, timeout, env=None):
        if "install" in command:
            target = Path(command[command.index("--target") + 1])
            (target / "yt_dlp").mkdir()
            return 0, "installed"
        return 0, "2026.10.26\n"

    updater._command = fake_command
    asyncio.run(updater.update())
    assert updater.status == "ok"
    assert Path(store.setting("worker_package_dir")).is_dir()
    assert store.setting("last_update_version") == "2026.10.26"
    store.close()


def test_failed_update_does_not_replace_active_worker(tmp_path):
    store = Store(tmp_path)
    store.set_setting("worker_package_dir", "/old/yt-dlp")
    updater = Updater(store)

    async def failed(command, *, timeout, env=None):
        return 1, "installation unavailable"

    updater._command = failed
    asyncio.run(updater.update())
    assert updater.status == "error"
    assert store.setting("worker_package_dir") == "/old/yt-dlp"
    assert not list((tmp_path / "yt-dlp-versions").glob(".staging-*"))
    store.close()


def test_update_scheduler_starts_due_job_and_stops(tmp_path):
    store = Store(tmp_path)
    updater = Updater(store)
    updater.configure("stable", 1)
    called = []

    async def fake_update():
        called.append(True)

    updater.update = fake_update

    async def exercise():
        updater.start_scheduler()
        await asyncio.sleep(0.05)
        assert called == [True]
        await updater.stop_scheduler()

    asyncio.run(exercise())
    store.close()


def test_unrestricted_profile_accepts_any_channel_but_restricted_profile_does_not(tmp_path, monkeypatch):
    profile = default_profile(str(tmp_path / "downloads"))
    emitted = []

    class FakeYdl:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, query, download=False):
            if query.startswith("ytsearch"):
                return {"entries": [{"id": "dQw4w9WgXcQ", "title": "Artist - Song",
                                     "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "channel": "Unknown"}]}
            return {"id": "dQw4w9WgXcQ", "title": "Artist - Song",
                    "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "channel": "Unknown"}

    monkeypatch.setattr(worker.yt_dlp, "YoutubeDL", FakeYdl)
    monkeypatch.setattr(worker, "emit", emitted.append)
    worker.search({"profile": profile.model_dump(), "query": "Song"})
    assert emitted[-1]["items"] == []
    profile.channels = []
    worker.search({"profile": profile.model_dump(), "query": "Song"})
    assert emitted[-1]["items"][0]["artist"] == "Artist"


def test_worker_download_rechecks_channel_and_writes_atomic_mp4(tmp_path, monkeypatch):
    profile = default_profile(str(tmp_path / "media"))
    video_id = "dQw4w9WgXcQ"
    final_path = Path(profile.output_dir) / "client" / "song.mp4"
    stage = Path(profile.output_dir) / ".partial" / "client" / video_id
    emitted = []
    metadata = {"id": video_id, "channel_id": profile.channels[0].id, "formats": [
        {"format_id": "video", "height": 1080, "vcodec": "avc1.1", "acodec": "none", "ext": "mp4"},
        {"format_id": "audio", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128},
    ]}

    class FakeYdl:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            return metadata

        def download(self, urls):
            (stage / "media.mp4").write_bytes(b"source")

    def fake_ffmpeg(command, **kwargs):
        Path(command[-1]).write_bytes(b"finished")

    monkeypatch.setattr(worker.yt_dlp, "YoutubeDL", FakeYdl)
    monkeypatch.setattr(worker.subprocess, "run", fake_ffmpeg)
    monkeypatch.setattr(worker, "emit", emitted.append)
    payload = {"profile": profile.model_dump(), "video_id": video_id,
               "final_path": str(final_path), "stage_dir": str(stage)}
    worker.download(payload)
    assert final_path.read_bytes() == b"finished"
    assert not stage.exists()
    assert emitted[-1] == {"event": "complete", "path": str(final_path)}

    metadata["channel_id"] = "UCxxxxxxxxxxxxxxxxxxxxxx"
    with pytest.raises(ValueError, match="not allowed"):
        worker.download(payload)
    assert not stage.exists()

    payload["final_path"] = str(tmp_path / "escape.mp4")
    with pytest.raises(ValueError, match="escapes"):
        worker.download(payload)
