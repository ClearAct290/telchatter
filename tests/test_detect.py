#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Terminal auto-detection. The bug: a real C64 got ANSI escape codes because a window-size
reply or an 'ansi' terminal name was taken as proof of an ANSI terminal."""
import socket, time
from test_upload import IAC, SB, SE, WILL, strip_ansi

TTYPE, NAWS, BINARY, SGA = 24, 31, 0, 3
TRUECOLOR = b"\x1b[38;2"


def open_conn(ttype=None, naws=None):
    s = socket.create_connection(("127.0.0.1", 2323), timeout=10)
    s.settimeout(5)
    s.recv(65536)
    s.settimeout(0.25)
    out = bytearray([IAC, WILL, BINARY, IAC, WILL, SGA])
    if naws:
        out += bytes([IAC, WILL, NAWS, IAC, SB, NAWS, 0, naws[0], 0, naws[1], IAC, SE])
    if ttype:
        out += bytes([IAC, WILL, TTYPE, IAC, SB, TTYPE, 0]) + ttype.encode() + bytes([IAC, SE])
    s.sendall(bytes(out))
    return s


def read(s, secs):
    raw = b""; t0 = time.time()
    while time.time() - t0 < secs:
        try:
            raw += s.recv(65536)
        except socket.timeout:
            pass
    return raw


def main():
    s = open_conn(ttype="C64", naws=(40, 25))
    data = read(s, 2.5)
    assert b"\x93" in data and len(data) > 200, f"expected PETSCII screen, got {data[:80]!r}"
    assert TRUECOLOR not in data and b"\x1b[6n" not in data and b"\x1b[2J" not in data
    print("[OK] A: TTYPE 'C64' (even with NAWS 40x25) -> PETSCII, no ANSI escape codes sent")
    s.close(); time.sleep(0.3)

    s = open_conn(ttype="ANSI", naws=(100, 35))
    data = read(s, 3.0)
    text = strip_ansi(data)
    assert b"\x1b[6n" in data, "server should probe the terminal with a cursor-position request"
    assert "Generic ASCII" in text and "C64 PETSCII" in text, f"expected the plain 1/2 menu:\n{text}"
    assert TRUECOLOR not in data and b"\x1b[2J" not in data
    print("[OK] B: unverified 'ANSI' client gets the plain-text 1/2 menu, not escape codes")
    s.sendall(b"2")
    data = read(s, 2.0)
    assert b"\x93" in data and TRUECOLOR not in data
    print("[OK] B: choosing 2 switches to PETSCII cleanly")
    s.close(); time.sleep(0.3)

    s = open_conn(ttype="xterm-256color", naws=(100, 35))
    seen = b""; t0 = time.time()
    while b"\x1b[6n" not in seen and time.time() - t0 < 3:
        try: seen += s.recv(65536)
        except socket.timeout: pass
    s.sendall(b"\x1b[12;40R")
    data = read(s, 1.5)
    assert TRUECOLOR in data and "WHAT WOULD YOU LIKE" in strip_ansi(data).upper(), strip_ansi(data)[:300]
    print("[OK] C: a terminal that answers the cursor probe is auto-detected as ANSI")
    s.sendall(bytes([0x9D]))
    data = read(s, 1.5)
    assert b"\x93" in data, "C64 cursor byte before login should flip the session to PETSCII"
    print("[OK] D: a C64 cursor key on a mis-detected session switches it to PETSCII")
    s.close()
    print("\n================ DETECTION TESTS PASSED ================")


if __name__ == "__main__":
    main()
