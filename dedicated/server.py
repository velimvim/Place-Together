#!/usr/bin/env python3
"""
PlaceTogether Dedicated Co-op Server (v3.3 - SQLite & Web Dashboard Edition)
Cross-platform 24/7 synchronization server for Roblox Studio collaborative building.

Features:
- High-Performance SQLite Backend: ACID transactions, Write-Ahead Logging (WAL), zero data corruption.
- Clean Console Output: Standard HTTP access logs are routed to 'server.log'; console only displays important events.
- Live Web Dashboard (/): Real-time web UI showing active projects and connected developers with their Roblox avatars.
- Roblox Avatar Integration: Tracks developer Roblox UserIds to display visual avatars in both Web Dashboard and Studio Plugin.
- Completely host-independent: runs 24/7 on remote Linux VPS, Windows Server, or local PC.
- Persistent rooms: projects are saved permanently to SQLite disk; players can leave and resume anytime.
- Room-level security: optional room passwords with salted SHA-256 hashing.
- Token-based authorization: verified session tokens for sync, state, and presence.
- Chunked synchronization: enables large map sync without HttpService payload limits.
- Delta revisions (?since=rev): fast incremental updates (< 1ms).
- Graceful shutdown: automatic WAL checkpoint on SIGINT/SIGTERM.
- Zero external dependencies: 100% Python standard library.
"""

import sys
import os
import json
import time
import argparse
import hashlib
import secrets
import signal
import threading
import sqlite3
import socket
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

# In-memory Roblox Avatar CDN cache (userId -> cdn_image_url)
ROBLOX_AVATAR_CACHE = {}
DEFAULT_AVATAR_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="#64748b">'
    '<circle cx="12" cy="8" r="4"/>'
    '<path d="M4 20c0-4 4-6 8-6s8 2 8 6"/>'
    '</svg>'
).encode("utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8787
DEFAULT_DB_FILE = os.path.join(BASE_DIR, "placetogether.db")
DEFAULT_LOG_FILE = os.path.join(BASE_DIR, "server.log")
LEGACY_DATA_FILE = os.path.join(BASE_DIR, "data.json")
MAX_PAYLOAD_SIZE = 30 * 1024 * 1024  # 30 MB maximum request body
HISTORY_BUFFER_SIZE = 500             # Retain last 500 delta batches per room

# Ephemeral in-memory state
state_lock = threading.RLock()
presences = {}          # room_id -> { token -> peer_data }
last_logged_peers = {}  # room_id -> set of active user names
invitations = []        # list of invite dicts
next_invite_id = 1
failed_attempts = {}    # ip -> [failed_count, last_timestamp]
is_shutting_down = False
db = None
access_logger = None
SERVER_START_TIME = time.time()

def console_log(tag: str, msg: str):
    """Outputs clean, formatted, high-priority messages to the console."""
    timestamp = time.strftime('%H:%M:%S')
    print(f"[{timestamp}] [{tag.upper():^7}] {msg}", flush=True)

class AccessLogger:
    """Thread-safe access logger that records raw HTTP requests into server.log."""
    def __init__(self, log_path: str):
        self.log_path = os.path.abspath(log_path)
        self.lock = threading.Lock()

    def log(self, ip: str, method: str, path: str, status: int, extra: str = ""):
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        line = f"[{timestamp}] {ip} - {method} {path} HTTP/{status} {extra}\n"
        with self.lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass

def safe_int(val, default=0) -> int:
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default

def hash_password(password: str, salt: str = None) -> tuple:
    if not password:
        return "", ""
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return hashed, salt

def verify_password(password: str, hashed: str, salt: str) -> bool:
    if not hashed:
        return True
    if not password:
        return False
    check_hash, _ = hash_password(password, salt)
    return secrets.compare_digest(check_hash, hashed)

class Database:
    """Thread-safe SQLite database manager with WAL mode and atomic transactions."""
    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, timeout=20.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute("PRAGMA journal_mode = WAL;")
            self.conn.execute("PRAGMA synchronous = NORMAL;")
            self.conn.execute("PRAGMA foreign_keys = ON;")
            self.conn.execute("PRAGMA busy_timeout = 5000;")
            self._create_tables()

    def _create_tables(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                password_hash TEXT DEFAULT '',
                salt TEXT DEFAULT '',
                owner TEXT DEFAULT '',
                revision INTEGER DEFAULT 1,
                created_at REAL,
                updated_at REAL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS objects (
                room_id TEXT NOT NULL,
                object_key TEXT NOT NULL,
                version INTEGER DEFAULT 1,
                class_name TEXT DEFAULT '',
                parent_key TEXT DEFAULT '',
                data_json TEXT NOT NULL,
                updated_at REAL,
                PRIMARY KEY (room_id, object_key),
                FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_objects_room ON objects(room_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_objects_class ON objects(room_id, class_name);")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                diff_json TEXT NOT NULL,
                created_at REAL,
                UNIQUE (room_id, revision),
                FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_history_room_rev ON history(room_id, revision);")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS guests (
                token TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                user_id INTEGER DEFAULT 0,
                device_key TEXT DEFAULT '',
                last_seen REAL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS room_tokens (
                token TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                user_name TEXT NOT NULL,
                user_id INTEGER DEFAULT 0,
                created_at REAL,
                FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS room_members (
                room_id TEXT NOT NULL,
                user_name TEXT NOT NULL,
                user_id INTEGER DEFAULT 0,
                joined_at REAL,
                PRIMARY KEY (room_id, user_name),
                FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS server_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL
            );
        """)

        # Seamlessly migrate existing tables if columns are missing
        self._migrate_columns()

    def get_config(self, key: str, default: str = "") -> str:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT value FROM server_config WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else default

    def set_config(self, key: str, value: str):
        with self.lock:
            with self.conn:
                self.conn.execute("""
                    INSERT INTO server_config (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                """, (key, str(value), time.time()))

    def is_dashboard_enabled(self) -> bool:
        val = self.get_config("web_dashboard_enabled", "0")
        return val in ("1", "true", "True", "yes", "on")

    def set_dashboard_enabled(self, enabled: bool):
        self.set_config("web_dashboard_enabled", "1" if enabled else "0")

    def _migrate_columns(self):
        """Automatically adds newly added columns to existing tables if missing."""
        tables_to_check = {
            "guests": [("user_id", "INTEGER DEFAULT 0")],
            "room_tokens": [("user_id", "INTEGER DEFAULT 0")],
            "room_members": [("user_id", "INTEGER DEFAULT 0")]
        }
        cur = self.conn.cursor()
        for tbl, cols in tables_to_check.items():
            try:
                cur.execute(f"PRAGMA table_info({tbl})")
                existing = [r["name"] for r in cur.fetchall()]
                for col_name, col_def in cols:
                    if col_name not in existing:
                        self.conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col_name} {col_def};")
            except Exception:
                pass

    def migrate_from_json_if_needed(self, json_path: str):
        if not os.path.exists(json_path):
            return

        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*) FROM rooms")
            if cur.fetchone()[0] > 0:
                return

            console_log("MIGRATE", f"Found legacy '{json_path}'. Migrating projects to SQLite...")
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                loaded_rooms = data.get("rooms", {})
                loaded_guests = data.get("guests", {})
                total_objs = 0

                with self.conn:
                    for token, g in loaded_guests.items():
                        cur.execute("""
                            INSERT OR REPLACE INTO guests (token, name, user_id, device_key, last_seen)
                            VALUES (?, ?, ?, ?, ?)
                        """, (token, g.get("name", "Guest"), safe_int(g.get("userId"), 0), g.get("deviceKey", ""), g.get("last_seen", time.time())))

                    for r_id, r in loaded_rooms.items():
                        c_at = r.get("created_at", time.time())
                        cur.execute("""
                            INSERT OR REPLACE INTO rooms (id, name, password_hash, salt, owner, revision, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            r_id,
                            r.get("name", "Project"),
                            r.get("password_hash", ""),
                            r.get("salt", ""),
                            r.get("owner", ""),
                            r.get("revision", 1),
                            c_at,
                            time.time()
                        ))

                        owner_token = r.get("owner", "")
                        owner_name = loaded_guests.get(owner_token, {}).get("name", "Owner")
                        cur.execute("INSERT OR IGNORE INTO room_members (room_id, user_name, user_id, joined_at) VALUES (?, ?, 0, ?)", (r_id, owner_name, c_at))

                        for obj_id, rec in r.get("objects", {}).items():
                            val = rec.get("value") if isinstance(rec, dict) and "value" in rec else rec
                            ver = rec.get("version", 1) if isinstance(rec, dict) and "version" in rec else 1
                            full_rec = {"version": ver, "value": val}
                            class_name = val.get("class", "") if isinstance(val, dict) else ""
                            parent_key = val.get("parent", "") if isinstance(val, dict) else ""
                            cur.execute("""
                                INSERT OR REPLACE INTO objects (room_id, object_key, version, class_name, parent_key, data_json, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                            """, (r_id, obj_id, ver, class_name, parent_key, json.dumps(full_rec, ensure_ascii=False), time.time()))
                            total_objs += 1

                console_log("MIGRATE", f"SUCCESS! Migrated {len(loaded_rooms)} room(s) and {total_objs} object(s) into SQLite.")
                bak_path = json_path + ".bak"
                try:
                    if not os.path.exists(bak_path):
                        os.rename(json_path, bak_path)
                        console_log("MIGRATE", f"Archived original '{json_path}' as '{bak_path}'.")
                except Exception:
                    pass
            except Exception as e:
                console_log("ERROR", f"Failed to migrate legacy data: {e}")

    def get_stats(self) -> dict:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*) FROM rooms")
            total_rooms = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM objects")
            total_objects = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM guests")
            total_guests = cur.fetchone()[0]
            return {
                "total_rooms": total_rooms,
                "total_objects": total_objects,
                "total_guests": total_guests
            }

    def get_rooms_list(self) -> list:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT r.id, r.name, r.password_hash, r.revision, r.created_at,
                       COUNT(o.object_key) as objects_count,
                       (SELECT COUNT(*) FROM room_members m WHERE m.room_id = r.id) as members_count
                FROM rooms r
                LEFT JOIN objects o ON r.id = o.room_id
                GROUP BY r.id
                ORDER BY r.created_at DESC
            """)
            rows = cur.fetchall()
            res = []
            now = time.time()
            for row in rows:
                r_id = row["id"]
                with state_lock:
                    room_p = presences.get(r_id, {})
                    active_peers = [p for p in room_p.values() if now - p.get("time", 0) < 15]
                res.append({
                    "id": r_id,
                    "name": row["name"],
                    "requires_password": bool(row["password_hash"]),
                    "objects_count": row["objects_count"],
                    "revision": row["revision"],
                    "members_count": max(1, row["members_count"]),
                    "active_peers": len(active_peers),
                    "created_at": row["created_at"] or 0
                })
            return res

    def get_room(self, room_id: str) -> dict or None:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM rooms WHERE id = ?", (room_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def find_room_by_id_or_name(self, query: str) -> dict or None:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM rooms WHERE id = ?", (query,))
            row = cur.fetchone()
            if row:
                return dict(row)
            cur.execute("SELECT * FROM rooms WHERE LOWER(name) = LOWER(?)", (query,))
            row = cur.fetchone()
            return dict(row) if row else None

    def create_room(self, room_id: str, name: str, pw_hash: str, salt: str, owner_token: str, owner_name: str, user_id: int = 0) -> dict:
        with self.lock:
            now = time.time()
            with self.conn:
                self.conn.execute("""
                    INSERT INTO rooms (id, name, password_hash, salt, owner, revision, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """, (room_id, name, pw_hash, salt, owner_token, now, now))
                self.conn.execute("""
                    INSERT OR IGNORE INTO room_members (room_id, user_name, user_id, joined_at)
                    VALUES (?, ?, ?, ?)
                """, (room_id, owner_name, user_id, now))
            return {
                "id": room_id,
                "name": name,
                "requires_password": bool(pw_hash)
            }

    def delete_room(self, room_id: str, password: str = "", token: str = "") -> tuple:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM rooms WHERE id = ?", (room_id,))
            row = cur.fetchone()
            if not row:
                return False, "Room not found on server"

            room = dict(row)
            pw_hash = room.get("password_hash", "")
            salt = room.get("salt", "")

            # Password check if room is password-protected
            if pw_hash:
                if not password:
                    return False, "Password required to delete this room"
                if not verify_password(password, pw_hash, salt):
                    return False, "Incorrect room password"

            with self.conn:
                self.conn.execute("DELETE FROM objects WHERE room_id = ?", (room_id,))
                self.conn.execute("DELETE FROM history WHERE room_id = ?", (room_id,))
                self.conn.execute("DELETE FROM room_tokens WHERE room_id = ?", (room_id,))
                self.conn.execute("DELETE FROM room_members WHERE room_id = ?", (room_id,))
                self.conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))

            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                self.conn.execute("VACUUM;")
            except Exception as e:
                logger.warning(f"Failed to vacuum database after room deletion: {e}")

            return True, room["name"]

    def is_authorized(self, room_id: str, token: str) -> bool:
        if not room_id or not token:
            return False
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT password_hash, owner FROM rooms WHERE id = ?", (room_id,))
            room = cur.fetchone()
            if not room:
                return False
            if not room["password_hash"]:
                return True
            if room["owner"] and room["owner"] == token:
                return True
            cur.execute("SELECT room_id FROM room_tokens WHERE token = ?", (token,))
            tok = cur.fetchone()
            if tok and tok["room_id"] == room_id:
                return True
            return False

    def create_room_token(self, token: str, room_id: str, user_name: str, user_id: int = 0):
        with self.lock:
            with self.conn:
                self.conn.execute("""
                    INSERT OR REPLACE INTO room_tokens (token, room_id, user_name, user_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (token, room_id, user_name, user_id, time.time()))
                self.conn.execute("""
                    INSERT OR IGNORE INTO room_members (room_id, user_name, user_id, joined_at)
                    VALUES (?, ?, ?, ?)
                """, (room_id, user_name, user_id, time.time()))

    def register_guest(self, token: str, name: str, device_key: str, user_id: int = 0):
        with self.lock:
            with self.conn:
                self.conn.execute("""
                    INSERT OR REPLACE INTO guests (token, name, user_id, device_key, last_seen)
                    VALUES (?, ?, ?, ?, ?)
                """, (token, name, user_id, device_key, time.time()))

    def get_guest(self, token: str) -> dict or None:
        if not token:
            return None
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM guests WHERE token = ?", (token,))
            row = cur.fetchone()
            if row:
                return dict(row)
            # Fallback to room_tokens if token was a session token generated upon room join/create
            cur.execute("SELECT user_name, user_id FROM room_tokens WHERE token = ?", (token,))
            row = cur.fetchone()
            if row:
                return {
                    "token": token,
                    "name": row["user_name"],
                    "user_id": row["user_id"] or 0,
                    "device_key": "",
                    "last_seen": time.time()
                }
            return None

    def update_token_user_id(self, token: str, user_id: int):
        if not token or user_id <= 0:
            return
        with self.lock:
            try:
                with self.conn:
                    self.conn.execute("UPDATE room_tokens SET user_id = ? WHERE token = ? AND (user_id IS NULL OR user_id = 0)", (user_id, token))
                    self.conn.execute("UPDATE guests SET user_id = ? WHERE token = ? AND (user_id IS NULL OR user_id = 0)", (user_id, token))
            except Exception:
                pass

    def get_room_objects(self, room_id: str, page: int = 0, limit: int = 0) -> tuple:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*) FROM objects WHERE room_id = ?", (room_id,))
            total = cur.fetchone()[0]

            if limit > 0:
                offset = max(0, (page - 1) * limit) if page > 0 else 0
                cur.execute("""
                    SELECT object_key, data_json FROM objects
                    WHERE room_id = ?
                    ORDER BY object_key ASC
                    LIMIT ? OFFSET ?
                """, (room_id, limit, offset))
                rows = cur.fetchall()
                chunk = {}
                for r in rows:
                    chunk[r["object_key"]] = json.loads(r["data_json"])
                has_more = (offset + limit) < total
                return chunk, total, has_more
            else:
                cur.execute("SELECT object_key, data_json FROM objects WHERE room_id = ?", (room_id,))
                rows = cur.fetchall()
                all_objs = {}
                for r in rows:
                    all_objs[r["object_key"]] = json.loads(r["data_json"])
                return all_objs, total, False

    def get_room_delta(self, room_id: str, since_rev: int) -> dict or None:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT revision FROM rooms WHERE id = ?", (room_id,))
            r = cur.fetchone()
            if not r:
                return None
            cur_rev = r["revision"]

            if since_rev >= cur_rev:
                return {}

            cur.execute("""
                SELECT revision, diff_json FROM history
                WHERE room_id = ? AND revision > ?
                ORDER BY revision ASC
            """, (room_id, since_rev))
            rows = cur.fetchall()
            if not rows or rows[0]["revision"] > since_rev + 1:
                return None

            deltas = {}
            for row in rows:
                diff = json.loads(row["diff_json"])
                deltas.update(diff)
            return deltas

    def apply_room_changes(self, room_id: str, changes: list, since_rev: int) -> tuple:
        with self.lock:
            with self.conn:
                cur = self.conn.cursor()
                cur.execute("SELECT revision, name, owner FROM rooms WHERE id = ?", (room_id,))
                room_row = cur.fetchone()
                if not room_row:
                    return None, "Room not found on server"

                current_rev = room_row["revision"]
                room_name = room_row["name"]
                new_rev = current_rev
                diff = {}
                now = time.time()

                for ch in changes:
                    obj_id = ch.get("id")
                    if not obj_id:
                        continue
                    if ch.get("deleted"):
                        cur.execute("DELETE FROM objects WHERE room_id = ? AND object_key = ?", (room_id, obj_id))
                        diff[obj_id] = {"version": current_rev + 1, "value": None}
                    else:
                        val = ch.get("value")
                        cur.execute("SELECT version FROM objects WHERE room_id = ? AND object_key = ?", (room_id, obj_id))
                        existing = cur.fetchone()
                        ver = (existing["version"] + 1) if existing else 1
                        rec = {"version": ver, "value": val}
                        class_name = val.get("class", "") if isinstance(val, dict) else ""
                        parent_key = val.get("parent", "") if isinstance(val, dict) else ""
                        data_json = json.dumps(rec, ensure_ascii=False)

                        cur.execute("""
                            INSERT INTO objects (room_id, object_key, version, class_name, parent_key, data_json, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(room_id, object_key) DO UPDATE SET
                                version = excluded.version,
                                class_name = excluded.class_name,
                                parent_key = excluded.parent_key,
                                data_json = excluded.data_json,
                                updated_at = excluded.updated_at
                        """, (room_id, obj_id, ver, class_name, parent_key, data_json, now))
                        diff[obj_id] = rec

                if diff:
                    new_rev = current_rev + 1
                    cur.execute("UPDATE rooms SET revision = ?, updated_at = ? WHERE id = ?", (new_rev, now, room_id))
                    cur.execute("""
                        INSERT INTO history (room_id, revision, diff_json, created_at)
                        VALUES (?, ?, ?, ?)
                    """, (room_id, new_rev, json.dumps(diff, ensure_ascii=False), now))
                    cur.execute("""
                        DELETE FROM history WHERE room_id = ? AND revision <= ?
                    """, (room_id, new_rev - HISTORY_BUFFER_SIZE))

                cur.execute("SELECT COUNT(*) FROM objects WHERE room_id = ?", (room_id,))
                total_objs = cur.fetchone()[0]

                remote_deltas = {}
                if since_rev > 0 and since_rev < current_rev:
                    cur.execute("""
                        SELECT revision, diff_json FROM history
                        WHERE room_id = ? AND revision > ? AND revision <= ?
                        ORDER BY revision ASC
                    """, (room_id, since_rev, current_rev))
                    for h in cur.fetchall():
                        h_diff = json.loads(h["diff_json"])
                        remote_deltas.update(h_diff)

                return {
                    "revision": new_rev,
                    "applied": True,
                    "total_objects": total_objs,
                    "remoteChanges": remote_deltas,
                    "diff_count": len(diff),
                    "room_name": room_name
                }, None

    def close(self):
        with self.lock:
            try:
                cur = self.conn.cursor()
                cur.execute("PRAGMA freelist_count;")
                row = cur.fetchone()
                if row and row[0] > 0:
                    self.conn.execute("VACUUM;")
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                self.conn.close()
            except Exception:
                pass


# HTML Web Dashboard Template (Dark Mode, Live Auto-Refresh, Roblox Avatars)
HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PlaceTogether Dedicated Server</title>
<style>
  :root {
    --bg: #0f1117;
    --card: #171b24;
    --card-border: #262c3a;
    --text: #f8fafc;
    --muted: #94a3b8;
    --subtle: #64748b;
    --accent: #3b82f6;
    --green: #22c55e;
    --green-bg: #143020;
    --danger: #ef4444;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 24px;
    line-height: 1.5;
  }
  .container { max-width: 960px; margin: 0 auto; }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-bottom: 20px;
    border-bottom: 1px solid var(--card-border);
    margin-bottom: 24px;
  }
  .brand { display: flex; align-items: center; gap: 12px; }
  .logo-cube {
    width: 34px; height: 34px; background: linear-gradient(135deg, #3b82f6, #6366f1);
    border-radius: 8px; display: flex; align-items: center; justify-content: center;
    font-weight: 800; font-size: 18px; color: white;
  }
  h1 { font-size: 20px; font-weight: 700; }
  .pulse-badge {
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--green-bg); color: var(--green);
    padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
  }
  .pulse-badge.offline {
    background: #2d161a; color: #f87171; border: 1px solid #782832;
  }
  .dot { width: 8px; height: 8px; background: var(--green); border-radius: 50%; box-shadow: 0 0 8px var(--green); transition: all 0.25s ease; }
  .dot.offline { background: #ef4444; box-shadow: 0 0 8px #ef4444; }
  .offline-banner {
    background: #2d161a; border: 1px solid #782832; color: #fca5a5;
    padding: 10px 16px; border-radius: 8px; margin-bottom: 20px;
    font-size: 13px; font-weight: 600; display: none; align-items: center; gap: 10px;
  }
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 28px; }
  .stat-card {
    background: var(--card); border: 1px solid var(--card-border);
    padding: 16px; border-radius: 10px;
  }
  .stat-label { font-size: 11px; text-transform: uppercase; color: var(--subtle); font-weight: 700; letter-spacing: 0.5px; }
  .stat-val { font-size: 24px; font-weight: 700; margin-top: 4px; color: var(--text); }
  .section-title { font-size: 15px; font-weight: 700; margin-bottom: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .project-card {
    background: var(--card); border: 1px solid var(--card-border);
    border-radius: 12px; padding: 20px; margin-bottom: 20px;
  }
  .project-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
  .project-title { font-size: 18px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
  .pill { font-size: 11px; padding: 2px 8px; border-radius: 6px; background: #262c3a; color: var(--muted); }
  .btn-download {
    display: inline-flex; align-items: center; gap: 6px;
    background: #1e293b; color: #38bdf8; font-weight: 600; font-size: 12px;
    padding: 5px 12px; border-radius: 6px; border: 1px solid #334155;
    text-decoration: none; transition: background 0.15s ease, border-color 0.15s ease;
  }
  .btn-download:hover {
    background: #0284c7; color: #ffffff; border-color: #0284c7;
  }
  .btn-toggle-dash {
    background: #1e293b; color: #94a3b8; font-size: 12px; font-weight: 600;
    padding: 5px 12px; border-radius: 6px; border: 1px solid #334155;
    cursor: pointer; transition: all 0.15s ease;
  }
  .btn-toggle-dash:hover {
    background: #dc2626; color: white; border-color: #dc2626;
  }
  .peers-title { font-size: 12px; font-weight: 700; color: var(--subtle); margin-bottom: 10px; text-transform: uppercase; }
  .peers-list { display: flex; flex-wrap: wrap; gap: 12px; }
  .peer-card {
    display: flex; align-items: center; gap: 10px;
    background: #12151e; border: 1px solid var(--card-border);
    padding: 8px 12px; border-radius: 8px; min-width: 200px;
  }
  .avatar {
    width: 40px; height: 40px; border-radius: 50%;
    background: #232734; border: 2px solid #3b82f6; object-fit: cover;
  }
  .peer-info { display: flex; flex-direction: column; }
  .peer-name { font-weight: 700; font-size: 13px; color: var(--text); text-decoration: none; }
  .peer-name:hover { text-decoration: underline; color: var(--accent); }
  .peer-status { font-size: 11px; color: var(--green); display: flex; align-items: center; gap: 4px; }
  .empty-peers { color: var(--subtle); font-size: 13px; font-style: italic; padding: 8px 0; }
  footer {
    margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--card-border);
    font-size: 12px; color: var(--subtle); display: flex; justify-content: space-between;
  }
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="brand">
      <div class="logo-cube">P</div>
      <div>
        <h1>PlaceTogether Dedicated Server</h1>
        <div style="font-size: 12px; color: var(--subtle);">SQLite WAL Edition v3.4</div>
      </div>
    </div>
    <div style="display: flex; align-items: center; gap: 10px;">
      <a href="/PlaceTogether.rbxmx" download class="btn-download">Download Plugin (.rbxmx)</a>
      <button onclick="disableDashboard()" class="btn-toggle-dash" title="Turn off Web Dashboard">Disable Dashboard</button>
      <div class="pulse-badge" id="server-status-badge"><span class="dot" id="server-status-dot"></span> <span id="server-status-text">Online & Syncing</span></div>
    </div>
  </header>

  <div id="offline-banner" class="offline-banner">
    <span style="font-size: 16px;">⚠️</span>
    <span>Server is offline or unreachable. "Online & Syncing" paused. Automatically attempting reconnection...</span>
  </div>

  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">Active Developers</div>
      <div class="stat-val" id="stat-peers">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total Projects</div>
      <div class="stat-val" id="stat-rooms">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total Map Objects</div>
      <div class="stat-val" id="stat-objects">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Uptime</div>
      <div class="stat-val" id="stat-uptime">0s</div>
    </div>
  </div>

  <div class="section-title">Projects & Connected Developers</div>
  <div id="projects-container">
    <div style="color: var(--muted); font-size: 14px;">Loading project status...</div>
  </div>

  <footer>
    <div>Access logs redirected to <code>server.log</code> &bull; Console filtered to high-priority events</div>
    <div>Auto-refreshes every 3s</div>
  </footer>
</div>

<script>
let lastRenderedHtml = '';

async function refreshDashboard() {
  const badge = document.getElementById('server-status-badge');
  const badgeText = document.getElementById('server-status-text');
  const badgeDot = document.getElementById('server-status-dot');
  const banner = document.getElementById('offline-banner');
  const container = document.getElementById('projects-container');

  function setOffline() {
    if (badge) badge.className = 'pulse-badge offline';
    if (badgeDot) badgeDot.className = 'dot offline';
    if (badgeText) badgeText.textContent = 'Server Offline';
    if (banner) banner.style.display = 'flex';
    if (container) container.style.opacity = '0.55';
    const uptimeEl = document.getElementById('stat-uptime');
    if (uptimeEl) uptimeEl.textContent = 'Offline';
  }

  function setOnline() {
    if (badge) badge.className = 'pulse-badge';
    if (badgeDot) badgeDot.className = 'dot';
    if (badgeText) badgeText.textContent = 'Online & Syncing';
    if (banner) banner.style.display = 'none';
    if (container) container.style.opacity = '1';
  }
  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 2500);
    const res = await fetch('/api/status', {
      headers: { 'Accept': 'application/json' },
      signal: controller.signal
    });
    clearTimeout(timeoutId);
    if (!res.ok) {
      setOffline();
      return;
    }
    const data = await res.json();
    setOnline();


    document.getElementById('stat-peers').textContent = data.active_peers;
    document.getElementById('stat-rooms').textContent = data.rooms_count;
    document.getElementById('stat-objects').textContent = data.total_objects;

    const uptime = data.uptime_seconds;
    const h = Math.floor(uptime / 3600);
    const m = Math.floor((uptime % 3600) / 60);
    const s = uptime % 60;
    document.getElementById('stat-uptime').textContent = (h > 0 ? h + 'h ' : '') + m + 'm ' + s + 's';

    const container = document.getElementById('projects-container');
    if (!data.rooms || data.rooms.length === 0) {
      const emptyHtml = '<div class="project-card"><div style="color: var(--subtle);">No projects created on server yet. Create a project from Roblox Studio PlaceTogether plugin.</div></div>';
      if (lastRenderedHtml !== emptyHtml) {
        container.innerHTML = emptyHtml;
        lastRenderedHtml = emptyHtml;
      }
      return;
    }

    let html = '';
    for (const r of data.rooms) {
      const peers = r.peers || [];
      html += `
        <div class="project-card">
          <div class="project-header">
            <div class="project-title">
              ${escapeHtml(r.name)}
              <span class="pill">ID: ${escapeHtml(r.id)}</span>
              <span class="pill">Rev ${r.revision}</span>
              <span class="pill">${r.objects_count} objects</span>
              ${r.requires_password ? '<span class="pill" style="color: #f59e0b;">Password Protected</span>' : ''}
            </div>
            <div style="display: flex; align-items: center; gap: 10px;">
              <div style="font-size: 12px; color: var(--muted);">${peers.length} active in Studio</div>
              <button onclick="deleteRoom('${escapeHtml(r.id)}', '${escapeHtml(r.name)}', ${r.requires_password})" style="background:#2d161a;color:#fca5a5;border:1px solid #782832;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;">Delete</button>
            </div>
          </div>
          <div class="peers-title">Developers Editing Now:</div>
          <div class="peers-list">
      `;

      if (peers.length === 0) {
        html += '<div class="empty-peers">No developers currently active in Studio. Changes are safely saved in SQLite.</div>';
      } else {
        for (const p of peers) {
          const avatarUrl = `/api/avatar?userId=${p.userId || 0}`;
          const profileUrl = (p.userId && p.userId > 0)
            ? `https://www.roblox.com/users/${p.userId}/profile`
            : '#';

          const hueColor = `hsl(${Math.round(p.hue * 360)}, 75%, 60%)`;
          const isIdle = p.isIdle === true;
          let actText = p.activity || (p.selected_count && p.selected_count > 0 ? `Selected ${p.selected_count} part(s)` : 'Viewing place');
          if (isIdle && !actText.includes('Idle (')) {
            const timeStr = formatDur(p.idleTime || 60);
            actText = (actText === 'Viewing place') ? `Idle (${timeStr})` : `Idle (${timeStr}) · ${actText}`;
          }
          const dotStyle = isIdle
            ? 'background: #eab308; box-shadow: 0 0 6px #eab308;'
            : 'background: var(--green); box-shadow: 0 0 6px var(--green);';
          const actStatusColor = isIdle ? 'color: #eab308;' : 'color: var(--green);';

          html += `
            <div class="peer-card">
              <img src="${avatarUrl}" class="avatar" style="border-color: ${hueColor};" alt="${escapeHtml(p.name)}" title="${escapeHtml(p.name)} (Roblox ID: ${p.userId || 'N/A'})">
              <div class="peer-info">
                <a href="${profileUrl}" target="_blank" rel="noopener" class="peer-name">${escapeHtml(p.name)}</a>
                <div class="peer-status" style="${actStatusColor}"><span class="dot" style="${dotStyle}"></span> ${escapeHtml(actText)}</div>
              </div>
            </div>
          `;
        }
      }

      html += `
          </div>
        </div>
      `;
    }

    if (html !== lastRenderedHtml) {
      container.innerHTML = html;
      lastRenderedHtml = html;
    }
  } catch (err) {
    setOffline();
    console.warn('Dashboard fetch failed (server offline):', err);
  }
}

async function deleteRoom(id, name, requiresPassword) {
  let pw = '';
  if (requiresPassword) {
    pw = prompt(`Project '${name}' is password-protected.\nEnter room password to confirm permanent deletion:`);
    if (pw === null) return;
    if (!pw.trim()) { alert('Password is required to delete this project.'); return; }
  } else {
    if (!confirm(`Are you sure you want to permanently delete project '${name}' (${id})?`)) return;
  }
  try {
    const res = await fetch(`/api/rooms/${id}/delete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw })
    });
    const data = await res.json();
    setOnline();

    if (res.ok) {
      refreshDashboard();
    } else {
      alert('Delete failed: ' + (data.error || 'Server error'));
    }
  } catch (err) {
    alert('Request failed: ' + err.message);
  }
}

function formatDur(s) {
  s = Math.max(0, Math.floor(s || 0));
  if (s < 60) return s + 's';
  if (s < 3600) {
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return (rem > 0 && m < 10) ? (m + 'm ' + rem + 's') : (m + 'm');
  }
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return m > 0 ? (h + 'h ' + m + 'm') : (h + 'h');
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function disableDashboard() {
  if (!confirm('Turn off the Web Dashboard? Studio synchronization will continue unaffected. You can re-enable the dashboard from the Roblox Studio plugin at any time.')) return;
  try {
    const res = await fetch('/api/config/dashboard', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: false })
    });
    if (res.ok) {
      window.location.reload();
    }
  } catch (e) {
    alert('Failed to disable dashboard: ' + e);
  }
}

refreshDashboard();
setInterval(refreshDashboard, 3000);
</script>
</body>
</html>
"""

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc_type, exc_val, _ = sys.exc_info()
        if exc_type:
            if issubclass(exc_type, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
                return
            if issubclass(exc_type, OSError) and getattr(exc_val, 'winerror', None) in (10054, 10053):
                return
            if issubclass(exc_type, OSError) and getattr(exc_val, 'errno', None) in (10054, 10053):
                return
        super().handle_error(request, client_address)

class PlaceTogetherHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
            self.close_connection = True

    def finish(self):
        try:
            super().finish()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
            pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
            self.close_connection = True
        except Exception as e:
            console_log("ERROR", f"Request exception: {e}")
            self.close_connection = True

    def log_message(self, format, *args):
        # Route ALL raw HTTP access logs to server.log file, NOT to console!
        try:
            msg = format % args
            if access_logger:
                access_logger.log(self.client_address[0], self.command, self.path, getattr(self, '_response_status', 200), msg)
        except Exception:
            pass

    def send_response(self, code, message=None):
        self._response_status = code
        super().send_response(code, message)

    def send_json(self, status_code: int, data: dict or list):
        try:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Room-Token, bypass-tunnel-reminder")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
            self.close_connection = True

    def send_html(self, status_code: int, html_str: str):
        try:
            body = html_str.encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
            self.close_connection = True

    def drop_connection(self):
        """Immediately closes TCP connection without sending HTTP headers or body,
        causing browsers to display ERR_EMPTY_RESPONSE."""
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.connection.close()
        except Exception:
            pass

    def handle_avatar_request(self, uid: int):
        """Proxies or redirects Roblox avatar headshots to eliminate CORS and render cleanly."""
        if uid <= 0:
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(DEFAULT_AVATAR_SVG)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(DEFAULT_AVATAR_SVG)
            except Exception:
                pass
            return

        cdn_url = ROBLOX_AVATAR_CACHE.get(uid)
        if not cdn_url:
            try:
                req_url = f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={uid}&size=150x150&format=Png&isCircular=false"
                req = urllib.request.Request(req_url, headers={"User-Agent": "PlaceTogether-Dedicated-Server/3.3"})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        items = data.get("data", [])
                        if items and items[0].get("imageUrl"):
                            cdn_url = items[0]["imageUrl"]
                            ROBLOX_AVATAR_CACHE[uid] = cdn_url
            except Exception:
                pass

        if cdn_url:
            self.send_response(302)
            self.send_header("Location", cdn_url)
            self.send_header("Cache-Control", "public, max-age=7200")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(DEFAULT_AVATAR_SVG)))
        self.send_header("Cache-Control", "public, max-age=60")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(DEFAULT_AVATAR_SVG)
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Room-Token, bypass-tunnel-reminder")
        self.end_headers()

    def get_token(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        custom = self.headers.get("X-Room-Token", "")
        if custom:
            return custom.strip()
        return ""

    def read_body(self) -> dict:
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return {}

        if content_length > MAX_PAYLOAD_SIZE:
            self.send_json(413, {"error": f"Payload too large. Limit is {MAX_PAYLOAD_SIZE // (1024*1024)} MB."})
            return None

        if content_length > 0:
            try:
                raw = self.rfile.read(content_length).decode("utf-8")
                return json.loads(raw)
            except Exception as e:
                self.send_json(400, {"error": f"Malformed JSON body: {str(e)}"})
                return None
        return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        query_string = self.path.split("?")[1] if "?" in self.path else ""
        query_params = {}
        if query_string:
            for pair in query_string.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    query_params[k] = v

        token = self.get_token()
        accept_header = self.headers.get("Accept", "")

        # 1. Web Dashboard: GET / or GET /dashboard or GET /status
        if path in ("/", "/dashboard", "/status"):
            if not db.is_dashboard_enabled():
                self.drop_connection()
                return
            if path in ("/", "/dashboard") and ("text/html" in accept_header or "application/json" not in accept_header):
                self.send_html(200, HTML_DASHBOARD)
                return

        # 1b. Config status: GET /api/config/dashboard
        if path == "/api/config/dashboard":
            self.send_json(200, {
                "enabled": db.is_dashboard_enabled()
            })
            return

        # 1c. Avatar proxy / fallback: GET /api/avatar?userId=<uid>
        if path == "/api/avatar":
            if not db.is_dashboard_enabled():
                self.drop_connection()
                return
            uid = safe_int(query_params.get("userId"), 0)
            self.handle_avatar_request(uid)
            return

        # 1c. Plugin download: GET /PlaceTogether.rbxmx or /api/plugin/download
        if path in ("/PlaceTogether.rbxmx", "/api/plugin/download"):
            plugin_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PlaceTogether.rbxmx")
            if os.path.exists(plugin_path):
                try:
                    with open(plugin_path, "rb") as pf:
                        data = pf.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", 'attachment; filename="PlaceTogether.rbxmx"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    pass
                return

        # 2. Status check: /api/status, /, /status
        if path in ("/api/status", "/", "/status", "/dashboard"):
            stats = db.get_stats()
            rooms_list = db.get_rooms_list()
            now = time.time()

            detailed_rooms = []
            total_active_peers = 0
            with state_lock:
                for r in rooms_list:
                    r_id = r["id"]
                    room_pres = presences.get(r_id, {})
                    active_peers = []
                    for p in room_pres.values():
                        if now - p.get("time", 0) < 15:
                            active_peers.append({
                                "name": p.get("name", "User"),
                                "userId": p.get("userId", 0),
                                "hue": p.get("hue", 0.5),
                                "activity": p.get("activity", "Viewing place"),
                                "isIdle": bool(p.get("isIdle", False)),
                                "idleTime": safe_int(p.get("idleTime"), 0),
                                "selected_count": len(p.get("selected", [])),
                                "last_seen_seconds": int(now - p.get("time", 0))
                            })
                    total_active_peers += len(active_peers)
                    detailed_rooms.append({
                        "id": r_id,
                        "name": r["name"],
                        "revision": r["revision"],
                        "objects_count": r["objects_count"],
                        "requires_password": r["requires_password"],
                        "created_at": r["created_at"],
                        "peers": active_peers
                    })

            self.send_json(200, {
                "server": "PlaceTogether-Dedicated-SQLite",
                "version": "3.3.0",
                "status": "online",
                "dedicated": True,
                "database": "SQLite (WAL Mode)",
                "rooms_count": stats["total_rooms"],
                "total_objects": stats["total_objects"],
                "active_peers": total_active_peers,
                "registered_guests": stats["total_guests"],
                "uptime_seconds": int(now - SERVER_START_TIME),
                "rooms": detailed_rooms
            })
            return

        # 3. List Rooms: /api/rooms
        if path == "/api/rooms":
            rooms_list = db.get_rooms_list()
            self.send_json(200, rooms_list)
            return

        # 4. Room Sync GET: /api/rooms/<id>/sync with support for ?since=<rev>&page=<p>&limit=<l>
        if path.startswith("/api/rooms/") and path.endswith("/sync"):
            parts = path.split("/")
            room_id = parts[3]
            room = db.get_room(room_id)
            if not room:
                self.send_json(404, {"error": "Room not found on server"})
                return

            if not db.is_authorized(room_id, token):
                self.send_json(401, {"error": "Unauthorized: Password required or invalid room token"})
                return

            since_rev = int(query_params.get("since", 0))

            if since_rev > 0 and since_rev == room["revision"]:
                self.send_json(200, {
                    "revision": room["revision"],
                    "unchanged": True,
                    "objects": {}
                })
                return

            if since_rev > 0 and since_rev < room["revision"]:
                deltas = db.get_room_delta(room_id, since_rev)
                if deltas is not None:
                    self.send_json(200, {
                        "revision": room["revision"],
                        "unchanged": False,
                        "objects": deltas,
                        "delta": True
                    })
                    return

            page = int(query_params.get("page", 0))
            limit = int(query_params.get("limit", 0))

            with db.lock:
                cur = db.conn.cursor()
                cur.execute("SELECT COUNT(*) FROM objects WHERE room_id = ?", (room_id,))
                total_objs = cur.fetchone()[0]

            # Auto-paginate if no limit specified on initial download of large places (>250 objects)
            # This prevents 10MB+ single payload that Roblox Studio HttpService drops!
            if since_rev == 0 and limit == 0 and total_objs > 250:
                limit = 250
                page = 1

            objs, total, has_more = db.get_room_objects(room_id, page=page, limit=limit)
            if limit > 0:
                self.send_json(200, {
                    "revision": room["revision"],
                    "objects": objs,
                    "page": page if page > 0 else 1,
                    "limit": limit,
                    "total_objects": total,
                    "has_more": has_more,
                    "owner": room["owner"]
                })
                return

            self.send_json(200, {
                "revision": room["revision"],
                "objects": objs,
                "total_objects": total,
                "owner": room["owner"]
            })
            return

        # 5. Room State GET: /api/rooms/<id>/state
        if path.startswith("/api/rooms/") and path.endswith("/state"):
            parts = path.split("/")
            room_id = parts[3]
            room = db.get_room(room_id)
            if not room:
                self.send_json(404, {"error": "Room not found on server"})
                return

            _, total, _ = db.get_room_objects(room_id, page=1, limit=1)
            self.send_json(200, {
                "id": room_id,
                "name": room["name"],
                "requires_password": bool(room["password_hash"]),
                "isOwner": (room["owner"] == token),
                "revision": room["revision"],
                "objects_count": total,
                "persistent": True,
                "database": "SQLite"
            })
            return

        # 6. Invitations inbox
        if path == "/api/invitations":
            guest = db.get_guest(token)
            user_name = guest["name"] if guest else ""
            with state_lock:
                pending = [inv for inv in invitations if inv.get("nick") == user_name and inv.get("state") == "pending"]
            self.send_json(200, pending)
            return

        # 7. Invitations outbox
        if path == "/api/invitations/outbox":
            guest = db.get_guest(token)
            user_name = guest["name"] if guest else ""
            with state_lock:
                my_invites = [inv for inv in invitations if inv.get("from") == user_name]
            self.send_json(200, my_invites)
            return

        if "text/html" in accept_header and not path.startswith("/api/"):
            self.drop_connection()
            return

        self.send_json(404, {"error": f"Endpoint not found: {path}"})

    def do_DELETE(self):
        self.do_POST()

    def do_POST(self):
        path = self.path.split("?")[0]
        data = self.read_body()
        if data is None:
            return
        token = self.get_token()

        # 0. Config / Dashboard toggle: POST /api/config/dashboard or /api/dashboard/toggle
        if path in ("/api/config/dashboard", "/api/dashboard/toggle"):
            new_state = data.get("enabled")
            if new_state is None:
                new_state = not db.is_dashboard_enabled()
            else:
                new_state = bool(new_state)

            db.set_dashboard_enabled(new_state)
            state_str = "ENABLED" if new_state else "DISABLED"
            console_log("CONFIG", f"Web Dashboard {state_str} (saved to SQLite)")
            self.send_json(200, {
                "success": True,
                "enabled": new_state
            })
            return

        # 1. Guest Registration: /api/guest
        if path == "/api/guest":
            name = data.get("name", "Guest").strip()
            user_id = safe_int(data.get("userId"), 0)
            device_key = data.get("deviceKey", "")
            guest_token = device_key if device_key else f"token_{secrets.token_hex(16)}"
            db.register_guest(guest_token, name, device_key, user_id)
            self.send_json(200, {
                "token": guest_token,
                "name": name,
                "userId": user_id
            })
            return

        # 2. Create Room: /api/rooms or /api/rooms/create
        if path in ("/api/rooms", "/api/rooms/create"):
            name = data.get("name", "Dedicated Studio Project").strip()
            password = data.get("password", "")
            custom_id = data.get("id", "").strip()

            if custom_id and custom_id.isalnum() and not db.get_room(custom_id):
                room_id = custom_id
            else:
                room_id = f"rm_{secrets.token_hex(4)}"

            pw_hash, salt = hash_password(password)
            guest = db.get_guest(token)
            owner_name = guest["name"] if guest else "Owner"
            user_id = guest["user_id"] if guest else 0

            db.create_room(room_id, name, pw_hash, salt, token, owner_name, user_id)

            creator_token = secrets.token_hex(20)
            db.create_room_token(creator_token, room_id, owner_name, user_id)

            console_log("CREATE", f"Project '{name}' created (ID: {room_id}, Protected: {bool(pw_hash)}) by '{owner_name}'")

            self.send_json(200, {
                "id": room_id,
                "name": name,
                "token": creator_token,
                "requires_password": bool(pw_hash)
            })
            return

        # Delete Room: POST /api/rooms/<id>/delete or POST /api/rooms/delete
        if (path.startswith("/api/rooms/") and path.endswith("/delete")) or path == "/api/rooms/delete":
            if path == "/api/rooms/delete":
                room_id = data.get("roomId", "").strip() or data.get("id", "").strip()
            else:
                parts = path.split("/")
                room_id = parts[3]

            room = db.get_room(room_id) or db.find_room_by_id_or_name(room_id)
            if not room:
                self.send_json(404, {"error": "Room not found on server"})
                return

            real_id = room["id"]
            password = data.get("password", "") if isinstance(data, dict) else ""

            client_ip = self.client_address[0]
            now = time.time()
            failed_info = failed_attempts.get(client_ip, [0, 0])
            if failed_info[0] >= 5 and (now - failed_info[1] < 15):
                wait_sec = int(15 - (now - failed_info[1]))
                self.send_json(429, {"error": f"Too many failed password attempts. Please wait {wait_sec}s."})
                return

            ok, res = db.delete_room(real_id, password=password, token=token)
            if not ok:
                if "password" in res.lower():
                    failed_attempts[client_ip] = [failed_info[0] + 1, now]
                    status_code = 401
                else:
                    status_code = 400
                self.send_json(status_code, {"error": res})
                return

            failed_attempts.pop(client_ip, None)

            with state_lock:
                presences.pop(real_id, None)
                last_logged_peers.pop(real_id, None)

            guest = db.get_guest(token)
            user_name = guest["name"] if guest else "User"
            console_log("DELETE", f"Project '{res}' (ID: {real_id}) deleted by '{user_name}' ({client_ip})")

            self.send_json(200, {
                "success": True,
                "message": f"Project '{res}' ({real_id}) deleted successfully"
            })
            return

        # 3. Authenticate / Join Room: /api/rooms/auth or /api/rooms/join
        if path in ("/api/rooms/auth", "/api/rooms/join"):
            query = data.get("roomId", "").strip()
            password = data.get("password", "")
            user_name = data.get("userName", "Peer").strip()
            user_id = safe_int(data.get("userId"), 0)

            room = db.find_room_by_id_or_name(query)
            if not room:
                self.send_json(404, {"error": "Room not found on server"})
                return

            client_ip = self.client_address[0]
            now = time.time()
            failed_info = failed_attempts.get(client_ip, [0, 0])
            if failed_info[0] >= 5 and (now - failed_info[1] < 15):
                wait_sec = int(15 - (now - failed_info[1]))
                self.send_json(429, {"error": f"Too many failed password attempts. Please wait {wait_sec}s."})
                return

            if not verify_password(password, room.get("password_hash", ""), room.get("salt", "")):
                failed_attempts[client_ip] = [failed_info[0] + 1, now]
                console_log("WARN", f"Failed password attempt for project '{room['name']}' from '{user_name}' ({client_ip})")
                self.send_json(401, {"error": "Incorrect room password"})
                return

            failed_attempts.pop(client_ip, None)

            room_id = room["id"]
            session_token = secrets.token_hex(20)
            db.create_room_token(session_token, room_id, user_name, user_id)

            _, total, _ = db.get_room_objects(room_id, page=1, limit=1)
            uid_str = f" (Roblox ID: {user_id})" if user_id > 0 else ""
            console_log("JOIN", f"'{user_name}'{uid_str} joined project '{room['name']}' ({room_id})")

            self.send_json(200, {
                "success": True,
                "token": session_token,
                "roomId": room_id,
                "name": room["name"],
                "revision": room["revision"],
                "objectsCount": total
            })
            return

        # 4. Synchronize POST: /api/rooms/<id>/sync
        if path.startswith("/api/rooms/") and path.endswith("/sync"):
            parts = path.split("/")
            room_id = parts[3]

            if not db.get_room(room_id):
                self.send_json(404, {"error": "Room not found on server"})
                return

            if not db.is_authorized(room_id, token):
                self.send_json(401, {"error": "Unauthorized: Password required or invalid room token"})
                return

            changes = data.get("changes", [])
            since_rev = int(data.get("since", 0))

            res, err = db.apply_room_changes(room_id, changes, since_rev)
            if err:
                self.send_json(400, {"error": err})
                return

            # Console log only when changes were actually made
            diff_count = res.get("diff_count", 0)
            if diff_count > 0:
                guest = db.get_guest(token)
                author = guest["name"] if (guest and guest.get("name")) else "Editor"
                console_log("SYNC", f"[{res['room_name']}] Rev {res['revision']}: +{diff_count} edit(s) synced by '{author}'")

            self.send_json(200, res)
            return

        # 5. Presence / Camera POST: /api/rooms/<id>/presence
        if path.startswith("/api/rooms/") and path.endswith("/presence"):
            parts = path.split("/")
            room_id = parts[3]
            room = db.get_room(room_id)
            if not room:
                self.send_json(404, {"error": "Room not found"})
                return

            if not db.is_authorized(room_id, token):
                self.send_json(401, {"error": "Unauthorized"})
                return

            guest = db.get_guest(token)
            user_name = guest["name"] if (guest and guest.get("name") and guest["name"] != "Peer") else data.get("name", "Peer")
            user_id = safe_int(data.get("userId"), guest["user_id"] if guest else 0)

            if user_id > 0 and token:
                db.update_token_user_id(token, user_id)

            now = time.time()
            with state_lock:
                room_presences = presences.setdefault(room_id, {})
                if data.get("leave"):
                    if token in room_presences:
                        del room_presences[token]
                        console_log("LEAVE", f"'{user_name}' left project '{room['name']}'")
                else:
                    activity = str(data.get("activity", "")).strip()
                    is_idle = bool(data.get("isIdle", False))
                    if not activity:
                        sel_count = len(data.get("selected", []))
                        activity = f"Selected {sel_count} part(s)" if sel_count > 0 else "Viewing place"

                    peer_seed = hash(token or user_name)
                    room_presences[token] = {
                        "id": token,
                        "name": user_name,
                        "userId": user_id,
                        "position": data.get("position", [0, 10, 0]),
                        "look": data.get("look", [0, 0, -1]),
                        "selected": data.get("selected", []),
                        "activity": activity,
                        "isIdle": is_idle,
                        "idleTime": safe_int(data.get("idleTime"), 0),
                        "hue": (abs(peer_seed) % 100) / 100.0,
                        "time": now
                    }

                active_peers = []
                current_active_names = set()
                for p_id, p in list(room_presences.items()):
                    if now - p.get("time", 0) < 15 and p_id != token:
                        active_peers.append(p)
                    if now - p.get("time", 0) < 15:
                        current_active_names.add(p.get("name", "Peer"))
                    elif now - p.get("time", 0) >= 15:
                        room_presences.pop(p_id, None)

                # Log when peer set changes
                prev_active = last_logged_peers.get(room_id, set())
                if current_active_names != prev_active:
                    last_logged_peers[room_id] = current_active_names
                    if current_active_names:
                        names_str = ", ".join(sorted(current_active_names))
                        console_log("PEERS", f"[{room['name']}] Online editors ({len(current_active_names)}): {names_str}")
                    else:
                        console_log("PEERS", f"[{room['name']}] All developers disconnected (0 online)")

            self.send_json(200, active_peers)
            return

        # 6. Save Room: /api/rooms/<id>/save
        if path.startswith("/api/rooms/") and path.endswith("/save"):
            parts = path.split("/")
            room_id = parts[3]
            room = db.get_room(room_id)
            if room:
                _, total, _ = db.get_room_objects(room_id, page=1, limit=1)
                self.send_json(200, {"saved": True, "database": "SQLite", "objects_count": total})
                return
            self.send_json(404, {"error": "Room not found"})
            return

        # 7. Invitations: /api/invitations
        if path == "/api/invitations":
            global next_invite_id
            target_nick = data.get("nick", "").strip()
            room_id = data.get("room", "")
            guest = db.get_guest(token)
            sender_name = guest["name"] if guest else "Host"
            room = db.get_room(room_id)
            room_name = room["name"] if room else "Project"

            with state_lock:
                inv_id = str(next_invite_id)
                next_invite_id += 1
                inv = {
                    "id": inv_id,
                    "from": sender_name,
                    "nick": target_nick,
                    "room": room_id,
                    "place": room_name,
                    "state": "pending",
                    "recipientOnline": False,
                    "delivered": False
                }
                invitations.append(inv)
            console_log("INVITE", f"Invite from '{sender_name}' to '{target_nick}' for '{room_name}'")
            self.send_json(200, inv)
            return

        # 8. Respond to Invitation: /api/invitations/<id>/respond
        if path.startswith("/api/invitations/") and path.endswith("/respond"):
            parts = path.split("/")
            inv_id = parts[3]
            decision = data.get("decision", "accept")
            guest = db.get_guest(token)
            user_name = guest["name"] if guest else "User"

            with state_lock:
                for inv in invitations:
                    if inv.get("id") == inv_id:
                        inv["state"] = "accepted" if decision == "accept" else "declined"
                        room_id = inv.get("room")
                        room = db.get_room(room_id)
                        session_token = secrets.token_hex(20)
                        user_id = guest["user_id"] if guest else 0
                        db.create_room_token(session_token, room_id, user_name, user_id)

                        console_log("INVITE", f"'{user_name}' {decision}ed invitation to '{room['name'] if room else room_id}'")

                        self.send_json(200, {
                            "decision": decision,
                            "room": {"id": room_id, "name": room["name"] if room else "Project"},
                            "token": session_token
                        })
                        return
            self.send_json(404, {"error": "Invitation not found"})
            return

        self.send_json(404, {"error": f"Endpoint not found: {path}"})


def handle_exit_signal(sig, frame):
    global is_shutting_down
    if is_shutting_down:
        return
    is_shutting_down = True
    console_log("SHUTDOWN", f"Received signal ({sig}). Committing SQLite WAL checkpoint...")
    if db:
        db.close()
    console_log("SHUTDOWN", "Server stopped gracefully.")
    sys.exit(0)


def get_network_ips():
    ips = []
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.append(ip)
    except Exception:
        pass
    return ips


def main():
    global db, access_logger, SERVER_START_TIME

    SERVER_START_TIME = time.time()

    parser = argparse.ArgumentParser(description="PlaceTogether Dedicated Co-op Server (v3.3 - SQLite & Web Dashboard)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host interface to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind (default: {DEFAULT_PORT})")
    parser.add_argument("--db", "--data", dest="db_file", type=str, default=DEFAULT_DB_FILE, help=f"Path to SQLite database (default: {DEFAULT_DB_FILE})")
    parser.add_argument("--log", dest="log_file", type=str, default=DEFAULT_LOG_FILE, help=f"Path to HTTP access log file (default: {DEFAULT_LOG_FILE})")
    parser.add_argument("--dashboard", choices=["on", "off", "auto"], default="auto",
                        help="Web Dashboard: on (enable), off (disable), or auto (use database setting, default: auto)")
    args = parser.parse_args()

    db_path = os.path.abspath(args.db_file)
    log_path = os.path.abspath(args.log_file)

    signal.signal(signal.SIGINT, handle_exit_signal)
    signal.signal(signal.SIGTERM, handle_exit_signal)

    access_logger = AccessLogger(log_path)
    db = Database(db_path)

    if args.dashboard == "on":
        db.set_dashboard_enabled(True)
        console_log("CONFIG", "Web Dashboard force-enabled via CLI flag.")
    elif args.dashboard == "off":
        db.set_dashboard_enabled(False)
        console_log("CONFIG", "Web Dashboard force-disabled via CLI flag.")

    json_candidates = [
        os.path.join(os.path.dirname(db_path), LEGACY_DATA_FILE),
        os.path.abspath(LEGACY_DATA_FILE)
    ]
    for j_path in json_candidates:
        if os.path.exists(j_path):
            db.migrate_from_json_if_needed(j_path)
            break

    stats = db.get_stats()

    print("=" * 68)
    print("  PlaceTogether Dedicated 24/7 Co-op Server (v3.3)")
    print("=" * 68)
    print(f"  Bind Address       : {args.host}:{args.port}")
    print(f"  Storage Engine     : SQLite 3 (WAL Mode / Instant ACID Commits)")
    print(f"  Database File      : {db_path}")
    print(f"  HTTP Access Log    : {log_path} (Raw HTTP traffic redirected here)")
    dash_label = "ENABLED" if db.is_dashboard_enabled() else "DISABLED"
    print(f"  Web Dashboard      : http://127.0.0.1:{args.port}/ [{dash_label}]")
    print(f"  Platform           : {sys.platform} (Python {sys.version.split()[0]})")
    print("-" * 68)
    print("  Available Connection Addresses (enter in plugin Settings):")
    for ip in get_network_ips():
        print(f"    -> http://{ip}:{args.port}")
    print(f"    -> http://127.0.0.1:{args.port} (Localhost)")
    print("=" * 68)

    console_log("READY", f"Database ready: {stats['total_rooms']} project(s), {stats['total_objects']} object(s), {stats['total_guests']} user(s).")

    server_address = (args.host, args.port)
    try:
        httpd = ThreadedHTTPServer(server_address, PlaceTogetherHandler)
    except Exception as e:
        console_log("FATAL", f"Failed to bind to {args.host}:{args.port}: {e}")
        try:
            input("\nPress Enter to close window...")
        except Exception:
            pass
        sys.exit(1)

    console_log("READY", f"Server running on http://{args.host}:{args.port}")
    if db.is_dashboard_enabled():
        console_log("INFO", "Open server address in browser to view live Web Dashboard with Roblox avatars.")
    else:
        console_log("INFO", "Web Dashboard is disabled (can be enabled via Studio Settings or --dashboard on).")
    console_log("INFO", "Press Ctrl+C to stop.\n")

    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        handle_exit_signal(signal.SIGINT, None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        handle_exit_signal(signal.SIGINT, None)
    except Exception:
        import traceback
        traceback.print_exc()
        try:
            input("\n[ERROR] Server stopped with an error. Press Enter to close window...")
        except Exception:
            pass
        sys.exit(1)
