#!/usr/bin/env python3
"""Preset-login test: a telnet client that answers NEW-ENVIRON with USER + PASSWORD.
   A) new user   -> account auto-created, lands on the home/readme screen
   B) same again -> auto-login, home screen
   C) same user, wrong password -> create-account screen with an EMPTY username box
   D) no NEW-ENVIRON at all -> normal 'log in / create account' menu"""
import socket, sys, time, uuid
from test_upload import strip_ansi, IAC, DONT, DO, WONT, WILL, SB, SE

ENV = 39
OPT_BINARY, OPT_ECHO, OPT_SGA, OPT_TTYPE, OPT_NAWS = 0, 1, 3, 24, 31


def connect(user=None, pw=None, speak_environ=True, answer_cpr=True):
    s = socket.create_connection(("127.0.0.1", 2323), timeout=10)
    s.settimeout(5)
    raw = s.recv(65536)              # answer the server's opening negotiation immediately,
    s.settimeout(0.03)               # like a real client (the server only waits ~0.3 s)
    try:
        while True:
            c = s.recv(65536)
            if not c: break
            raw += c
    except socket.timeout:
        pass
    s.settimeout(0.25)
    out = bytearray(); i = 0
    while i < len(raw):
        if raw[i] == IAC and i + 2 < len(raw) and raw[i+1] in (WILL, WONT, DO, DONT):
            cmd, opt = raw[i+1], raw[i+2]
            if cmd == DO and opt in (OPT_BINARY, OPT_SGA, OPT_NAWS):
                out += bytes([IAC, WILL, opt])
            elif cmd == DO and opt == ENV and speak_environ:
                out += bytes([IAC, WILL, ENV])
            elif cmd == DO:
                out += bytes([IAC, WONT, opt])
            elif cmd == WILL and opt in (OPT_ECHO, OPT_SGA, OPT_BINARY):
                out += bytes([IAC, DO, opt])
            i += 3
        else:
            i += 1
    out += bytes([IAC, SB, OPT_NAWS, 0, 100, 0, 35, IAC, SE])
    s.sendall(bytes(out))
    if user and speak_environ:
        # reply to the server's SEND with IS: VAR USER VALUE x USERVAR PASSWORD VALUE y
        body = bytes([0]) + b"USER" + bytes([1]) + user.encode() + bytes([3]) + b"PASSWORD" + bytes([1]) + pw.encode()
        s.sendall(bytes([IAC, SB, ENV, 0]) + body + bytes([IAC, SE]))
    if answer_cpr:
        # a real terminal answers the server's cursor-position probe (ESC[6n) with ESC[row;colR
        seen = b""; t0 = time.time()
        while b"\x1b[6n" not in seen and time.time() - t0 < 3.0:
            try: seen += s.recv(65536)
            except socket.timeout: pass
        s.sendall(b"\x1b[1;1R")
    return s


def read(s, secs):
    raw = b""; t0 = time.time()
    while time.time() - t0 < secs:
        try: raw += s.recv(65536)
        except socket.timeout: pass
    return strip_ansi(raw)


def main():
    name = "pre" + uuid.uuid4().hex[:6]

    s = connect(name, "secret99")
    t = read(s, 2.5)
    assert "README" in t.upper() or "WELCOME" in t.upper(), f"A: expected home screen:\n{t[:400]}"
    print(f"[OK] A: new preset user '{name}' auto-created and logged in"); s.close(); time.sleep(0.3)

    s = connect(name, "secret99")
    t = read(s, 2.5)
    assert "README" in t.upper() or "WELCOME" in t.upper(), f"B: expected home screen:\n{t[:400]}"
    print("[OK] B: existing preset user auto-logged in"); s.close(); time.sleep(0.3)

    s = connect(name, "WRONGpass1")
    t = read(s, 2.5)
    assert "CONFIRM PASSWORD" in t.upper(), f"C: expected create-account screen:\n{t[:400]}"
    assert name not in t, "C: taken username must NOT be prefilled"
    print("[OK] C: taken username -> create-account screen with empty username box"); s.close(); time.sleep(0.3)

    s = connect(speak_environ=False)
    t = read(s, 1.5)
    s.sendall(b"1"); t += read(s, 1.0)
    assert "WHAT WOULD YOU LIKE" in t.upper(), f"D: expected log-in/create menu:\n{t[:400]}"
    print("[OK] D: client without presets gets the log in / create account menu"); s.close()
    print("\n================ PRESET LOGIN TESTS PASSED ================")


if __name__ == "__main__":
    main()
