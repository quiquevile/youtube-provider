import asyncio
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx
import pytest

from youtube_provider.api import create_api
from youtube_provider.service import ProviderService, ndjson_lines, video_id_from_query
from youtube_provider.storage import Store


def test_concurrent_password_setup_persists_only_the_first_hash(tmp_path):
    barrier = Barrier(2)

    def submit(store, candidate):
        barrier.wait(timeout=5)
        return store.initialize_admin_password(candidate)

    stores = [Store(tmp_path), Store(tmp_path)]
    candidates = ["first-hash", "second-hash"]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            accepted = list(pool.map(submit, stores, candidates))
        assert accepted.count(True) == 1
    finally:
        for store in stores:
            store.close()

    reopened = Store(tmp_path)
    try:
        assert reopened.setting("admin_password_hash") == candidates[accepted.index(True)]
    finally:
        reopened.close()


def test_keys_are_profile_scoped_and_revocable(tmp_path: Path):
    store = Store(tmp_path)
    profile = store.profile("karaoke")
    assert profile is not None
    second = profile.model_copy(update={"slug": "other"})
    store.save_profile(second)
    key_id, secret = store.add_key("karaoke", "living room")
    assert store.authenticate_key("karaoke", secret) == key_id
    assert store.authenticate_key("other", secret) is None
    assert store.revoke_key("karaoke", key_id)
    assert store.authenticate_key("karaoke", secret) is None
    store.close()


def test_existing_key_table_is_migrated_and_client_folders_are_editable(tmp_path: Path):
    db = sqlite3.connect(tmp_path / "provider.sqlite3")
    db.execute("CREATE TABLE api_keys (id TEXT PRIMARY KEY, profile TEXT NOT NULL, label TEXT NOT NULL, "
               "digest TEXT NOT NULL UNIQUE, created REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)")
    db.close()
    store = Store(tmp_path)
    key_id, _ = store.add_key("karaoke", "client")
    assert store.key_output_dir("karaoke", key_id) is None
    assert store.set_key_output_dir("karaoke", key_id, str(tmp_path / "client"))
    assert store.key_output_dir("karaoke", key_id) == str(tmp_path / "client")
    assert store.keys("karaoke")[0]["output_dir"] == str(tmp_path / "client")
    with pytest.raises(ValueError, match="absolute"):
        store.set_key_output_dir("karaoke", key_id, "relative/path")
    assert store.set_key_output_dir("karaoke", key_id, "")
    assert store.key_output_dir("karaoke", key_id) is None
    assert store.revoke_key("karaoke", key_id)
    assert not store.set_key_output_dir("karaoke", key_id, str(tmp_path / "elsewhere"))
    store.close()


def test_clean_media_names_are_reserved_without_overwriting_other_files(tmp_path: Path):
    store = Store(tmp_path)
    first, _ = store.add_key("karaoke", "first")
    second, _ = store.add_key("karaoke", "second")
    directory = tmp_path / "media"
    directory.mkdir()
    (directory / "Elton John - Your Song.mp4").write_bytes(b"existing")
    first_path = store.reserve_output_path("karaoke", first, "s2XQ76mFwno", directory,
                                           "Elton John - Your Song.mp4")
    second_path = store.reserve_output_path("karaoke", second, "fGI2vCk6ykY", directory,
                                            "Elton John - Your Song.mp4")
    assert first_path.name == "Elton John - Your Song (2).mp4"
    assert second_path.name == "Elton John - Your Song (3).mp4"
    assert store.reserve_output_path("karaoke", first, "s2XQ76mFwno", directory,
                                     "Elton John - Your Song.mp4") == first_path
    store.close()
    reopened = Store(tmp_path)
    assert reopened.reserve_output_path("karaoke", first, "s2XQ76mFwno", directory,
                                        "Elton John - Your Song.mp4") == first_path
    reopened.close()


@pytest.mark.parametrize("base", ["/assets", "ftp://host/icons", "https://user:pass@host/icons", "https://host/?x=1"])
def test_icon_base_must_be_a_public_http_directory(tmp_path, base):
    store = Store(tmp_path)
    try:
        with pytest.raises(ValueError, match="YSP_ICON_BASE_URL"):
            create_api(ProviderService(store), icon_base_url=base)
    finally:
        store.close()


@pytest.mark.parametrize("icon_base", [None, "https://karaoke.example/assets/provider-icons/"])
def test_auth_discovery_fetch_validation_and_consumer_isolation(tmp_path: Path, icon_base):
    store = Store(tmp_path)
    one_id, one_secret = store.add_key("karaoke", "one")
    two_id, two_secret = store.add_key("karaoke", "two")
    item = {"id": "dQw4w9WgXcQ", "title": "Song", "artist": "Artist", "source": "KaraFun",
            "iconUrl": "http://localhost/icons/youtube.svg", "channel_id": store.profile("karaoke").channels[0].id}
    store.put_item("karaoke", item)
    service = ProviderService(store)
    # Keep the HTTP test focused on protocol behavior without starting a network worker.
    service._download = lambda *args: __import__("asyncio").sleep(0)

    async def fake_search(profile, query, icon_url):
        yield json.dumps([{**{key: item[key] for key in ("id", "title", "artist", "source")},
                           "iconUrl": icon_url}]).encode() + b"\n"

    service.search = fake_search
    async def exercise():
        icon_dir = tmp_path / "shared-icons"
        icon_dir.mkdir()
        (icon_dir / "youtube.svg").write_bytes(b"old icon from previous deployment")
        app = create_api(service, icon_dir=icon_dir, icon_base_url=icon_base)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://localhost") as client:
            base = "/profiles/karaoke/api"
            assert (await client.get(base)).json()["types"] == {"song": {"versions": [0]}}
            assert (await client.get(base + "/auth/v0")).status_code == 401
            headers = {"Authorization": f"bEaReR {one_secret}"}
            assert (await client.get(base + "/auth/v0", headers=headers)).status_code == 204
            assert (await client.post(base + "/song/v0/search", headers=headers, json={"query": " "})).status_code == 400
            assert (await client.post(base + "/song/v0/search", content="{}",
                                      headers={**headers, "Content-Type": "text/plain"})).status_code == 400
            found = await client.post(base + "/song/v0/search", headers=headers, json={"query": "ABBA"})
            assert found.status_code == 200
            assert found.headers["content-type"].startswith("application/x-ndjson")
            assert json.loads(found.text)[0]["title"] == "Song"
            icon = await client.get("/icons/youtube.svg")
            assert icon.status_code == 200 and icon.headers["content-type"] == "image/svg+xml"
            assert icon.content.startswith(b"<svg") and (icon_dir / "youtube.svg").read_bytes() == icon.content
            version = hashlib.sha256(icon.content).hexdigest()[:16]
            expected = f"{icon_base}youtube.svg?v={version}" if icon_base else "http://localhost/icons/youtube.svg"
            assert json.loads(found.text)[0]["iconUrl"] == expected
            fetch_url = base + "/song/v0/items/dQw4w9WgXcQ/fetch"
            assert (await client.post(fetch_url, headers=headers, json={"timestamp": -1})).status_code == 400
            assert (await client.post(fetch_url, headers=headers,
                                      json={"timestamp": 1, "meta": {"x": "y" * 5000}})).status_code == 400
            assert (await client.post(base + "/song/v0/items/unknown/fetch", headers=headers,
                                      json={"timestamp": 1})).status_code == 404
            assert (await client.post(fetch_url, headers=headers, json={"timestamp": 1, "meta": {"room": 1}})).status_code == 202
            assert store.download("karaoke", one_id, item["id"])["meta"] == {"room": 1}
            assert store.download("karaoke", two_id, item["id"]) is None
            assert (await client.post(base + "/song/v0/items/dQw4w9WgXcQ/cancel",
                                      headers={"Authorization": f"Bearer {two_secret}"})).status_code == 404
            assert (await client.post(base + "/song/v0/items/dQw4w9WgXcQ/cancel", headers=headers)).status_code == 202
            assert store.download("karaoke", one_id, item["id"])["status"] == "canceled"

    asyncio.run(exercise())
    store.close()


def test_ndjson_splits_at_utf8_limit_and_rejects_lookalike_urls():
    items = [{"id": str(i), "title": "canción" * 100, "artist": "a", "source": "s",
              "iconUrl": "https://example.test/icon"} for i in range(100)]
    lines = ndjson_lines(items)
    assert len(lines) > 1
    assert all(len(line) <= 65536 for line in lines)
    assert sum(len(json.loads(line)) for line in lines) == 100
    assert video_id_from_query("https://youtube.com.evil.test/watch?v=dQw4w9WgXcQ") is None
    assert video_id_from_query("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
