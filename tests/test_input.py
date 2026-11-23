#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Typing + scrolling keys: '[' and ']' are ordinary characters while typing (and in passwords), but
scroll when you are not typing. Also: lower-case q quits, the space bar types a space."""
import socket, sqlite3, sys, time, uuid
from test_preset import connect, read


def last_body(name):
    return sqlite3.connect("telchatter.db").execute(
        "select body from messages where username=? order by id desc limit 1", (name,)).fetchone()[0]


def closed_within(s, secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if s.recv(65536) == b"":
                return True
        except socket.timeout:
            pass
        except OSError:
            return True
    return False


def main():
    name = "inp" + uuid.uuid4().hex[:6]
    s = connect(name, "pa[ss]99")
    t = read(s, 2.5)
    assert "README" in t.upper() or "WELCOME" in t.upper(), t[:300]
    assert "TelChatter" in t
    print("[OK] password containing [ ] accepted; home shows the readme.txt branding")
    s.sendall(b"\x1b[B"); read(s, 1.0)
    s.sendall(b"a[b]c"); read(s, 0.6)
    s.sendall(b"\r\n"); read(s, 1.0)
    assert last_body(name) == "a[b]c", last_body(name)
    print("[OK] typed '[' and ']' land in the message text: 'a[b]c'")
    s.sendall(b"[[[]]]"); read(s, 1.0)
    s.sendall(b"zz"); read(s, 0.5)
    s.sendall(b"\r\n"); read(s, 1.0)
    assert last_body(name) == "zz", last_body(name)
    print("[OK] [ and ] scroll (not typed) when the box is empty")
    s.close(); time.sleep(0.3)
    more_tests()
    print("\n================ INPUT TESTS PASSED ================")


def more_tests():
    name = "spc" + uuid.uuid4().hex[:6]
    s = connect(name, "secret99"); read(s, 2.5)
    s.sendall(b"\x1b[B"); read(s, 1.0)
    s.sendall(b"hello big world"); read(s, 0.8)
    s.sendall(b"\r\n"); read(s, 1.0)
    assert last_body(name) == "hello big world", repr(last_body(name))
    print(f"[OK] space bar works in the message box: {last_body(name)!r}")
    s.close(); time.sleep(0.3)

    s = connect(name, "secret99"); read(s, 2.5)
    s.sendall(b"q"); t = read(s, 1.0)
    assert "DISCONNECT" in t.upper(), t[:300]
    s.sendall(b"\x1b[D"); read(s, 0.3)
    s.sendall(b"\r\n")
    assert closed_within(s, 4), "server should close the connection after confirming"
    print("[OK] lower-case q opens 'Disconnect?' on the readme and quits when confirmed")

    s = connect(name, "secret99"); read(s, 2.5)
    s.sendall(b"\x1b[B"); read(s, 1.0)
    s.sendall(b"\t"); read(s, 0.4)
    s.sendall(b"q"); t = read(s, 1.0)
    assert "DISCONNECT" in t.upper(), t[:300]
    s.sendall(b"\x1b[D"); read(s, 0.3); s.sendall(b"\r\n")
    assert closed_within(s, 4)
    print("[OK] q quits from the room screen when not typing")

    uname = "sp" + uuid.uuid4().hex[:6]
    pw = "pass word 1"
    s = connect()
    t = read(s, 1.5)
    assert "WHAT WOULD YOU LIKE" in t.upper(), t[:300]
    s.sendall(b"\x1b[B"); read(s, 0.3); s.sendall(b"\r\n"); t = read(s, 0.8)
    assert "CONFIRM PASSWORD" in t.upper()
    s.sendall(uname.encode()); read(s, 0.3); s.sendall(b"\t")
    s.sendall(pw.encode()); read(s, 0.3); s.sendall(b"\t")
    s.sendall(pw.encode()); read(s, 0.3); s.sendall(b"\t\t")
    s.sendall(b"\r\n"); t = read(s, 1.5)
    assert "ACCOUNT CREATED" in t.upper(), t[:300]
    s.sendall(b"\r\n"); read(s, 0.8)
    s.sendall(b"\t"); s.sendall(pw.encode()); read(s, 0.3); s.sendall(b"\t\t")
    s.sendall(b"\r\n"); t = read(s, 2.0)
    assert "Welcome" in t or "README" in t.upper(), t[:300]
    import tc_db
    stored = sqlite3.connect("telchatter.db").execute(
        "select pw from accounts where username=?", (uname,)).fetchone()[0]
    assert tc_db.verify_password(pw, stored), "the stored password must contain the typed spaces"
    print("[OK] spaces typed into the password boxes are kept (sign-up and login)")
    s.close()


if __name__ == "__main__":
    main()
