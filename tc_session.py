# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# TelChatter - Copyright (C) 2026 ClearAct290-Reddit
"""
TelChatter - per-connection session: telnet IO, key parsing, screens.

The telnet negotiation / key-parsing shape (IAC handling, NAWS, PETSCII cursor-key
bytes, the lone-ESC-vs-arrow-sequence timeout trick) is adapted from the author's own
Retro Recoil project (itself following the same approach as the TelTube project,
GPLv2+). See LICENSE / GITHUB_README.md.

Terminal auto-detection: rather than asking the user to choose, TelChatter looks at
what the client itself told us during telnet option negotiation (TTYPE, and whether
it answers NAWS at all) during a short negotiation window right after connecting. A
real/emulated Commodore 64 BBS terminal typically never answers NAWS (no concept of
a resizable window) and, if it answers TTYPE at all, reports something like
"ANSI" is still common from C64 terminal programs, which is why emptiness-of-NAWS is
weighted more heavily than TTYPE text. If detection is ambiguous, the user is asked
once (`page_choose_terminal` in tc_screens.py), in plain 7-bit text so every client -
whatever it turns out to be - can read the question.
"""
import asyncio
import binascii
import threading
import time
from collections import deque

from tc_render import Grid, Presenter


class RawLogger:
    """Appends tagged, timestamped lines to [logging] raw_file - every inbound byte
    chunk a client sends, telnet negotiation, terminal-mode decisions and (most
    usefully when someone reports "Invalid file format") the exact bytes an image
    upload arrived as and why the decoder rejected them. One instance is shared by
    the whole server (opened once in tc_server.py); writes are append-only text, a
    lock keeps lines from different connections from interleaving mid-line, and any
    failure to write (disk full, permissions) is swallowed - this is a debug aid,
    never something that should take the server down."""

    def __init__(self, path, enabled=True, max_dump=256):
        self.path = path
        self.enabled = bool(enabled) and bool(path)
        self.max_dump = max(16, int(max_dump))
        self._lock = threading.Lock()
        self._fh = None
        if self.enabled:
            try:
                self._fh = open(path, "a", encoding="utf-8", buffering=1)
            except OSError:
                self.enabled = False

    def hexdump(self, data):
        """Hex + ASCII preview of `data`, truncated in the middle for long payloads so
        the log shows the header and trailer of a file (where format signatures and
        end-markers live) without storing the whole thing."""
        n = len(data)
        if n <= self.max_dump:
            head, tail, skipped = data, b"", 0
        else:
            half = self.max_dump // 2
            head, tail, skipped = data[:half], data[-half:], n - 2 * half

        def fmt(b):
            h = binascii.hexlify(b).decode("ascii")
            pairs = " ".join(h[i:i + 2] for i in range(0, len(h), 2))
            ascii_ = "".join(chr(c) if 32 <= c < 127 else "." for c in b)
            return f"{pairs}  |{ascii_}|"

        if not tail:
            return f"{n}B: {fmt(head)}"
        return f"{n}B: {fmt(head)} ... [{skipped}B omitted] ... {fmt(tail)}"

    def log(self, ip, tag, msg):
        if not self.enabled or self._fh is None:
            return
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{ip}] {tag}: {msg}\n"
        try:
            with self._lock:
                self._fh.write(line)
        except OSError:
            self.enabled = False


class _NullRawLogger:
    """Used when raw logging is off or before an App exists (e.g. in unit tests that
    build a bare Session) - every call is a harmless no-op."""
    enabled = False

    def hexdump(self, data):
        return f"{len(data)}B"

    def log(self, ip, tag, msg):
        pass


NULL_RAWLOG = _NullRawLogger()

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
OPT_BINARY, OPT_ECHO, OPT_SGA, OPT_TTYPE, OPT_NAWS = 0, 1, 3, 24, 31
OPT_ENVIRON = 39   # NEW-ENVIRON (RFC 1572): how a client hands over a preset USER / PASSWORD

PET_DOWN, PET_UP, PET_RIGHT, PET_LEFT, PET_DEL = 0x11, 0x91, 0x1D, 0x9D, 0x14
PET_CRSR = {PET_DOWN: "DOWN", PET_UP: "UP", PET_RIGHT: "RIGHT", PET_LEFT: "LEFT", PET_DEL: "BACKSPACE"}

PETSCII_TTYPES = ("PETSCII", "C64", "CBM", "C64TERM")


class Nav(Exception):
    """Raised to jump to a different screen/state."""
    def __init__(self, state):
        self.state = state


class Session:
    def __init__(self, app, reader, writer, ip):
        self.app = app
        self.cfg = app.cfg
        self.reader, self.writer, self.ip = reader, writer, ip
        self.cols = int(app.cfg.server_default_cols)
        self.rows = int(app.cfg.server_default_rows)
        self.term_mode = "ansi"
        self.petscii = False
        self.skipped = 0
        self.chosen = False
        self.naws_seen = False
        self.cpr_seen = False      # client answered our ESC[6n cursor-position probe (=> a real ANSI terminal)
        self.ttype_seen = ""
        self.color_mode = str(app.cfg.display_color_mode)
        self.presenter = Presenter(False, self.color_mode, rate=app.cfg.video_rate_limit_bytes,
                                    gap=app.cfg.video_delta_gap_merge, full_refresh=app.cfg.video_full_refresh_seconds)
        self.keys = deque()
        self.key_ev = asyncio.Event()
        self.closed = False
        self.inbuf = b""
        self.after_cr = False
        self.refused = set()
        self.username = None
        self.name_color = 1
        self.is_operator = False
        # preset login passed by the telnet client (NEW-ENVIRON USER + PASSWORD)
        self.preset_user = None
        self.preset_pass = None
        self.environ_will = False
        self.rtask = None
        self.room_id = None
        self.connected_at = time.monotonic()
        self.last_input_at = time.monotonic()
        # ZMODEM upload in progress: a tc_zmodem.ZmodemReceiver, or None. While set,
        # parse() hands it every byte (after undoing telnet IAC framing) instead of the
        # normal key parser; _zm_carry holds a trailing lone IAC across chunk boundaries.
        self.zmodem = None
        self._zm_carry = b""
        # The only telnet option bytes _parse_zmodem will ever trust after an IAC
        # WILL/WONT/DO/DONT/SB during a transfer - everything else is left as plain data.
        self._zm_known_opts = frozenset((OPT_BINARY, OPT_ECHO, OPT_SGA, OPT_TTYPE, OPT_NAWS, OPT_ENVIRON))
        self.rawlog = getattr(app, "rawlog", None) or NULL_RAWLOG
        self.rawlog.log(self.ip, "connect", "new session")

    # ---------------------------------------------------------- low level io
    def write_raw(self, b):
        try:
            self.writer.write(bytes(b))
        except Exception:
            self.closed = True

    async def flush(self):
        try:
            await asyncio.wait_for(self.writer.drain(), 30)
        except (asyncio.TimeoutError, Exception):
            self.closed = True

    def new_grid(self):
        return Grid(self.rows, self.cols)

    async def present(self, g, force=False):
        if self.closed:
            return
        if self.zmodem is not None:
            # While a ZMODEM transfer is attached the wire belongs to the protocol. Anything
            # else we send (a screen redraw, an escape sequence) lands in the middle of the
            # sender's parsing: SyncTERM answered exactly that with "HEX header not
            # terminated" / "INVALID HEADER". Suppress it - the caller redraws afterwards.
            self.rawlog.log(self.ip, "zmodem", "screen update suppressed during transfer")
            return
        if not force:
            try:
                pending = self.writer.transport.get_write_buffer_size()
            except Exception:
                pending = 0
            limit = int((self.cfg.video_commodore_rate_limit_bytes if self.petscii
                         else self.cfg.video_rate_limit_bytes) * 0.25)
            if pending > limit and self.skipped < 4:
                self.skipped += 1
                return
        self.skipped = 0
        data = self.presenter.encode(g, force=force)
        if data:
            self.write_raw(data)
            await self.flush()

    def set_petscii(self):
        self.term_mode = "petscii"
        self.petscii = True
        self.cols = int(self.cfg.server_commodore_cols)
        self.rows = int(self.cfg.server_commodore_rows)
        self.presenter = Presenter(True, "16", rate=self.cfg.video_commodore_rate_limit_bytes,
                                    gap=self.cfg.video_delta_gap_merge, full_refresh=self.cfg.video_full_refresh_seconds)
        self.rawlog.log(self.ip, "term", f"-> petscii (ttype={self.ttype_seen!r} naws_seen={self.naws_seen} cpr_seen={self.cpr_seen})")

    def set_ansi(self):
        self.term_mode = "ansi"
        self.petscii = False
        self.presenter = Presenter(False, self.color_mode, rate=self.cfg.video_rate_limit_bytes,
                                    gap=self.cfg.video_delta_gap_merge, full_refresh=self.cfg.video_full_refresh_seconds)
        self.rawlog.log(self.ip, "term", f"-> ansi (ttype={self.ttype_seen!r} naws_seen={self.naws_seen} cpr_seen={self.cpr_seen})")

    def clear_raw(self):
        if self.petscii:
            self.write_raw(bytes([0x93]))
        else:
            self.write_raw(b"\x1b[0m\x1b[2J\x1b[H")

    def uptime_seconds(self):
        return time.monotonic() - self.connected_at

    def idle_seconds(self):
        return time.monotonic() - self.last_input_at

    # ---------------------------------------------------------- telnet / keys
    def on_negotiate(self, cmd, opt):
        if cmd == WILL and opt == OPT_TTYPE:
            # ask for the terminal-type subnegotiation
            self.write_raw([IAC, SB, OPT_TTYPE, 1, IAC, SE])
            return
        if cmd == WILL and opt == OPT_ENVIRON:
            self.environ_will = True
            # SEND: VAR "USER", USERVAR "PASSWORD" / "PASS"
            self.write_raw([IAC, SB, OPT_ENVIRON, 1, 0] + list(b"USER") +
                           [3] + list(b"PASSWORD") + [3] + list(b"PASS") + [IAC, SE])
            return
        if cmd == DO and opt == OPT_NAWS:
            return
        offered = (OPT_BINARY, OPT_ECHO, OPT_SGA, OPT_TTYPE, OPT_NAWS, OPT_ENVIRON)
        if opt in offered:
            return
        if opt in self.refused:
            return
        self.refused.add(opt)
        if cmd == DO:
            self.write_raw([IAC, WONT, opt])
        elif cmd == WILL:
            self.write_raw([IAC, DONT, opt])

    def on_sb(self, d):
        if not d:
            return
        if d[0] == OPT_NAWS and len(d) >= 5 and self.term_mode != "petscii":
            w = (d[1] << 8) | d[2]
            h = (d[3] << 8) | d[4]
            if w > 0 and h > 0:
                self.naws_seen = True
                self.cols, self.rows = max(40, min(w, 240)), max(14, min(h, 100))
                self.presenter.reset()
        elif d[0] == OPT_ENVIRON and len(d) >= 2 and d[1] in (0, 2):   # IS / INFO
            self._parse_environ(d[2:])
        elif d[0] == OPT_TTYPE and len(d) >= 2 and d[1] == 0:
            try:
                self.ttype_seen = d[2:].decode("ascii", "replace").upper()
                self.rawlog.log(self.ip, "ttype", repr(self.ttype_seen))
            except Exception:
                pass

    def _parse_environ(self, body):
        """Decode NEW-ENVIRON VAR/USERVAR name + VALUE pairs; keep USER and the password."""
        found, name, val, state, esc = {}, bytearray(), bytearray(), None, False

        def flush():
            if state is not None and name:
                found[name.decode("ascii", "ignore").upper()] = val.decode("utf-8", "ignore")

        for b in body:
            if esc:
                (val if state == "val" else name).append(b)
                esc = False
            elif b == 2:
                esc = True
            elif b in (0, 3):
                flush()
                name, val, state = bytearray(), bytearray(), "name"
            elif b == 1:
                state = "val"
            else:
                (val if state == "val" else name).append(b)
        flush()
        user = (found.get("USER") or "").strip()
        pw = next((found[k] for k in ("PASSWORD", "PASS", "PWD") if found.get(k)), "")
        if user:
            self.preset_user = user
        if pw:
            self.preset_pass = pw

    def emit_key(self, k):
        if len(self.keys) < 128:
            self.keys.append((k, time.monotonic()))
            self.key_ev.set()

    def parse(self, data):
        if self.zmodem is not None:
            self._parse_zmodem(data)
            return
        self._parse_keys(data)

    def _parse_keys(self, data):
        if data:
            self.rawlog.log(self.ip, "recv", self.rawlog.hexdump(data))
        buf = self.inbuf + data
        i, n = 0, len(buf)
        while i < n:
            b = buf[i]
            if b == IAC:
                if i + 1 >= n:
                    break
                c = buf[i + 1]
                if c in (WILL, WONT, DO, DONT):
                    if i + 2 >= n:
                        break
                    self.on_negotiate(c, buf[i + 2])
                    i += 3
                elif c == SB:
                    j = buf.find(bytes([IAC, SE]), i + 2)
                    if j < 0:
                        break
                    self.on_sb(buf[i + 2:j])
                    i = j + 2
                else:
                    i += 2
                continue
            if b in PET_CRSR:
                # Recognised regardless of term_mode: these specific control bytes are
                # what a real C64's cursor/DEL keys send, and a regular ANSI keyboard
                # essentially never produces them by itself. Accepting them unconditionally
                # means that if terminal auto-detection ever guesses "ANSI" for what is
                # actually a PETSCII client, that client can still navigate (and reach the
                # "T" switch-terminal option, see page_choose_terminal) instead of being
                # stuck looking at undecipherable escape-code soup with no way to recover.
                if (b in (0x91, 0x9D) and self.term_mode == "ansi" and self.username is None
                        and (i == 0 or buf[i - 1] < 0x80)):
                    # UP / LEFT from a real C64 keyboard are high-bit bytes a 7-bit ANSI terminal
                    # never sends by itself: we were wrong about the terminal, switch to PETSCII.
                    self.set_petscii()
                    self.clear_raw()
                self.emit_key(PET_CRSR[b])
                i += 1
                self.after_cr = False
                continue
            if self.term_mode == "petscii":
                if 0xC1 <= b <= 0xDA:
                    self.emit_key(chr(b - 0x80))
                    i += 1
                    self.after_cr = False
                    continue
            if b == 27:
                if i + 1 >= n:
                    break
                c = buf[i + 1]
                if c in (ord("["), ord("O")):
                    j = i + 2
                    while j < n and not (0x40 <= buf[j] <= 0x7E):
                        j += 1
                    if j >= n:
                        break
                    fin = chr(buf[j])
                    k = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(fin)
                    if k:
                        self.emit_key(k)
                    elif fin == "R" and c == ord("["):
                        self.cpr_seen = True      # ESC [ row ; col R - reply to our ESC[6n probe
                    i = j + 1
                else:
                    self.emit_key("ESC")
                    i += 1
                self.after_cr = False
                continue
            i += 1
            if b in (13, 10):
                if b == 10 and self.after_cr:
                    self.after_cr = False
                    continue
                self.after_cr = (b == 13)
                self.emit_key("ENTER")
                continue
            if b == 0 and self.after_cr:
                self.after_cr = False
                continue
            self.after_cr = False
            if b in (8, 127):
                self.emit_key("BACKSPACE")
            elif b == 9:
                self.emit_key("TAB")
            elif b == 32:
                self.emit_key("SPACE")
            elif 32 < b < 127:
                self.emit_key(chr(b))
        self.inbuf = buf[i:]

    def _parse_zmodem(self, data):
        """While a ZMODEM transfer may be in progress, undo ONLY the telnet framing that can
        be recognised with certainty, and hand everything else to the ZmodemReceiver exactly
        as received.

        This used to also treat any IAC (0xFF) followed by a byte in the telnet-command range
        (WILL/WONT/DO/DONT/SB, or even just "anything else") as a 2-byte telnet command to
        swallow. That is wrong here: once telnet BINARY mode is negotiated (which TelChatter
        always does), a conforming client is free to send raw 8-bit data without doubling
        0xFF at all - IAC-escaping a literal data byte is strictly a non-binary-mode NVT
        convention - and ZMODEM frames are dense, close-to-uniform binary data, so 0xFF turns
        up constantly as real protocol bytes (CRC bytes, ZDLE-escaped data, ...). Swallowing
        "0xFF + the next byte" on sight corrupted the ZMODEM stream itself (bad header CRCs,
        dropped sub-packet bytes), not just file payloads - precisely the class of bug this
        switch to ZMODEM was meant to fix.

        So the only things ever stripped here are the cases a real telnet negotiation cannot
        be confused with real data:
          - IAC IAC: the one escaping a binary-mode client CAN be relied on for, so it is
            still undone (-> one literal 0xFF byte)
          - IAC SB ... IAC SE: a subnegotiation has its own unambiguous terminator
          - IAC WILL/WONT/DO/DONT <option>, but only for options TelChatter actually
            recognises (NAWS, TTYPE, ...) - an option byte that doesn't match anything real is
            left as plain data instead of being guessed away, since a real negotiation from a
            well-behaved client never offers an option this session hasn't already seen
        A lone IAC followed by anything else (including a byte that merely LOOKS like a
        telnet command) is passed straight through as the data byte it actually is.
        self._zm_carry holds a trailing lone IAC across chunk boundaries so a 0xFF split
        right at the edge of two reads is not misread as a command."""
        buf = self._zm_carry + data
        self._zm_carry = b""
        out = bytearray()
        i, n = 0, len(buf)
        while i < n:
            b = buf[i]
            if b != IAC:
                out.append(b)
                i += 1
                continue
            if i + 1 >= n:
                self._zm_carry = bytes(buf[i:])
                break
            c = buf[i + 1]
            if c == IAC:
                out.append(IAC)
                i += 2
                continue
            if c in (WILL, WONT, DO, DONT) and i + 2 < n and buf[i + 2] in self._zm_known_opts:
                self.on_negotiate(c, buf[i + 2])
                i += 3
                continue
            if c == SB and i + 2 < n and buf[i + 2] in self._zm_known_opts:
                j = buf.find(bytes([IAC, SE]), i + 2)
                if j < 0:
                    self._zm_carry = bytes(buf[i:])
                    break
                self.on_sb(buf[i + 2:j])
                i = j + 2
                continue
            # Not a recognisable telnet command: this IAC is real ZMODEM/binary data.
            out.append(b)
            i += 1
        if not out:
            return
        self.rawlog.log(self.ip, "zmodem-recv", self.rawlog.hexdump(bytes(out)))
        zm = self.zmodem
        if zm is None:
            return            # the transfer ended while we were mid-loop above
        stray = zm.feed(bytes(out))
        if stray:
            stray = stray.replace(b"rz\r", b"")     # sz's "start rz now" text is not typing
            # Bytes the receiver didn't recognise as part of the ZMODEM exchange - only
            # possible before a real transfer has started (e.g. the user pressed ESC to
            # back out while still on the "waiting for your terminal" screen). Route them
            # through the normal key parser so that still works.
            self._parse_keys(stray)

    async def start_zmodem(self, recv):
        """Attach `recv` (a tc_zmodem.ZmodemReceiver) so parse() starts feeding it bytes,
        and send its opening ZRINIT invitation."""
        self.zmodem = recv
        self.keys.clear()
        self.inbuf = b""
        self._zm_carry = b""
        self.rawlog.log(self.ip, "zmodem-start", "receiver attached")
        recv.start()
        await self.flush()

    def stop_zmodem(self):
        self.zmodem = None
        self._zm_carry = b""
        self.rawlog.log(self.ip, "zmodem-stop", "receiver detached")

    async def _read_loop(self):
        try:
            while True:
                lone_esc = self.inbuf == b"\x1b"
                try:
                    data = await asyncio.wait_for(self.reader.read(65536), 0.06 if lone_esc else None)
                except asyncio.TimeoutError:
                    self.inbuf = b""
                    self.emit_key("ESC")
                    continue
                if not data:
                    break
                self.parse(data)
        except (ConnectionError, OSError):
            pass
        finally:
            self.closed = True
            self.key_ev.set()

    def try_key(self):
        if self.keys:
            return self.keys.popleft()
        return None

    async def get_key(self, timeout=None):
        """Blocking: returns a bare key string, or None on timeout. Updates last_input_at
        on every real key so idle/session timeouts are measured correctly."""
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.keys:
                k, _t = self.keys.popleft()
                self.last_input_at = time.monotonic()
                return k
            if self.closed:
                raise ConnectionError("closed")
            rem = None if end is None else end - time.monotonic()
            if rem is not None and rem <= 0:
                return None
            self.key_ev.clear()
            try:
                await asyncio.wait_for(self.key_ev.wait(), 5 if rem is None else min(rem, 5))
            except asyncio.TimeoutError:
                pass

    # ---------------------------------------------------------- lifecycle
    async def run(self):
        self.write_raw([IAC, WILL, OPT_ECHO, IAC, WILL, OPT_SGA, IAC, WILL, OPT_BINARY, IAC, DO, OPT_BINARY,
                         IAC, DO, OPT_NAWS, IAC, DO, OPT_TTYPE, IAC, DO, OPT_ENVIRON])
        await self.flush()
        self.rtask = asyncio.ensure_future(self._read_loop())
        await asyncio.sleep(float(self.cfg.server_negotiate_wait))
        state = ("auto_detect",)
        while True:
            try:
                fn = STATE_TABLE[state[0]]
                state = await fn(self, *state[1:])
            except Nav as nv:
                state = nv.state
            except ConnectionError:
                raise
            except Exception:
                import logging
                logging.getLogger("telchatter").exception("[%s] session error", self.ip)
                state = ("choose_terminal",) if not self.chosen else ("home",)

    async def close(self):
        if self.rtask:
            self.rtask.cancel()
        try:
            self.clear_raw()
            await asyncio.wait_for(self.writer.drain(), 2)
        except Exception:
            pass
        try:
            self.writer.close()
        except Exception:
            pass


# STATE_TABLE is filled in by tc_screens (avoids a circular import at module load)
STATE_TABLE = {}
