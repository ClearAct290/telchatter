# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# TelChatter - Copyright (C) 2026 ClearAct290-Reddit
"""TelChatter - server entry point: App (shared state) and the accept loop."""
import asyncio
import logging
import os
import sys
import time

from tc_config import Config
from tc_db import Db
from tc_session import RawLogger
import tc_screens  # noqa: F401  (populates tc_session.STATE_TABLE on import)

log = logging.getLogger("telchatter")
VERSION = "1.0"


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.rawlog = RawLogger(cfg.logging_raw_file, enabled=cfg.logging_raw_enabled,
                                 max_dump=cfg.logging_raw_max_dump_bytes)
        if self.rawlog.enabled:
            msg = f"raw-traffic debug log enabled: {self.rawlog.path}"
            log.info(msg)
            print("i  " + msg)
        self.db = Db(cfg.paths_database, default_room_names=cfg.room_names())
        self.db.ensure_admin_account(str(cfg.security_admin_username), str(cfg.security_admin_password))
        if str(cfg.security_admin_password) == "admin1234":
            msg = ("SECURITY: the admin account still uses the DEFAULT login (%s / admin1234). Anyone who "
                   "knows it can edit your rooms and readme. Set [security] admin_password in "
                   "tc_config.ini and restart the server." % cfg.security_admin_username)
            log.warning(msg)
            print("!! " + msg)
        tc_screens.ensure_readme_file(cfg)
        self.sessions = set()
        self.online = set()          # sessions that are logged in (shown in the status bar count)
        self._rooms_cache = None
        self._fails = {}

    # ---- rooms (cached; refreshed on admin edits)
    def room_list(self):
        if self._rooms_cache is None:
            self._rooms_cache = self.db.list_rooms()
        return self._rooms_cache

    def refresh_rooms(self):
        self._rooms_cache = None

    def room_by_id(self, room_id):
        for r in self.room_list():
            if r["id"] == room_id:
                return r
        return None

    # ---- login throttling (per IP and per account)
    def login_locked(self, ip, username):
        now = time.monotonic()
        left = 0.0
        for key in (("ip", str(ip)), ("user", username.lower())):
            rec = self._fails.get(key)
            if rec and rec[1] > now:
                left = max(left, rec[1] - now)
        return left

    def login_failed(self, ip, username):
        now = time.monotonic()
        limit = int(self.cfg.security_login_max_attempts)
        for key in (("ip", str(ip)), ("user", username.lower())):
            rec = self._fails.setdefault(key, [0, 0.0])
            if rec[1] and rec[1] <= now:
                rec[0], rec[1] = 0, 0.0
            rec[0] += 1
            if rec[0] >= limit:
                rec[1] = now + float(self.cfg.security_login_lockout_seconds)
                rec[0] = 0

    def login_ok(self, ip, username):
        self._fails.pop(("user", username.lower()), None)

    # ---------------------------------------------------------------- connections
    def ip_count(self, ip):
        return sum(1 for s in self.sessions if s.ip == ip)

    async def handle(self, reader, writer):
        ip = writer.get_extra_info("peername")
        ip = ip[0] if ip else "?"
        try:
            import socket
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        if len(self.sessions) >= int(self.cfg.server_max_clients) or \
                self.ip_count(ip) >= int(self.cfg.server_max_clients_per_ip):
            writer.close()
            return
        from tc_session import Session
        sess = Session(self, reader, writer, ip)
        self.sessions.add(sess)
        log.info("[%s] connected (%d online)", ip, len(self.sessions))
        try:
            await sess.run()
        except ConnectionError:
            pass
        except Exception:
            log.exception("[%s] fatal session error", ip)
        finally:
            self.sessions.discard(sess)
            self.online.discard(sess)
            await sess.close()
            log.info("[%s] disconnected (%d online)", ip, len(self.sessions))


def setup_logging(cfg):
    handlers = [logging.StreamHandler()]
    if cfg.logging_file:
        handlers.append(logging.FileHandler(str(cfg.logging_file), encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, str(cfg.logging_level).upper(), logging.INFO),
                        handlers=handlers, format="%(asctime)s %(levelname)s %(message)s")


async def _idle_reaper(app):
    """Belt-and-suspenders background sweep: screens already check idle/session-length
    limits on their own input-wait timeouts, but a session sitting in a long blocking
    wait (none currently, but future-proofing) would otherwise only be caught next time
    it calls get_key. This catches it from the outside too."""
    while True:
        await asyncio.sleep(30)
        now_limit_idle = float(app.cfg.session_idle_timeout_minutes) * 60
        now_limit_max = float(app.cfg.session_max_session_hours) * 3600
        for sess in list(app.sessions):
            try:
                if sess.idle_seconds() > now_limit_idle or sess.uptime_seconds() > now_limit_max:
                    sess.closed = True
            except Exception:
                pass


async def serve(cfg):
    app = App(cfg)
    server = await asyncio.start_server(app.handle, cfg.server_host, cfg.server_port)
    log.info("TelChatter %s listening on %s:%s", VERSION, cfg.server_host, cfg.server_port)
    print(f"TelChatter {VERSION} listening on telnet://{cfg.server_host}:{cfg.server_port} (Ctrl+C to stop)")
    asyncio.ensure_future(_idle_reaper(app))
    async with server:
        await server.serve_forever()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="TelChatter - a telnet BBS chat server")
    ap.add_argument("-c", "--config", default=None, help="path to the config file (default: tc_config.ini next to tc_server.py)")
    args = ap.parse_args()
    cfg = Config(args.config)
    setup_logging(cfg)
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
