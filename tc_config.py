# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# TelChatter - Copyright (C) 2026 ClearAct290-Reddit
"""TelChatter - config (tc_config.ini).

Same idea as the author's Retro Recoil / TelTube config files: missing keys fall
back to the defaults below, ``cfg.section_key`` attribute access, and the file is
written next to the server on first run. Relative paths in [paths] resolve against
the folder that holds the config file, not the shell's current directory.
"""
import configparser
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_INI = """\
; TelChatter server configuration.  Missing keys fall back to these defaults.
; Restart the server after editing this file.  (Delete a line to get its default.)

[server]
host = 0.0.0.0
port = 2323
; shown in the top banner and the default README
name = TelChatter
max_clients = 100
max_clients_per_ip = 4
; used until the client reports its window size (telnet NAWS); PETSCII clients
; always use [server] commodore_cols x commodore_rows instead, below
default_cols = 80
default_rows = 24
commodore_cols = 40
commodore_rows = 25
; seconds to wait for telnet option negotiation before the first screen
negotiate_wait = 0.3

[session]
; minutes of no keypress before an automatic disconnect
idle_timeout_minutes = 30
; hard ceiling on a single session's connected time, even with activity
max_session_hours = 6

[display]
; truecolor | 256 | 16.  ANSI/ASCII clients only - PETSCII always uses the
; real C64 16-colour set via reverse-video cells.
color_mode = truecolor

[rooms]
; how many chat rooms exist by default on a brand new install, and their
; starting names ("Room 1".."Room N" if you list fewer names than count).
; The operator (the first account created on a new server) can rename, add and
; remove rooms afterwards from the Admin menu - this is only the first-run seed.
count = 3
names = Room 1, Room 2, Room 3
; how many past messages a client may scroll back through in one room
scrollback_default = 65
scrollback_max = 750

[images]
; allow the image-upload option in chat rooms at all
enabled = true
; give up on a transfer that never starts, or never finishes, after this long (seconds)
upload_timeout_seconds = 120
; hard cap on the uploaded file's size, in bytes, before it is refused outright
max_upload_bytes = 8000000
; the stored art is never larger than this many character cells across/down,
; regardless of how big a viewer's terminal is - keeps one huge image from
; making every room slow to scroll for everyone
max_art_cols = 160
max_art_rows = 80
; animated GIFs are resampled to at most this many frames per second
gif_max_fps = 10
; a GIF frame-delta cell is only resent if it changed by at least this much
; perceptual distance in the 16-colour palette (0 = resend on any change at
; all, higher = more bandwidth saved at the cost of subtle motion)
motion_threshold = 1

[zmodem]
; Images are received over ZMODEM (the same protocol SyncTERM, Qodem, mTelnet and
; "sz/rz" speak), not by pasting the file as simulated keystrokes: it is a real binary
; transfer, so it survives whatever that client does with line endings or control
; bytes, and a damaged block is automatically re-sent rather than silently corrupting
; the image. Nothing here usually needs changing.
; Ask the sender to escape EVERY control byte (the ESCCTL flag in our ZRINIT). Leave this
; OFF: with it on, SyncTERM treats even the CR / LF / XON that end a ZMODEM hex header as
; "unescaped control characters", drops them and rejects the header ("HEX header not
; terminated with LF ... INVALID HEADER"). Off sends the classic header every `rz` sends
; (0100000023be50); the telnet link is binary-clean, and every block is CRC-checked anyway.
escape_ctrl = false
; Also print "rz" + CR before our first ZRINIT. Off by default: terminals that can
; auto-start an upload react to the ZRINIT header itself, and the extra text would
; otherwise be drawn on the screen. Turn it on only for a client that needs it.
rz_prefix = false

[video]
; bytes/second budget the server will spend pushing frames to one ANSI client,
; and the much smaller budget for a real/emulated Commodore link
rate_limit_bytes = 400000
commodore_rate_limit_bytes = 4500
delta_gap_merge = 3
full_refresh_seconds = 20.0

[security]
; The ONLY account that ever gets the Admin menu (edit the readme, rename/add/remove
; rooms). It is not "whoever signs up first" - it is specifically this one account,
; and its username is always reserved so nobody else can register it. On every start,
; the server makes sure an account with this username exists and that its password
; matches admin_password below, so to change the admin password, edit it here and
; restart the server (there is no in-app "change password" screen for it). Change
; admin_password away from the default before exposing this server to anyone else.
admin_username = admin
admin_password = admin1234
; failed logins allowed per IP / per account before a temporary lock-out
login_max_attempts = 6
login_lockout_seconds = 60

[paths]
database = telchatter.db
; edit this file to change the home-screen readme (re-read on every view)
readme = readme.txt

[logging]
level = INFO
file =
; Raw-traffic debug log: every inbound byte chunk a client sends (telnet negotiation,
; keypresses, and - most usefully for tracking down an "Invalid file format" report -
; the exact bytes received during an image upload, plus the decoder's error message
; and a hex dump of the start/end of the file it rejected). Written as plain text,
; one line per event, each prefixed with a timestamp and the client's IP. On by
; default so a fresh install already has this evidence the first time something
; looks wrong; set to false to turn it off (e.g. a busy public server).
raw_enabled = true
raw_file = raw_data.log
; a chunk longer than this is hex-dumped only up to this many bytes (plus a final
; few bytes), so one giant image upload does not turn the log into another copy of
; the image - the point is to see the framing/format, not store the whole file
raw_max_dump_bytes = 256
"""


class Config:
    def __init__(self, path=None):
        self.path = os.path.abspath(path or os.path.join(BASE_DIR, "tc_config.ini"))
        self.base = os.path.dirname(self.path)
        self._cp = configparser.ConfigParser(inline_comment_prefixes=(";",), interpolation=None)
        self._cp.read_string(DEFAULT_INI)
        if os.path.exists(self.path):
            self._cp.read(self.path, encoding="utf-8")
        else:
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(DEFAULT_INI)
                print(f"Wrote default config to {self.path}")
            except OSError:
                pass

    def reload(self):
        new = configparser.ConfigParser(inline_comment_prefixes=(";",), interpolation=None)
        new.read_string(DEFAULT_INI)
        new.read(self.path, encoding="utf-8")
        self._cp = new

    def __getattr__(self, name):
        if name.startswith("_") or "_" not in name:
            raise AttributeError(name)
        section, key = name.split("_", 1)
        if not self._cp.has_option(section, key):
            raise AttributeError(name)
        raw = self._cp.get(section, key).strip()
        if section == "paths" or (section == "logging" and key in ("file", "raw_file")):
            return raw if (not raw or os.path.isabs(raw)) else os.path.join(self.base, raw)
        low = raw.lower()
        if low in ("true", "yes", "on"):
            return True
        if low in ("false", "no", "off"):
            return False
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            return raw

    def room_names(self):
        count = int(self.rooms_count)
        raw = [n.strip() for n in str(self.rooms_names).split(",") if n.strip()]
        names = []
        for i in range(count):
            names.append(raw[i] if i < len(raw) else f"Room {i + 1}")
        return names
