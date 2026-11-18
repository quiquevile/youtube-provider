import sys

from youtube_provider import cli
from youtube_provider.storage import Store


def test_local_password_bootstrap_and_serve_validation(tmp_path, monkeypatch):
    values = iter(["long-test-password", "long-test-password"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: next(values))
    monkeypatch.setattr(sys, "argv", ["youtube-song-provider", "--data-dir", str(tmp_path), "admin", "set-password"])
    cli.main()
    store = Store(tmp_path)
    assert store.setting("admin_password_hash").startswith("$argon2id$")
    store.close()

    called = []
    monkeypatch.setattr(cli.asyncio, "run", lambda coroutine: (called.append(coroutine), coroutine.close()))
    monkeypatch.setattr(sys, "argv", ["youtube-song-provider", "--data-dir", str(tmp_path), "serve",
                                        "--api-port", "3030", "--admin-port", "3031"])
    cli.main()
    assert len(called) == 1
