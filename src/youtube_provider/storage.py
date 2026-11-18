"""SQLite state for profiles, credentials, search items and per-client downloads."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .model import Profile, default_profile


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.data_dir / "provider.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock, self.db:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (
                    slug TEXT PRIMARY KEY, config TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY, profile TEXT NOT NULL, label TEXT NOT NULL,
                    digest TEXT NOT NULL UNIQUE, created REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (profile) REFERENCES profiles(slug)
                );
                CREATE TABLE IF NOT EXISTS media_paths (
                    profile TEXT NOT NULL, key_id TEXT NOT NULL, id TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE, PRIMARY KEY (profile, key_id, id)
                );
                CREATE TABLE IF NOT EXISTS items (
                    profile TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                    created REAL NOT NULL, PRIMARY KEY (profile, id)
                );
                CREATE TABLE IF NOT EXISTS downloads (
                    profile TEXT NOT NULL, key_id TEXT NOT NULL, id TEXT NOT NULL,
                    data TEXT NOT NULL, PRIMARY KEY (profile, key_id, id)
                );
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    digest TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL
                );
            """)
            key_columns = {row[1] for row in self.db.execute("PRAGMA table_info(api_keys)")}
            if "output_dir" not in key_columns:
                self.db.execute("ALTER TABLE api_keys ADD COLUMN output_dir TEXT")
            initialized = self.db.execute("SELECT 1 FROM settings WHERE name='initialized'").fetchone()
            if not initialized:
                output_root = Path(os.environ.get("YSP_OUTPUT_ROOT", str(self.data_dir / "downloads"))).resolve()
                profile = default_profile(str(output_root / "karaoke"))
                self.db.execute("INSERT INTO profiles VALUES (?, ?)", (profile.slug, profile.model_dump_json()))
                self.db.execute("INSERT INTO settings VALUES ('initialized', '1')")

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def profiles(self) -> list[Profile]:
        with self.lock:
            rows = self.db.execute("SELECT config FROM profiles ORDER BY slug").fetchall()
        return [Profile.model_validate_json(row[0]) for row in rows]

    def profile(self, slug: str) -> Profile | None:
        with self.lock:
            row = self.db.execute("SELECT config FROM profiles WHERE slug=?", (slug,)).fetchone()
        return Profile.model_validate_json(row[0]) if row else None

    def save_profile(self, profile: Profile) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO profiles VALUES (?, ?) ON CONFLICT(slug) DO UPDATE SET config=excluded.config",
                            (profile.slug, profile.model_dump_json()))

    def delete_profile(self, slug: str) -> bool:
        with self.lock, self.db:
            self.db.execute("DELETE FROM api_keys WHERE profile=?", (slug,))
            self.db.execute("DELETE FROM items WHERE profile=?", (slug,))
            self.db.execute("DELETE FROM downloads WHERE profile=?", (slug,))
            self.db.execute("DELETE FROM media_paths WHERE profile=?", (slug,))
            return self.db.execute("DELETE FROM profiles WHERE slug=?", (slug,)).rowcount > 0

    @staticmethod
    def _output_dir(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("client output directory must be a path string")
        if not value.strip():
            return None
        if "\0" in value or not Path(value).is_absolute():
            raise ValueError("client output directory must be an absolute path")
        return str(Path(value).resolve())

    def add_key(self, profile: str, label: str, output_dir: str | None = None) -> tuple[str, str]:
        if not self.profile(profile):
            raise ValueError("unknown profile")
        if not label.strip() or len(label) > 128:
            raise ValueError("key label must contain 1-128 characters")
        output_dir = self._output_dir(output_dir)
        key_id = secrets.token_hex(16)
        secret = secrets.token_urlsafe(32)
        digest = hashlib.sha256(secret.encode()).hexdigest()
        with self.lock, self.db:
            self.db.execute("INSERT INTO api_keys (id, profile, label, digest, created, revoked, output_dir) "
                            "VALUES (?, ?, ?, ?, ?, 0, ?)",
                            (key_id, profile, label.strip(), digest, time.time(), output_dir))
        return key_id, secret

    def keys(self, profile: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT id, label, created, revoked, output_dir FROM api_keys "
                                   "WHERE profile=? ORDER BY created DESC", (profile,)).fetchall()
        return [dict(row) for row in rows]

    def key_output_dir(self, profile: str, key_id: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT output_dir FROM api_keys WHERE profile=? AND id=?",
                                  (profile, key_id)).fetchone()
        return row[0] if row else None

    def set_key_output_dir(self, profile: str, key_id: str, output_dir: str | None) -> bool:
        output_dir = self._output_dir(output_dir)
        with self.lock, self.db:
            return self.db.execute("UPDATE api_keys SET output_dir=? WHERE profile=? AND id=? AND revoked=0",
                                   (output_dir, profile, key_id)).rowcount > 0

    def reserve_output_path(self, profile: str, key_id: str, item_id: str,
                            directory: Path, filename: str) -> Path:
        directory = directory.resolve()
        with self.lock, self.db:
            row = self.db.execute("SELECT path FROM media_paths WHERE profile=? AND key_id=? AND id=?",
                                  (profile, key_id, item_id)).fetchone()
            if row and Path(row[0]).parent == directory:
                return Path(row[0])
            if row:
                self.db.execute("DELETE FROM media_paths WHERE profile=? AND key_id=? AND id=?",
                                (profile, key_id, item_id))
            stem, suffix = Path(filename).stem, Path(filename).suffix
            index = 1
            while True:
                name = filename if index == 1 else f"{stem} ({index}){suffix}"
                candidate = directory / name
                reserved = self.db.execute("SELECT 1 FROM media_paths WHERE path=?", (str(candidate),)).fetchone()
                if not reserved and not candidate.exists():
                    self.db.execute("INSERT INTO media_paths VALUES (?, ?, ?, ?)",
                                    (profile, key_id, item_id, str(candidate)))
                    return candidate
                index += 1

    def release_output_path(self, profile: str, key_id: str, item_id: str, path: Path) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM media_paths WHERE profile=? AND key_id=? AND id=? AND path=?",
                            (profile, key_id, item_id, str(path)))

    def authenticate_key(self, profile: str, secret: str) -> str | None:
        digest = hashlib.sha256(secret.encode()).hexdigest()
        with self.lock:
            row = self.db.execute("SELECT id, digest FROM api_keys WHERE profile=? AND digest=? AND revoked=0",
                                  (profile, digest)).fetchone()
        return row["id"] if row and secrets.compare_digest(digest, row["digest"]) else None

    def revoke_key(self, profile: str, key_id: str) -> bool:
        with self.lock, self.db:
            return self.db.execute("UPDATE api_keys SET revoked=1 WHERE profile=? AND id=?", (profile, key_id)).rowcount > 0

    def key_active(self, profile: str, key_id: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM api_keys WHERE profile=? AND id=? AND revoked=0",
                                   (profile, key_id)).fetchone() is not None

    def put_item(self, profile: str, item: dict) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO items VALUES (?, ?, ?, ?) ON CONFLICT(profile,id) DO UPDATE SET data=excluded.data, created=excluded.created",
                            (profile, item["id"], _json(item), time.time()))

    def item(self, profile: str, item_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT data FROM items WHERE profile=? AND id=?", (profile, item_id)).fetchone()
        return json.loads(row[0]) if row else None

    def put_download(self, profile: str, key_id: str, state: dict) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO downloads VALUES (?, ?, ?, ?) ON CONFLICT(profile,key_id,id) DO UPDATE SET data=excluded.data",
                            (profile, key_id, state["id"], _json(state)))

    def download(self, profile: str, key_id: str, item_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT data FROM downloads WHERE profile=? AND key_id=? AND id=?",
                                  (profile, key_id, item_id)).fetchone()
        return json.loads(row[0]) if row else None

    def downloads(self, profile: str, key_id: str, since: float | None = None) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT data FROM downloads WHERE profile=? AND key_id=?", (profile, key_id)).fetchall()
        states = [json.loads(row[0]) for row in rows]
        return [state for state in states if since is None or state["timestamp"] >= since]

    def setting(self, name: str, default: str | None = None) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return row[0] if row else default

    def set_setting(self, name: str, value: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))

    def create_session(self) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            self.db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (digest, csrf, time.time() + 8 * 3600))
        return token, csrf

    def session(self, token: str) -> str | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            row = self.db.execute("SELECT csrf FROM sessions WHERE digest=? AND expires>?", (digest, time.time())).fetchone()
        return row[0] if row else None

    def delete_session(self, token: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE digest=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def delete_all_sessions(self) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions")
