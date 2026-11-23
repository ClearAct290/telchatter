# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# TelChatter - Copyright (C) 2026 ClearAct290-Reddit
"""
TelChatter - ZMODEM receiver (client -> server file upload).

Pasting a binary file into a terminal as "ASCII input" is not reliable: the client may
rewrite line endings (LF -> CRLF breaks a PNG's signature), stop partway through, or
mangle bytes it considers control codes. ZMODEM is the classic BBS answer: every block
is length-delimited and CRC-checked, a damaged block is re-requested instead of being
silently accepted, and the sender (SyncTERM, Qodem, lrzsz ``sz``, ...) shows its own
progress dialog, so no input lock is needed on our side.

This module is only the protocol state machine - bytes in, bytes out, no sockets and no
asyncio - so it can be unit-tested against a sender directly:

    zm = ZmodemReceiver(send=write_to_client)
    zm.start()                      # announces itself with ZRINIT
    stray = zm.feed(bytes_from_client)
    ...
    zm.files   -> [(filename, data)] once zm.finished is True

Telnet framing (IAC IAC -> 0xFF) must already have been removed from what is fed in; it
is the session's job (tc_session.py). Only the receiving direction is implemented, with
CRC-16 and CRC-32 frames, streaming (ZCRCG/ZCRCQ/ZCRCW/ZCRCE) and ZRPOS error recovery.
Crash recovery (resuming an earlier partial file) is deliberately not supported: every
file is received from byte 0. Only the first file of a batch is kept; the rest are
skipped with ZSKIP.
"""
import binascii
import time
import zlib

ZPAD, ZDLE = 0x2A, 0x18
ZBIN, ZHEX, ZBIN32 = 0x41, 0x42, 0x43

(ZRQINIT, ZRINIT, ZSINIT, ZACK, ZFILE, ZSKIP, ZNAK, ZABORT, ZFIN, ZRPOS, ZDATA, ZEOF, ZFERR,
 ZCRC, ZCHALLENGE, ZCOMPL, ZCAN, ZFREECNT, ZCOMMAND, ZSTDERR) = range(20)

FRAME_NAMES = ("ZRQINIT", "ZRINIT", "ZSINIT", "ZACK", "ZFILE", "ZSKIP", "ZNAK", "ZABORT", "ZFIN",
               "ZRPOS", "ZDATA", "ZEOF", "ZFERR", "ZCRC", "ZCHALLENGE", "ZCOMPL", "ZCAN",
               "ZFREECNT", "ZCOMMAND", "ZSTDERR")

# data sub-packet terminators (sent after a ZDLE)
ZCRCE, ZCRCG, ZCRCQ, ZCRCW = 0x68, 0x69, 0x6A, 0x6B
_TERMS = frozenset((ZCRCE, ZCRCG, ZCRCQ, ZCRCW))

# capability flags in the last byte of a ZRINIT header
CANFDX, CANOVIO, CANBRK, CANFC32, ESCCTL = 0x01, 0x02, 0x04, 0x20, 0x40

_XOFF = bytes((0x11, 0x13, 0x91, 0x93))      # software flow control, ignored if not escaped
_XOFF_SET = frozenset(_XOFF)
CAN5 = b"\x18" * 5
CANIT = b"\x18" * 8 + b"\x08" * 8            # "abort the transfer" string senders understand
MAX_SUBPACKET = 1 << 20                      # sanity bound on one sub-packet (normal: 1-8 KiB)


def hex_header(ftype, data=0):
    """A ZHEX header: ``**<ZDLE>B`` + 14 lowercase hex digits (type, 4 data bytes, CRC-16)
    + CR + LF|0x80 (+ XON, except after ZFIN/ZACK), exactly as lrzsz writes them."""
    if isinstance(data, int):
        data = (data & 0xFFFFFFFF).to_bytes(4, "little")
    body = bytes([ftype]) + bytes(data)
    crc = binascii.crc_hqx(body, 0)
    out = b"**\x18B" + binascii.hexlify(body + crc.to_bytes(2, "big")) + b"\r\x8a"
    if ftype not in (ZFIN, ZACK):
        out += b"\x11"
    return out


def _unesc(c):
    """The data byte that `c` (the byte after a ZDLE) stands for, or None if illegal."""
    if c == 0x6C:
        return 0x7F
    if c == 0x6D:
        return 0xFF
    if (c & 0x60) == 0x40:
        return c ^ 0x40
    return None


class _Bad(Exception):
    pass


def _read_escaped(buf, i, count):
    """Decode `count` ZDLE-escaped bytes from buf[i:] -> (bytes, next_i), or None when the
    buffer ends first. Unescaped XON/XOFF bytes (a flow-control layer may inject them) are
    skipped. Raises _Bad on an impossible escape sequence."""
    out = bytearray()
    n = len(buf)
    while len(out) < count:
        if i >= n:
            return None
        c = buf[i]
        i += 1
        if c == ZDLE:
            if i >= n:
                return None
            v = _unesc(buf[i])
            i += 1
            if v is None:
                raise _Bad()
            out.append(v)
        elif c not in _XOFF_SET:
            out.append(c)
    return bytes(out), i


def clean_filename(raw):
    """Keep only a harmless display name from whatever the sender announced."""
    name = raw.decode("latin-1", "replace").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if 32 <= ord(ch) < 127).strip()
    return name[:64] or "upload"


class ZmodemReceiver:
    """One upload session. See the module docstring for the call pattern.

    Public state: ``started`` (a ZMODEM header has been seen), ``finished`` (ZFIN
    exchanged), ``failed`` (reason string if the transfer was aborted), ``files``
    (completed [(name, bytes)]), ``error`` (soft problem, e.g. file too large),
    ``over_and_out`` (the sender's closing "OO" arrived), ``last_rx`` (monotonic time of
    the last byte received) and ``progress`` (bytes of the current file so far)."""

    def __init__(self, send, max_bytes=8_000_000, escape_ctrl=False, log=None,
                 zrinit_interval=4.0, clock=time.monotonic):
        self._send = send
        self.max_bytes = int(max_bytes)
        self.escape_ctrl = bool(escape_ctrl)
        self._logf = log or (lambda msg: None)
        self.zrinit_interval = float(zrinit_interval)
        self.clock = clock

        self.buf = bytearray()
        self.mode = "hdr"                 # "hdr": looking for a header; "sub": inside data sub-packets
        self.started = False
        self.finished = False
        self.failed = ""
        self.error = ""
        self.over_and_out = False
        self.files = []
        self.cur = None                   # {"name", "size", "data"} of the file being received
        self.errors = 0
        self.last_rx = clock()
        self._last_zrinit = -1e9
        self._last_zrpos = -1e9
        self._can_tail = b""
        self._next_progress = 65536
        self._reset_sub()

    # ------------------------------------------------------------------ small helpers
    def _log(self, msg):
        try:
            self._logf(msg)
        except Exception:
            pass

    def _tx(self, ftype, data=0, note=""):
        self._log(f"tx {FRAME_NAMES[ftype]} {data if isinstance(data, int) else bytes(data).hex()}{note}")
        self._send(hex_header(ftype, data))

    def _send_zrinit(self):
        flags = CANFDX | CANOVIO | CANFC32 | (ESCCTL if self.escape_ctrl else 0)
        self._last_zrinit = self.clock()
        self._tx(ZRINIT, bytes((0, 0, 0, flags)))

    def _zrpos(self, force=False):
        now = self.clock()
        if not force and now - self._last_zrpos < 0.5:
            return
        self._last_zrpos = now
        self._tx(ZRPOS, len(self.cur["data"]) if self.cur else 0)

    def _reset_sub(self):
        self._kind = None                 # "file" | "data" | "sinit"
        self._crc_len = 2
        self._sd = bytearray()
        self._crc = bytearray()
        self._term = None
        self._zdle = False

    def _begin_sub(self, kind, fmt):
        self._reset_sub()
        self._kind = kind
        self._crc_len = 4 if fmt == ZBIN32 else 2
        self.mode = "sub"

    @property
    def done(self):
        return self.finished or bool(self.failed)

    @property
    def progress(self):
        return len(self.cur["data"]) if self.cur else (len(self.files[0][1]) if self.files else 0)

    # ------------------------------------------------------------------ public API
    def start(self):
        """Announce readiness (ZRINIT). A terminal that auto-detects ZMODEM may open its
        own upload dialog when it sees this."""
        self._send_zrinit()

    def poll(self):
        """Call every so often: repeats ZRINIT while the sender has not started yet."""
        if not self.started and not self.done and self.clock() - self._last_zrinit >= self.zrinit_interval:
            self._send_zrinit()

    def cancel(self, reason="cancelled"):
        """Abort from our side (user pressed ESC, timeout, ...). Tells the sender to stop."""
        if not self.done:
            self._abort(reason)

    def feed(self, data):
        """Process bytes from the client. Returns the bytes that were NOT part of the
        ZMODEM conversation while no transfer had started (i.e. ordinary keystrokes such
        as ESC), for the caller to treat as typed input."""
        if not data:
            return b""
        self.last_rx = self.clock()
        probe = self._can_tail + bytes(data)
        self._can_tail = probe[-4:]
        if CAN5 in probe and not self.finished:
            self._log("rx 5x CAN (sender cancelled)")
            self.failed = self.failed or "cancelled by your terminal"
            self.buf.clear()
            return b""
        if self.finished:
            if b"OO" in probe:
                self.over_and_out = True
            return b""
        if self.failed:
            return b""
        self.buf += data
        stray = bytearray()
        while self.buf and not self.done:
            progress = self._scan_header(stray) if self.mode == "hdr" else self._read_subpacket()
            if not progress:
                break
        return bytes(stray)

    # ------------------------------------------------------------------ headers
    def _noise(self, chunk, stray):
        if not self.started:
            stray += chunk

    def _scan_header(self, stray):
        buf = self.buf
        p = buf.find(b"*")
        if p < 0:
            self._noise(buf, stray)
            buf.clear()
            return False
        if p:
            self._noise(buf[:p], stray)
            del buf[:p]
        n = len(buf)
        q = 0
        while q < n and buf[q] == ZPAD:
            q += 1
        if q >= n:
            return False
        if buf[q] != ZDLE:                         # a lone '*' keystroke / line noise
            self._noise(buf[:q], stray)
            del buf[:q]
            return True
        if q + 1 >= n:
            return False
        fmt = buf[q + 1]
        start = q + 2
        if fmt == ZHEX:
            if n < start + 14:
                return False
            try:
                raw = binascii.unhexlify(bytes(buf[start:start + 14]))
            except (binascii.Error, ValueError):
                del buf[:start]
                return True
            body, got = raw[:5], int.from_bytes(raw[5:], "big")
            if binascii.crc_hqx(body, 0) != got:
                self._log("rx hex header CRC mismatch (ignored)")
                del buf[:start]
                return True
            del buf[:start + 14]
            self._header(body[0], bytes(body[1:5]), ZHEX)
            return True
        if fmt in (ZBIN, ZBIN32):
            clen = 2 if fmt == ZBIN else 4
            try:
                res = _read_escaped(buf, start, 5 + clen)
            except _Bad:
                del buf[:start]
                return True
            if res is None:
                return False
            data, end = res
            body, tail = data[:5], data[5:]
            if fmt == ZBIN:
                ok = binascii.crc_hqx(body, 0) == int.from_bytes(tail, "big")
            else:
                ok = zlib.crc32(body) == int.from_bytes(tail, "little")
            if not ok:
                self._log("rx binary header CRC mismatch (ignored)")
                del buf[:start]
                return True
            del buf[:end]
            self._header(body[0], body[1:5], fmt)
            return True
        del buf[:q + 1]                            # '*' + ZDLE + something unknown
        return True

    def _header(self, ftype, d4, fmt):
        self.started = True
        pos = int.from_bytes(d4, "little")
        name = FRAME_NAMES[ftype] if ftype < len(FRAME_NAMES) else f"?{ftype}"
        self._log(f"rx {name} pos={pos} fmt={chr(fmt)}")
        if ftype == ZRQINIT:
            self._send_zrinit()
        elif ftype == ZSINIT:
            self._begin_sub("sinit", fmt)
        elif ftype == ZFILE:
            self._begin_sub("file", fmt)
        elif ftype == ZDATA:
            if self.cur is None:
                self._log("ZDATA with no file open (ignored)")
            elif pos != len(self.cur["data"]):
                self._log(f"ZDATA at {pos} but we have {len(self.cur['data'])}")
                self._zrpos()
            else:
                self._begin_sub("data", fmt)
        elif ftype == ZEOF:
            self._on_eof(pos)
        elif ftype == ZFIN:
            self._tx(ZFIN, 0)
            self.finished = True
            self.mode = "hdr"
            self.buf.clear()
            if self.cur is not None and not self.files:
                self.error = self.error or "transfer ended before the file was complete"
        elif ftype == ZCHALLENGE:
            self._tx(ZACK, pos)
        elif ftype == ZFREECNT:
            self._tx(ZACK, 0)
        elif ftype in (ZABORT, ZCAN, ZFERR):
            self._abort("cancelled by your terminal" if ftype != ZFERR else "your terminal reported a file error",
                        send_can=False)
        elif ftype == ZCOMMAND:
            self._abort("unsupported request from your terminal")
        # ZNAK / ZACK / ZRPOS / ZSKIP / ZRINIT / ... are receiver-side frames: nothing to do

    def _abort(self, reason, send_can=True):
        self.failed = reason
        self.mode = "hdr"
        self.buf.clear()
        self._log(f"abort: {reason}")
        if send_can:
            self._send(CANIT)

    # ------------------------------------------------------------------ sub-packets
    def _sub_bad(self, why):
        kind = self._kind
        self.errors += 1
        self._log(f"bad {kind} sub-packet: {why} (error #{self.errors})")
        self._reset_sub()
        self.mode = "hdr"
        if self.errors > 40:
            self._abort("too many transmission errors")
        elif kind == "data" and self.cur is not None:
            self._zrpos(force=True)
        elif kind in ("file", "sinit"):
            self._tx(ZNAK, 0)

    def _read_subpacket(self):
        buf = self.buf
        i, n = 0, len(buf)
        sd = self._sd
        while i < n:
            if self._term is None:
                if self._zdle:
                    c = buf[i]
                    i += 1
                    self._zdle = False
                    if c in _TERMS:
                        self._term = c
                        continue
                    v = _unesc(c)
                    if v is None:
                        del buf[:i]
                        self._sub_bad("illegal escape")
                        return True
                    sd.append(v)
                    continue
                j = buf.find(b"\x18", i)
                run = buf[i:j if j >= 0 else n]
                if run:
                    sd += run.translate(None, _XOFF)
                if len(sd) > MAX_SUBPACKET:
                    del buf[:n]
                    self._sub_bad("sub-packet too long")
                    return True
                if j < 0:
                    i = n
                    break
                i = j + 1
                self._zdle = True
            else:
                c = buf[i]
                i += 1
                if self._zdle:
                    self._zdle = False
                    v = _unesc(c)
                    if v is None:
                        del buf[:i]
                        self._sub_bad("illegal escape in CRC")
                        return True
                    self._crc.append(v)
                elif c == ZDLE:
                    self._zdle = True
                    continue
                elif c in _XOFF_SET:
                    continue
                else:
                    self._crc.append(c)
                if len(self._crc) == self._crc_len:
                    del buf[:i]
                    self._finish_subpacket()
                    return True
        del buf[:i]
        return False

    def _finish_subpacket(self):
        term, data, got = self._term, bytes(self._sd), bytes(self._crc)
        kind, crc_len = self._kind, self._crc_len
        if crc_len == 2:
            want = binascii.crc_hqx(bytes([term]), binascii.crc_hqx(data, 0)).to_bytes(2, "big")
        else:
            want = zlib.crc32(bytes([term]), zlib.crc32(data)).to_bytes(4, "little")
        if got != want:
            self._sub_bad(f"CRC mismatch ({len(data)} bytes)")
            return
        self._reset_sub()
        self.mode = "hdr"
        if kind == "sinit":
            self._tx(ZACK, 1)
        elif kind == "file":
            self._on_zfile(data)
        elif kind == "data":
            self._on_data(data, term, crc_len)

    # ------------------------------------------------------------------ file handling
    def _on_zfile(self, data):
        parts = data.split(b"\x00", 1)
        name = clean_filename(parts[0])
        info = parts[1].split(b"\x00", 1)[0].decode("ascii", "ignore").split() if len(parts) > 1 else []
        size = int(info[0]) if info and info[0].isdigit() else None
        self._log(f"file offered: name={name!r} size={size}")
        if self.cur is not None and self.cur["name"] == name:
            self._zrpos(force=True)                 # the sender repeated ZFILE: our ZRPOS was lost
        elif self.cur is not None or self.files:
            self._log("extra file skipped (one image per upload)")
            self._tx(ZSKIP, 0)
        elif size is not None and size > self.max_bytes:
            self.error = f"file too large ({size} bytes, limit {self.max_bytes})"
            self._log(self.error)
            self._tx(ZSKIP, 0)
        else:
            self.cur = {"name": name, "size": size, "data": bytearray()}
            self._next_progress = 65536
            self._tx(ZRPOS, 0)

    def _on_data(self, data, term, crc_len):
        cur = self.cur
        cur["data"] += data
        have = len(cur["data"])
        if have > self.max_bytes:
            self.error = f"file too large (limit {self.max_bytes} bytes)"
            self._abort(self.error)
            return
        if have >= self._next_progress:
            self._log(f"progress {have} of {cur['size']} bytes")
            self._next_progress = have + 65536
        if term in (ZCRCQ, ZCRCW):
            self._tx(ZACK, have)
        if term in (ZCRCG, ZCRCQ):                  # more sub-packets follow with no header between
            self._begin_sub("data", ZBIN32 if crc_len == 4 else ZBIN)

    def _on_eof(self, pos):
        cur = self.cur
        if cur is None:                              # duplicate ZEOF after we finished: just re-invite
            self._send_zrinit()
        elif pos == len(cur["data"]):
            self.files.append((cur["name"], bytes(cur["data"])))
            self._log(f"file complete: {cur['name']!r} {pos} bytes")
            self.cur = None
            self._send_zrinit()
        else:
            self._log(f"ZEOF at {pos} but we have {len(cur['data'])}")
            self._zrpos()
