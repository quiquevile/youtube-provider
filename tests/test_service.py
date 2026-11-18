import asyncio
import json
from pathlib import Path

import pytest
from starlette.requests import Request

from youtube_provider.api import create_api
from youtube_provider.service import ProviderService, WorkerError, public_item
from youtube_provider.storage import Store


def item(store: Store) -> dict:
    return {"id": "dQw4w9WgXcQ", "title": "Dancing Queen", "artist": "ABBA",
            "source": "KaraFun Karaoke", "iconUrl": "https://example.test/icons/youtube.svg",
            "channel_id": store.profile("karaoke").channels[0].id}


def test_download_publishes_progress_completion_and_repeats_without_work(tmp_path: Path):
    store = Store(tmp_path)
    profile = store.profile("karaoke")
    key_id, _ = store.add_key("karaoke", "client")
    result = item(store)
    service = ProviderService(store)
    calls = []

    async def fake_worker(payload, on_event, *, timeout, job_key=None):
        calls.append(payload)
        await on_event({"event": "selected"})
        await on_event({"event": "progress", "progress": 0.5, "eta": 10})
        await on_event({"event": "progress", "progress": 0.3})  # progress cannot go backward
        path = Path(payload["final_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mp4")
        await on_event({"event": "complete", "path": str(path)})

    service.run_worker = fake_worker

    async def exercise():
        queue = service.subscribe("karaoke", key_id)
        service.fetch(profile, key_id, result, 10, {"room": 1})
        await service.jobs[("karaoke", key_id, result["id"])]
        states = [queue.get_nowait() for _ in range(5)]
        assert [state["status"] for state in states] == ["pending", "downloading", "downloading", "downloading", "complete"]
        assert states[3]["progress"] == 0.5
        assert states[-1]["progress"] == 1
        assert Path(states[-1]["path"]).is_file()
        assert Path(states[-1]["path"]).name == "ABBA - Dancing Queen.mp4"
        assert Path(states[-1]["path"]).parent == Path(profile.output_dir)
        service.fetch(profile, key_id, result, 11, None)
        assert len(calls) == 1
        updated = queue.get_nowait()
        assert updated["timestamp"] == 11 and "meta" not in updated
        Path(updated["path"]).unlink()
        service.fetch(profile, key_id, result, 12, None)
        await service.jobs[("karaoke", key_id, result["id"])]
        assert len(calls) == 2
        service.unsubscribe("karaoke", key_id, queue)

    asyncio.run(exercise())
    store.close()


def test_client_folder_overrides_profile_directory_for_new_downloads(tmp_path: Path):
    store = Store(tmp_path)
    profile = store.profile("karaoke")
    default_key, _ = store.add_key("karaoke", "default")
    custom_dir = tmp_path / "private-client"
    custom_key, _ = store.add_key("karaoke", "custom", str(custom_dir))
    service = ProviderService(store)
    original = item(store)
    payloads = []

    async def fake_worker(payload, on_event, *, timeout, job_key=None):
        payloads.append(payload)
        path = Path(payload["final_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mp4")
        await on_event({"event": "complete", "path": str(path)})

    service.run_worker = fake_worker

    async def exercise():
        service.fetch(profile, default_key, original, 1, None)
        service.fetch(profile, custom_key, original, 1, None)
        await asyncio.gather(*tuple(service.jobs.values()))
        default_state = store.download("karaoke", default_key, original["id"])
        custom_state = store.download("karaoke", custom_key, original["id"])
        assert Path(default_state["path"]).parent == Path(profile.output_dir)
        assert Path(custom_state["path"]).parent == custom_dir
        assert Path(custom_state["path"]).name == "ABBA - Dancing Queen.mp4"
        assert {payload["profile"]["output_dir"] for payload in payloads} == {
            profile.output_dir, str(custom_dir)}

        new_dir = tmp_path / "new-client-folder"
        assert store.set_key_output_dir("karaoke", custom_key, str(new_dir))
        other = {**original, "id": "fGI2vCk6ykY", "title": "Your Song"}
        service.fetch(profile, custom_key, other, 2, None)
        await service.jobs[("karaoke", custom_key, other["id"])]
        assert Path(store.download("karaoke", custom_key, other["id"])["path"]).parent == new_dir
        service.fetch(profile, custom_key, original, 3, None)
        assert store.download("karaoke", custom_key, original["id"])["path"] == custom_state["path"]

    asyncio.run(exercise())
    store.close()


def test_failed_download_releases_clean_filename(tmp_path: Path):
    store = Store(tmp_path)
    profile = store.profile("karaoke")
    key_id, _ = store.add_key("karaoke", "client")
    service = ProviderService(store)
    first = item(store)

    async def failed_worker(payload, on_event, *, timeout, job_key=None):
        raise WorkerError("download failed")

    service.run_worker = failed_worker

    async def exercise():
        service.fetch(profile, key_id, first, 1, None)
        await service.jobs[("karaoke", key_id, first["id"])]
        assert store.download("karaoke", key_id, first["id"])["status"] == "error"
        second = {**first, "id": "fGI2vCk6ykY"}
        path = store.reserve_output_path("karaoke", key_id, second["id"], Path(profile.output_dir),
                                         "ABBA - Dancing Queen.mp4")
        assert path.name == "ABBA - Dancing Queen.mp4"

    asyncio.run(exercise())
    store.close()


def test_worker_process_reports_errors_without_leaving_children(tmp_path: Path):
    store = Store(tmp_path)
    service = ProviderService(store)

    async def exercise():
        events = []
        async def collect(event):
            events.append(event)
        with pytest.raises(WorkerError, match="unknown worker action"):
            await service.run_worker({"action": "invalid"}, collect, timeout=5)
        assert not events

    asyncio.run(exercise())
    store.close()


def test_cancel_isolated_by_key_and_preserves_timestamp(tmp_path: Path):
    store = Store(tmp_path)
    profile = store.profile("karaoke")
    first, _ = store.add_key("karaoke", "first")
    second, _ = store.add_key("karaoke", "second")
    result = item(store)
    service = ProviderService(store)
    started = asyncio.Event()

    async def fake_worker(payload, on_event, *, timeout, job_key=None):
        started.set()
        await asyncio.sleep(3600)

    service.run_worker = fake_worker

    async def exercise():
        service.fetch(profile, first, result, 42, None)
        service.fetch(profile, second, result, 99, None)
        await asyncio.wait_for(started.wait(), 2)
        assert service.cancel("karaoke", first, result["id"])
        await asyncio.sleep(0)
        assert store.download("karaoke", first, result["id"])["status"] == "canceled"
        assert store.download("karaoke", first, result["id"])["timestamp"] == 42
        assert store.download("karaoke", second, result["id"])["status"] == "pending"
        await service.stop()

    asyncio.run(exercise())
    store.close()


def test_search_stream_deduplicates_sources_and_status_filters_since(tmp_path: Path):
    store = Store(tmp_path)
    profile = store.profile("karaoke")
    key_id, secret = store.add_key("karaoke", "client")
    service = ProviderService(store)
    result = item(store)

    async def fake_source(profile, query, channel_id, icon_url, video_id=None):
        return [{**result, "iconUrl": icon_url}]

    service.search_source = fake_source

    async def exercise():
        lines = [line async for line in service.search(profile, "ABBA", "https://example.test/icon.svg")]
        assert len(lines) == 1
        assert json.loads(lines[0]) == [public_item({**result, "iconUrl": "https://example.test/icon.svg"})]
        service.publish("karaoke", key_id, {**public_item(result), "status": "pending", "progress": 0, "timestamp": 5})
        app = create_api(service)
        route = next(route for route in app.routes if getattr(route, "path", "") == "/profiles/{slug}/api/song/v0/status")
        scope = {"type": "http", "method": "GET", "path": "/profiles/karaoke/api/song/v0/status",
                 "headers": [(b"authorization", f"Bearer {secret}".encode())], "query_string": b"since=10",
                 "scheme": "http", "server": ("localhost", 80)}
        request = Request(scope)
        response = await route.endpoint("karaoke", request)
        stream = response.body_iterator
        assert await anext(stream) == b": connected\n\n"
        service.publish("karaoke", key_id, {**public_item(result), "status": "downloading", "progress": 0.5, "timestamp": 5})
        service.publish("karaoke", key_id, {**public_item(result), "status": "complete", "progress": 1, "timestamp": 12,
                                           "path": str(tmp_path / "complete.mp4")})
        event = await asyncio.wait_for(anext(stream), 1)
        assert json.loads(event[6:])["timestamp"] == 12
        await stream.aclose()
        assert not service.listeners.get(("karaoke", key_id))

    asyncio.run(exercise())
    store.close()
