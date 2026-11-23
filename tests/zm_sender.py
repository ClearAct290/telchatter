# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""A small ZMODEM *sender* used only by the tests: it plays the part of SyncTERM / lrzsz
``sz`` so the receiver in tc_zmodem.py can be exercised end to end.

    s = ZSender([("pic.png", data)], crc32=True, escape_all=True, style="stream")
    out = s.start()                 # bytes to send first ("rz\\r" + ZRQINIT, like sz)
    out = s.feed(bytes_from_server) # reply to whatever the receiver said
    s.done / s.aborted

Options
  crc32       use ZBIN32 headers/sub-packets (CRC-32) instead of ZBIN (CRC-16)
  escape_all  escape every control character (what a ZRINIT with ESCCTL asks for)
  style       "stream": ZCRCG blocks + a final ZCRCE, ZEOF right away (lrzsz default)
              "q":      like stream, but every 4th block ends in ZCRCQ (ack requested)
              "w":      every block ends in ZCRCW and waits for the ZACK (windowed)
  corrupt_pos damage the sub-packet that starts at this file offset, once
  eager       send ZRQINIT first (sz does); False = wait for the receiver's ZRINIT
"""
import binascii
import re
import zlib

ZPAD, ZDLE = 0x2A, 0x18
ZBIN, ZHEX, ZBIN32 = 0x41, 0x42, 0x43
(ZRQINIT, ZRINIT, ZSINIT, ZACK, ZFILE, ZSKIP, ZNAK, ZABORT, ZFIN, ZRPOS, ZDATA, ZEOF) = range(12)
ZCRCE, ZCRCG, ZCRCQ, ZCRCW = 0x68, 0x69, 0x6A, 0x6B

_ALWAYS = (0x18, 0x10, 0x90, 0x11, 0x91, 0x13, 0x93)
_HDR_RE = re.compile(rb"\*\*\x18B([0-9a-fA-F]{14})")


def hex_hdr(t, pos=0):
    body = bytes([t]) + (pos & 0xFFFFFFFF).to_bytes(4, "little")
    crc = binascii.crc_hqx(body, 0)
    return b"**\x18B" + binascii.hexlify(body + crc.to_bytes(2, "big")) + b"\r\x8a" + (b"" if t in (ZFIN, ZACK) else b"\x11")


class ZSender:
    def __init__(self, files, crc32=True, escape_all=False, block=1024, style="stream",
                 corrupt_pos=None, eager=True, rz_prefix=True):
        self.files = list(files)
        self.crc32, self.escape_all, self.block, self.style = crc32, escape_all, block, style
        self.corrupt_pos, self.eager, self.rz_prefix = corrupt_pos, eager, rz_prefix
        self.idx = 0
        self.state = "init"
        self.done = False
        self.aborted = False
        self.skipped = []
        self.acked = 0
        self._corrupted = False
        self._rx = b""
        self._pos = 0
        self.log = []

    # ---------------------------------------------------------------- encoding
    def escape(self, data):
        table = getattr(self, "_table", None)
        if table is None:
            table = []
            for c in range(256):
                esc = c in _ALWAYS or (self.escape_all and (c & 0x60) == 0)
                table.append(bytes([ZDLE, c ^ 0x40]) if esc else bytes([c]))
            self._table = table
        out = b"".join(map(table.__getitem__, data))
        return re.sub(rb"@\r", b"@\x18M", out)          # CR after '@' is always escaped

    def bin_hdr(self, t, pos=0):
        body = bytes([t]) + (pos & 0xFFFFFFFF).to_bytes(4, "little")
        if self.crc32:
            tail = zlib.crc32(body).to_bytes(4, "little")
        else:
            tail = binascii.crc_hqx(body, 0).to_bytes(2, "big")
        return bytes([ZPAD, ZDLE, ZBIN32 if self.crc32 else ZBIN]) + self.escape(body + tail)

    def sub(self, data, term, pos=None):
        if self.crc32:
            crc = zlib.crc32(bytes([term]), zlib.crc32(data)).to_bytes(4, "little")
        else:
            crc = binascii.crc_hqx(bytes([term]), binascii.crc_hqx(data, 0)).to_bytes(2, "big")
        out = bytearray(self.escape(data) + bytes([ZDLE, term]) + self.escape(crc))
        if pos is not None and pos == self.corrupt_pos and not self._corrupted and len(out) > 4:
            self._corrupted = True
            out[len(out) // 2] ^= 0x01
            self.log.append(f"corrupted sub-packet at {pos}")
        return bytes(out)

    # ---------------------------------------------------------------- flow
    def start(self):
        self.state = "wait_rinit"
        if not self.eager:
            return b""
        return (b"rz\r" if self.rz_prefix else b"") + hex_hdr(ZRQINIT)

    def _zfile(self):
        name, data = self.files[self.idx]
        info = name.encode() + b"\x00" + f"{len(data)} 0 100644 0 {len(self.files) - self.idx} {sum(len(d) for _, d in self.files[self.idx:])}".encode() + b"\x00"
        self.state = "wait_rpos"
        return self.bin_hdr(ZFILE, 0) + self.sub(info, ZCRCW)

    def _data_from(self, pos):
        """Frames carrying the current file from `pos` on."""
        data = self.files[self.idx][1]
        self._pos = pos
        if self.style == "w":
            return self._w_block()
        out = bytearray(self.bin_hdr(ZDATA, pos))
        n = 0
        while True:
            chunk = data[pos:pos + self.block]
            last = pos + len(chunk) >= len(data)
            if last:
                term = ZCRCE
            elif self.style == "q" and (n % 4 == 3):
                term = ZCRCQ
            else:
                term = ZCRCG
            out += self.sub(chunk, term, pos)
            pos += len(chunk)
            n += 1
            if last:
                break
        out += self.bin_hdr(ZEOF, len(data))
        self.state = "wait_rinit_after_eof"
        return bytes(out)

    def _w_block(self):
        data = self.files[self.idx][1]
        pos = self._pos
        if pos >= len(data):
            self.state = "wait_rinit_after_eof"
            return self.bin_hdr(ZEOF, len(data))
        chunk = data[pos:pos + self.block]
        self.state = "wait_ack"
        return self.bin_hdr(ZDATA, pos) + self.sub(chunk, ZCRCW, pos)

    def _next_file_or_fin(self):
        if self.idx < len(self.files):
            return self._zfile()
        self.state = "wait_fin"
        return hex_hdr(ZFIN)

    def feed(self, data):
        if b"\x18" * 5 in data:
            self.aborted = True
            return b""
        self._rx += data
        out = b""
        while True:
            m = _HDR_RE.search(self._rx)
            if not m:
                self._rx = self._rx[-64:]
                break
            raw = binascii.unhexlify(m.group(1))
            self._rx = self._rx[m.end():]
            t, pos = raw[0], int.from_bytes(raw[1:5], "little")
            assert binascii.crc_hqx(raw[:5], 0) == int.from_bytes(raw[5:], "big"), "receiver sent a header with a bad CRC"
            self.log.append(f"rx type={t} pos={pos}")
            out += self._on(t, pos)
        return out

    def _on(self, t, pos):
        if t == ZRINIT:
            if self.state == "wait_rinit":
                return self._next_file_or_fin()
            if self.state == "wait_rinit_after_eof":
                self.idx += 1
                return self._next_file_or_fin()
            return b""
        if t == ZRPOS:
            if self.state in ("wait_rpos", "wait_rinit_after_eof", "wait_ack"):
                return self._data_from(pos)
            return b""
        if t == ZACK:
            if self.state == "wait_ack":
                self.acked = pos
                self._pos = pos
                return self._w_block()
            return b""
        if t == ZSKIP:
            self.skipped.append(self.files[self.idx][0])
            self.idx += 1
            return self._next_file_or_fin()
        if t == ZNAK and self.state == "wait_rpos":
            return self._zfile()
        if t == ZFIN and self.state == "wait_fin":
            self.done = True
            return b"OO"
        return b""
