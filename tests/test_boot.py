import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get_status(url):
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_server_keeps_admin_and_song_api_on_separate_ports(tmp_path):
    database = tmp_path / "provider.sqlite3"
    assert not database.exists()
    api_port, admin_port = free_port(), free_port()
    command = [sys.executable, "-m", "youtube_provider.cli", "--data-dir", str(tmp_path), "serve",
               "--api-port", str(api_port), "--admin-port", str(admin_port)]
    icons = tmp_path / "shared-icons"
    env = {**os.environ, "YSP_ICON_DIR": str(icons), "YSP_ICON_BASE_URL": "http://karaoke.example/assets/provider-icons"}
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"server exited early: {process.stdout.read()}")
            try:
                if get_status(f"http://127.0.0.1:{api_port}/health") == 200:
                    break
            except (OSError, TimeoutError):
                time.sleep(0.1)
        else:
            raise AssertionError("API did not become ready")
        assert database.is_file()
        with urllib.request.urlopen(f"http://127.0.0.1:{api_port}/icons/youtube.svg", timeout=1) as response:
            assert response.read() == (icons / "youtube.svg").read_bytes()
        assert get_status(f"http://127.0.0.1:{api_port}/profiles/karaoke/api") == 200
        assert get_status(f"http://127.0.0.1:{api_port}/admin/api/state") == 404
        assert get_status(f"http://127.0.0.1:{api_port}/setup") == 404
        with urllib.request.urlopen(f"http://127.0.0.1:{admin_port}/", timeout=1) as page:
            assert b'action="/setup"' in page.read()
        assert get_status(f"http://127.0.0.1:{admin_port}/profiles/karaoke/api") == 404
    finally:
        process.terminate()
        process.communicate(timeout=10)
