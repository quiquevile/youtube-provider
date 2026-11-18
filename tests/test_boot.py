import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from youtube_provider.admin import HASHER
from youtube_provider.storage import Store


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
    store = Store(tmp_path)
    store.set_setting("admin_password_hash", HASHER.hash("long-test-password"))
    store.close()
    api_port, admin_port = free_port(), free_port()
    command = [sys.executable, "-m", "youtube_provider.cli", "--data-dir", str(tmp_path), "serve",
               "--api-port", str(api_port), "--admin-port", str(admin_port)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
        assert get_status(f"http://127.0.0.1:{api_port}/profiles/karaoke/api") == 200
        assert get_status(f"http://127.0.0.1:{api_port}/admin/api/state") == 404
        assert get_status(f"http://127.0.0.1:{admin_port}/login") == 200
        assert get_status(f"http://127.0.0.1:{admin_port}/profiles/karaoke/api") == 404
    finally:
        process.terminate()
        process.communicate(timeout=10)
