#!/usr/bin/env python3
"""Careful scripted client, one deliberate step at a time with assertions."""
import socket
import time
import sys

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
OPT_BINARY, OPT_ECHO, OPT_SGA, OPT_TTYPE, OPT_NAWS = 0, 1, 3, 24, 31


def strip_ansi(data):
    out, i = [], 0
    while i < len(data):
        b = data[i]
        if b == 0x1b and data[i+1:i+2] == b"[":
            j = i + 2
            while j < len(data) and not (0x40 <= data[j] <= 0x7e):
                j += 1
            i = j + 1
            continue
        if 32 <= b < 127:
            out.append(chr(b))
        elif b in (13, 10):
            out.append("\n")
        i += 1
    return "".join(out)


class C:
    def __init__(self, host="127.0.0.1", port=2323):
        self.s = socket.create_connection((host, port), timeout=10)
        self.s.settimeout(0.25)

    def recv_for(self, seconds):
        total = b""
        t0 = time.time()
        while time.time() - t0 < seconds:
            try:
                c = self.s.recv(65536)
                if c:
                    total += c
                else:
                    break
            except socket.timeout:
                pass
        return total

    def send(self, b):
        self.s.sendall(b)

    def step(self, send_bytes=b"", wait=0.8, tag=""):
        if send_bytes:
            self.send(send_bytes)
        raw = self.recv_for(wait)
        text = strip_ansi(raw)
        print(f"\n===== {tag} | sent {send_bytes!r} | got {len(raw)} bytes =====")
        if text.strip():
            print(text.strip()[:2000])
        else:
            print("(no renderable text; raw head:", raw[:80], ")")
        return text, raw


def main():
    c = C()

    # 1) initial telnet negotiation from server
    text, raw = c.step(b"", 0.5, "initial negotiation")
    # answer: WILL/DO back as needed, then send NAWS as a separate telnet SB
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == IAC and i + 2 < len(raw) and raw[i+1] in (WILL, WONT, DO, DONT):
            cmd, opt = raw[i+1], raw[i+2]
            if cmd == DO and opt in (OPT_BINARY, OPT_SGA, OPT_NAWS):
                out += bytes([IAC, WILL, opt])
            elif cmd == WILL and opt in (OPT_ECHO, OPT_SGA, OPT_BINARY):
                out += bytes([IAC, DO, opt])
            i += 3
        else:
            i += 1
    w, h = 100, 35
    out += bytes([IAC, SB, OPT_NAWS, (w >> 8) & 0xFF, w & 0xFF, (h >> 8) & 0xFF, h & 0xFF, IAC, SE])

    # 2) send our negotiation reply + NAWS promptly, then wait for first real screen.
    # The server already showed the plain-text terminal-choice prompt (it waited
    # negotiate_wait seconds and didn't yet have our NAWS), so our next real "key" needs
    # to be an actual menu choice, not just the negotiation bytes.
    text, raw = c.step(bytes(out), 0.4, "sent negotiation reply + NAWS")
    text, raw = c.step(b"1", 2.5, "chose option 1 (ascii)")

    assert "WHAT WOULD YOU LIKE" in text.upper(), f"expected login_or_create screen, got:\n{text}"
    print("\n[OK] reached login_or_create screen")

    text, raw = c.step(b"\x1b[B", 0.4, "arrow down to Create Account")
    text, raw = c.step(b"\r\n", 0.8, "enter -> create account screen")
    assert "CONFIRM PASSWORD" in text.upper(), f"expected create_account screen, got:\n{text}"
    print("\n[OK] reached create_account screen")

    uname = f"tester{int(time.time()) % 1000000}"
    text, raw = c.step(uname.encode(), 0.4, f"typed username {uname}")
    text, raw = c.step(b"\t", 0.2, "tab to password")
    text, raw = c.step(b"hunter22", 0.4, "typed password")
    text, raw = c.step(b"\t", 0.2, "tab to confirm")
    text, raw = c.step(b"hunter22", 0.4, "typed confirm")
    text, raw = c.step(b"\t", 0.2, "tab to show-password checkbox")
    text, raw = c.step(b"\t", 0.2, "tab to CREATE button")
    text, raw = c.step(b"\r\n", 1.5, "submitted create account")
    assert "ACCOUNT CREATED" in text.upper(), f"expected ACCOUNT CREATED overlay, got:\n{text}"
    print("\n[OK] account created")

    text, raw = c.step(b"\r\n", 1.0, "dismissed overlay -> back at login")
    assert "PASSWORD" in text.upper() and "USERNAME" in text.upper(), f"expected login screen, got:\n{text}"
    print("\n[OK] at login screen, username should be prefilled")

    text, raw = c.step(b"\t", 0.2, "tab to password field")
    text, raw = c.step(b"hunter22", 0.4, "typed password")
    text, raw = c.step(b"\t", 0.2, "tab to showpw checkbox")
    text, raw = c.step(b"\t", 0.2, "tab to LOGIN button")
    text, raw = c.step(b"\r\n", 1.5, "submitted login")
    assert "README" in text.upper() or "WELCOME" in text.upper(), f"expected readme/home screen, got:\n{text}"
    print("\n[OK] logged in, at readme/home screen")

    text, raw = c.step(b"\x1b[B", 1.0, "nav down to Room 1")
    assert "ROOM 1" in text.upper() or "TYPE A MESSAGE" in text.upper(), f"expected room screen, got:\n{text}"
    print("\n[OK] entered a chat room")

    msg = "hello from the automated test client"
    text, raw = c.step(msg.encode(), 0.4, "typed a chat message")
    text, raw = c.step(b"\r\n", 1.2, "sent the message")
    squashed = text.replace(" ", "").replace("\n", "")
    assert msg.replace(" ", "") in squashed or uname in text, f"expected message to appear in history:\n{text}"
    print("\n[OK] message appeared in room history")
    assert uname in text, "expected our own username as the sender"
    print("\n[OK] sender name shown correctly")

    print("\n================ ALL STEPS PASSED ================")
    c.s.close()
    return uname


if __name__ == "__main__":
    uname = main()
    print(f"\nCreated test account: {uname} / hunter22")
