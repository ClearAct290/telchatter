# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""Test helper: act as a ZMODEM-capable telnet terminal (what SyncTERM does when you pick
Upload -> ZModem) on an already-connected socket.

    zmodem_send(c, [("pic.png", data)], crc32=True, escape_all=True)

`c` is a test client with an `.s` socket. Everything the server sends is un-framed
(IAC IAC -> 0xFF, negotiations dropped) and handed to the ZSender; its replies go back
IAC-doubled, exactly as a standards-following telnet client in binary mode would."""
import socket
import time

from zm_sender import ZSender

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240


def telnet_escape(b):
    return b.replace(b"\xff", b"\xff\xff")


class Unframer:
    """Streaming telnet un-framer (keeps a split IAC sequence across reads)."""
    def __init__(self):
        self.carry = b""

    def feed(self, data):
        buf, self.carry = self.carry + data, b""
        out, i, n = bytearray(), 0, len(buf)
        while i < n:
            b = buf[i]
            if b != IAC:
                out.append(b); i += 1; continue
            if i + 1 >= n:
                self.carry = buf[i:]; break
            c = buf[i + 1]
            if c == IAC:
                out.append(IAC); i += 2
            elif c in (WILL, WONT, DO, DONT):
                if i + 2 >= n:
                    self.carry = buf[i:]; break
                i += 3
            elif c == SB:
                j = buf.find(bytes([IAC, SE]), i + 2)
                if j < 0:
                    self.carry = buf[i:]; break
                i = j + 2
            else:
                i += 2
        return bytes(out)


def zmodem_send(c, files, timeout=40.0, after_start=None, stop_after_bytes=None,
                 raw_binary=False, **sender_kw):
    """Run a whole ZMODEM send. Returns the ZSender (check .done / .aborted / .log).
    `stop_after_bytes`: stop answering after the receiver has been sent that many bytes of
    file data (used to simulate a terminal that vanishes mid-transfer).
    `raw_binary`: send bytes exactly as the ZMODEM sender produced them, with NO telnet
    IAC-doubling. RFC 854/856 require doubling even in binary mode, so a conforming client
    never does this - it exists to prove the server tolerates a client that gets it wrong
    (an un-doubled 0xFF followed by an ordinary byte must stay data).
    The returned sender has `.server_bytes`: everything the server sent while the transfer
    ran, un-framed, so a test can check that only ZMODEM frames were on the wire."""
    zs = ZSender(files, **sender_kw)
    zs.server_bytes = bytearray()
    un = Unframer()
    wrap = (lambda b: b) if raw_binary else telnet_escape
    first = zs.start()
    if first:
        c.s.sendall(wrap(first))
    if after_start:
        after_start(zs)
    t0 = time.time()
    sent_total = 0
    c.s.settimeout(0.2)
    while time.time() - t0 < timeout and not (zs.done or zs.aborted):
        try:
            data = c.s.recv(65536)
        except socket.timeout:
            continue
        if not data:
            break
        clean = un.feed(data)
        zs.server_bytes += clean
        reply = zs.feed(clean)
        if reply:
            c.s.sendall(wrap(reply))
            sent_total += len(reply)
            if stop_after_bytes is not None and sent_total >= stop_after_bytes:
                break
    c.s.settimeout(0.25)
    return zs
