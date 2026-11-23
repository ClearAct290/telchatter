# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# TelChatter - Copyright (C) 2026 ClearAct290-Reddit
"""
TelChatter - database (telchatter.db, sqlite3).

Passwords are NEVER stored. Only a salted, deliberately slow hash is kept, in a
self-describing string so the cost can be raised later without breaking old rows:

    scrypt$16384$8$1$<salt hex>$<hash hex>          (default, hashlib.scrypt)
    pbkdf2_sha256$600000$<salt hex>$<hash hex>      (fallback if scrypt is missing)

* the salt is 16 random bytes from ``secrets`` (OS CSPRNG), unique per account
* verification uses ``hmac.compare_digest`` (constant time)
* logging in with an unknown name still burns one hash, so response time does not
  reveal which usernames exist

Schema:
  accounts(username PK nocase, pw, created_at, last_login, is_operator)
  rooms(id PK, name, position)
  messages(id PK, room_id, username, name_color, kind, body, image_id, created_at)
  images(id PK, owner, fmt, animated, cols, rows, frame_ms, data BLOB, created_at)

`messages.kind` is "text" or "image"; for an image message, `image_id` points into
`images` and `body` holds a short caption/filename only.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
PBKDF2_ITERS = 600_000

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
RESERVED_RE = re.compile(r"^(system|server|admin|root|operator|sysop)$", re.I)
PASSWORD_MIN, PASSWORD_MAX = 6, 64


# ---------------------------------------------------------------- password hashing
def _scrypt(pw, salt, n, r, p):
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32,
                          maxmem=128 * n * r * p * 2 + (1 << 20))


def hash_password(password):
    salt = secrets.token_bytes(16)
    if hasattr(hashlib, "scrypt"):
        try:
            dk = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
            return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"
        except (ValueError, MemoryError, OSError):
            pass
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    try:
        parts = stored.split("$")
        if parts[0] == "scrypt" and len(parts) == 6:
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            salt, want = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
            got = _scrypt(password, salt, n, r, p)
        elif parts[0] == "pbkdf2_sha256" and len(parts) == 4:
            salt, want = bytes.fromhex(parts[2]), bytes.fromhex(parts[3])
            got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(parts[1]))
        else:
            return False
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError, MemoryError, OSError):
        return False


def needs_rehash(stored):
    parts = stored.split("$")
    try:
        if parts[0] == "scrypt":
            return (int(parts[1]), int(parts[2]), int(parts[3])) < (SCRYPT_N, SCRYPT_R, SCRYPT_P)
        if parts[0] == "pbkdf2_sha256":
            return hasattr(hashlib, "scrypt") or int(parts[1]) < PBKDF2_ITERS
    except (ValueError, IndexError):
        pass
    return True


_DUMMY = None


def _dummy_hash():
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password(secrets.token_hex(8))
    return _DUMMY


def validate_username(username, extra_reserved=None):
    if not USERNAME_RE.match(username or ""):
        return "3-16 chars: letters, digits, _"
    if RESERVED_RE.match(username):
        return "That name is reserved"
    if extra_reserved and username.lower() == str(extra_reserved).lower():
        return "That name is reserved"
    return ""


def validate_password(password):
    if len(password or "") < PASSWORD_MIN:
        return f"Password needs {PASSWORD_MIN}+ characters"
    if len(password) > PASSWORD_MAX:
        return f"Password is too long (max {PASSWORD_MAX})"
    return ""


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    username      TEXT PRIMARY KEY COLLATE NOCASE,
    pw            TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL DEFAULT 0,
    last_login    REAL NOT NULL DEFAULT 0,
    name_color    INTEGER NOT NULL DEFAULT -1,
    is_operator   INTEGER NOT NULL DEFAULT 0,
    managed_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rooms (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id    INTEGER NOT NULL,
    username   TEXT NOT NULL,
    name_color INTEGER NOT NULL DEFAULT 1,
    kind       TEXT NOT NULL DEFAULT 'text',
    body       TEXT NOT NULL DEFAULT '',
    image_id   INTEGER,
    created_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(room_id, id);
CREATE TABLE IF NOT EXISTS images (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner      TEXT NOT NULL,
    fmt        TEXT NOT NULL,
    animated   INTEGER NOT NULL DEFAULT 0,
    cols       INTEGER NOT NULL,
    rows       INTEGER NOT NULL,
    frame_ms   INTEGER NOT NULL DEFAULT 100,
    data       BLOB NOT NULL,
    created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


class Db:
    """One connection guarded by a lock (sqlite3 objects are not thread-safe, and the
    slow password hashing / image conversion run in worker threads)."""

    def __init__(self, path="telchatter.db", default_room_names=None):
        self.path = path
        self._lock = threading.Lock()
        fresh = not os.path.exists(path)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        try:
            self.conn.execute("ALTER TABLE accounts ADD COLUMN managed_admin INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # already there (fresh DB, or a server that already restarted since this change)
        self.conn.commit()
        if fresh:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        if default_room_names:
            self._seed_rooms(default_room_names)
        self.admin_username = None  # set by ensure_admin_account(); extra reserved name for validate_username

    def close(self):
        with self._lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    # ---- rooms
    def _seed_rooms(self, names):
        with self._lock:
            n = self.conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
            if n:
                return
            for i, name in enumerate(names):
                self.conn.execute("INSERT INTO rooms(name, position) VALUES (?,?)", (name, i))
            self.conn.commit()

    def list_rooms(self):
        with self._lock:
            rows = self.conn.execute("SELECT id, name, position FROM rooms ORDER BY position, id").fetchall()
        return [dict(r) for r in rows]

    def rename_room(self, room_id, new_name):
        with self._lock:
            self.conn.execute("UPDATE rooms SET name=? WHERE id=?", (new_name, room_id))
            self.conn.commit()

    def add_room(self, name):
        with self._lock:
            pos = self.conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM rooms").fetchone()[0]
            cur = self.conn.execute("INSERT INTO rooms(name, position) VALUES (?,?)", (name, pos))
            self.conn.commit()
            return cur.lastrowid

    def remove_room(self, room_id):
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE room_id=?", (room_id,))
            self.conn.execute("DELETE FROM rooms WHERE id=?", (room_id,))
            self.conn.commit()

    # ---- accounts
    def username_taken(self, username):
        with self._lock:
            cur = self.conn.execute("SELECT 1 FROM accounts WHERE username=? COLLATE NOCASE", (username,))
            return cur.fetchone() is not None

    def create_account(self, username, password, name_color=-1):
        err = validate_username(username, extra_reserved=self.admin_username) or validate_password(password)
        if err:
            return False, err
        pw = hash_password(password)
        with self._lock:
            try:
                # Regular signups are never operators - only the one dedicated admin
                # account (see ensure_admin_account) ever gets the Admin menu.
                self.conn.execute(
                    "INSERT INTO accounts(username,pw,created_at,last_login,name_color,is_operator,managed_admin) "
                    "VALUES (?,?,?,?,?,0,0)",
                    (username, pw, time.time(), time.time(), name_color))
                self.conn.commit()
                return True, ""
            except sqlite3.IntegrityError:
                return False, "Username taken"

    def ensure_admin_account(self, admin_username, admin_password):
        """Called once at startup. Makes sure exactly one account - the one named
        `admin_username` in the config - is the operator, with its password kept in
        sync with `admin_password` from the config (there is no in-app way to change
        it). Also demotes any other account that might have been left as an operator
        by an older version of this server (which made whoever registered first the
        operator); it is never silently handed to someone just because they happen to
        have already registered the configured admin name under a DIFFERENT prior
        config (managed_admin=0 on that row) - that would mean overwriting a stranger's
        password, so instead this logs a warning and leaves that account alone."""
        import logging
        log = logging.getLogger("telchatter")
        self.admin_username = admin_username
        err = validate_password(admin_password)
        if err:
            log.warning("[security] admin_password in the config is weak (%s) - using it anyway, "
                        "but you should pick something stronger.", err)
        new_hash = hash_password(admin_password)
        with self._lock:
            row = self.conn.execute(
                "SELECT username, pw, managed_admin FROM accounts WHERE username=? COLLATE NOCASE",
                (admin_username,)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO accounts(username,pw,created_at,last_login,name_color,is_operator,managed_admin) "
                    "VALUES (?,?,?,?,-1,1,1)",
                    (admin_username, new_hash, time.time(), time.time()))
                log.info("[security] created the admin account '%s' from the config.", admin_username)
            elif row["managed_admin"]:
                if not verify_password(admin_password, row["pw"]):
                    self.conn.execute(
                        "UPDATE accounts SET pw=?, is_operator=1 WHERE username=? COLLATE NOCASE",
                        (new_hash, row["username"]))
                    log.info("[security] admin password changed in the config - updated the stored hash.")
                else:
                    self.conn.execute(
                        "UPDATE accounts SET is_operator=1 WHERE username=? COLLATE NOCASE", (row["username"],))
            else:
                log.warning("[security] an account named '%s' already exists and is NOT the managed admin "
                            "account - leaving its password alone and NOT granting it operator access. "
                            "Pick a different [security] admin_username, or rename/remove that account, "
                            "if you want '%s' to be the admin.", admin_username, admin_username)
            # Demote every other operator (covers DBs from before this change, where
            # whichever account registered first was automatically made the operator).
            self.conn.execute(
                "UPDATE accounts SET is_operator=0 WHERE is_operator=1 AND NOT (username=? COLLATE NOCASE)",
                (admin_username,))
            self.conn.commit()

    def authenticate(self, username, candidates):
        """Returns the account's stored username (original case) or None."""
        with self._lock:
            row = self.conn.execute("SELECT username, pw FROM accounts WHERE username=? COLLATE NOCASE",
                                    (username,)).fetchone()
        if row is None:
            verify_password("x", _dummy_hash())
            return None
        uname, pw = row["username"], row["pw"]
        ok_pw = None
        for cand in candidates:
            if verify_password(cand, pw):
                ok_pw = cand
                break
        if ok_pw is None:
            return None
        new_hash = hash_password(ok_pw) if needs_rehash(pw) else None
        with self._lock:
            if new_hash:
                self.conn.execute("UPDATE accounts SET pw=? WHERE username=? COLLATE NOCASE", (new_hash, uname))
            self.conn.execute("UPDATE accounts SET last_login=? WHERE username=? COLLATE NOCASE", (time.time(), uname))
            self.conn.commit()
        return uname

    def get_account(self, username):
        with self._lock:
            row = self.conn.execute(
                "SELECT username, created_at, last_login, name_color, is_operator FROM accounts "
                "WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        return dict(row) if row else None

    def set_name_color(self, username, color_idx):
        with self._lock:
            self.conn.execute("UPDATE accounts SET name_color=? WHERE username=? COLLATE NOCASE",
                              (color_idx, username))
            self.conn.commit()

    # ---- messages
    def add_text_message(self, room_id, username, name_color, body):
        now = time.time()
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO messages(room_id, username, name_color, kind, body, created_at) VALUES (?,?,?,'text',?,?)",
                (room_id, username, name_color, body, now))
            self.conn.commit()
            return cur.lastrowid, now

    def add_image_message(self, room_id, username, name_color, image_id, caption=""):
        now = time.time()
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO messages(room_id, username, name_color, kind, body, image_id, created_at) "
                "VALUES (?,?,?,'image',?,?,?)",
                (room_id, username, name_color, caption, image_id, now))
            self.conn.commit()
            return cur.lastrowid, now

    def recent_messages(self, room_id, limit):
        """Most recent `limit` messages in a room, oldest first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, username, name_color, kind, body, image_id, created_at FROM messages "
                "WHERE room_id=? ORDER BY id DESC LIMIT ?", (room_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def message_count(self, room_id):
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM messages WHERE room_id=?", (room_id,)).fetchone()[0]

    # ---- images
    def store_image(self, owner, fmt, animated, cols, rows, frame_ms, data):
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO images(owner, fmt, animated, cols, rows, frame_ms, data, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (owner, fmt, 1 if animated else 0, cols, rows, frame_ms, data, time.time()))
            self.conn.commit()
            return cur.lastrowid

    def get_image(self, image_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT id, owner, fmt, animated, cols, rows, frame_ms, data FROM images WHERE id=?",
                (image_id,)).fetchone()
        return dict(row) if row else None

    # ---- settings (readme/motd overrides, etc.)
    def get_setting(self, key, default=""):
        with self._lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        with self._lock:
            self.conn.execute("INSERT INTO settings(key, value) VALUES (?,?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self.conn.commit()
