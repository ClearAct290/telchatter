#!/usr/bin/env python3
"""Login as an existing test account, enter a room, and exercise the image-upload flow
over ZMODEM (the test plays the part of SyncTERM's Upload -> ZModem): PNG + animated GIF,
attach / delete / post, invalid files, and backing out with ESC."""
import socket
import sys
import time

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

    def step(self, send_bytes=b"", wait=0.8, tag=""):
        if send_bytes:
            self.s.sendall(send_bytes)
        raw = self.recv_for(wait)
        text = strip_ansi(raw)
        print(f"\n===== {tag} | sent {len(send_bytes)} bytes | got {len(raw)} bytes =====")
        if text.strip():
            print(text.strip()[:1500])
        else:
            print("(no text; raw head:", raw[:80], ")")
        return text, raw


def login(c, uname, pw):
    out = bytearray()
    raw = c.recv_for(0.5)
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
    c.step(bytes(out), 0.3, "negotiation")
    text, raw = c.step(b"1", 2.5, "ascii choice")
    assert "WHAT WOULD YOU LIKE" in text.upper()
    text, raw = c.step(b"\r\n", 0.8, "default to login (sel=0)")
    assert "USERNAME" in text.upper() and "PASSWORD" in text.upper() and "CONFIRM" not in text.upper()
    text, raw = c.step(uname.encode(), 0.4, "typed username")
    text, raw = c.step(b"\t", 0.2, "tab to password")
    text, raw = c.step(pw.encode(), 0.4, "typed password")
    text, raw = c.step(b"\t", 0.2, "tab to showpw")
    text, raw = c.step(b"\t", 0.2, "tab to login btn")
    text, raw = c.step(b"\r\n", 1.5, "submit login")
    assert "README" in text.upper() or "WELCOME" in text.upper(), text
    print("\n[OK] logged in")
    text, raw = c.step(b"\x1b[B", 1.0, "nav to room 1")
    assert "TYPE A MESSAGE" in text.upper()
    print("[OK] in room 1")
    return text


def wait_for(c, needle, max_s, tag):
    """Poll the socket until `needle` (case-insensitive) shows in the text received so far."""
    t0 = time.time(); acc = b""
    while time.time() - t0 < max_s:
        acc += c.recv_for(0.2)
        if needle.lower() in strip_ansi(acc).lower():
            break
    text = strip_ansi(acc)
    print(f"\n===== {tag} | waited {time.time()-t0:.1f}s | got {len(acc)} bytes =====")
    print(text.strip()[:600] if text.strip() else "(no text)")
    return text, time.time() - t0


_USER = None


def n_images():
    import sqlite3
    return sqlite3.connect("telchatter.db").execute(
        "select count(*) from messages where kind='image' and username=?", (_USER,)).fetchone()[0]


def open_upload_dialog(c, tabs):
    """Focus IMAGE, press ENTER, confirm -> the 'waiting for your terminal' screen."""
    for _ in range(tabs):
        c.step(b"\t", 0.2, "tab")
    text, _ = c.step(b"\r\n", 0.8, "enter on IMAGE -> upload dialog")
    assert "ZMODEM" in text.upper() and "READY TO UPLOAD" in text.upper(), text
    text, _ = c.step(b"\r\n", 0.8, "confirm yes (default)")
    assert "WAITING FOR YOUR TERMINAL" in text.upper(), text
    return text


def do_upload(c, filepath, kind, tabs=2, **sender_kw):
    """kind: 'ok' (attach then SEND), 'invalid', or 'attach_only' (leave attached, return)."""
    import os
    from zm_client import zmodem_send
    with open(filepath, "rb") as f:
        raw_img = f.read()
    print(f"\n[uploading {filepath}, {len(raw_img)} bytes, ZMODEM {sender_kw or ''}]")
    open_upload_dialog(c, tabs)
    print("[OK] upload dialog open, server is waiting for the ZMODEM sender")

    t0 = time.time()
    zs = zmodem_send(c, [(os.path.basename(filepath), raw_img)], **sender_kw)
    assert zs.done, f"ZMODEM transfer did not complete: aborted={zs.aborted} log={zs.log[-6:]}"
    print(f"[OK] ZMODEM transfer completed in {time.time() - t0:.1f}s")
    if kind == "invalid":
        text, el = wait_for(c, "invalid file format", 6, "waiting for invalid-file popup")
        assert "INVALID FILE FORMAT" in text.upper(), text
        print(f"[OK] a transferred file that is not an image is rejected with the popup")
        c.recv_for(3.0)                        # let the popup time out back to the room
        return
    text, el = wait_for(c, "image uploaded", 8, "waiting for upload-complete state in input box")
    assert "IMAGE UPLOADED" in text.upper(), f"expected attached state in input box, got:\n{text}"
    print("[OK] converted, and the input box shows 'image uploaded' (no input lock involved)")
    if kind == "attach_only":
        return
    before = n_images()
    c.step(b"\r\n", 1.2, "ENTER on SEND -> post image")
    # the screen only re-sends CHANGED cells, so check the database rather than the screen text
    assert n_images() == before + 1, "image was not posted to the room"
    print("[OK] image posted to the room")


def cancel_upload(c, tabs=2):
    """ESC on the 'waiting for your terminal' screen backs out at once - no lock."""
    open_upload_dialog(c, tabs)
    t0 = time.time()
    c.s.sendall(b"\x1b")
    text, el = wait_for(c, "type a message", 4, "waiting for return to the room after ESC")
    assert "TYPE A MESSAGE" in text.upper() or "IMAGE" in text.upper(), f"ESC should leave the upload screen:\n{text}"
    assert time.time() - t0 < 2.5, "ESC must cancel immediately, not after a lock period"
    print("[OK] ESC cancels the upload immediately")


def delete_pending(c):
    """Focus is on SEND after an upload; TAB to IMAGE, ENTER, choose YES to delete."""
    c.step(b"\t", 0.2, "tab to IMAGE (attached)")
    text, _ = c.step(b"\r\n", 0.8, "press IMAGE again -> delete confirm")
    assert "DELETE UPLOADED IMAGE" in text.upper(), text
    c.step(b"\x1b[D", 0.2, "arrow to YES")
    c.step(b"\r\n", 0.8, "confirm delete")
    print("[OK] attached image deleted")


def main():
    uname, pw = sys.argv[1], sys.argv[2]
    global _USER
    _USER = uname
    c = C()
    login(c, uname, pw)
    do_upload(c, "test_static.png", "ok")                 # static PNG -> posted
    do_upload(c, "test_anim.gif", "ok")                   # animated GIF -> posted
    do_upload(c, "test_static.png", "attach_only", tabs=2)
    delete_pending(c)                                     # attach then delete (must NOT be posted)
    do_upload(c, "test_invalid.bin", "invalid", tabs=0)   # focus is still on IMAGE after delete
    cancel_upload(c, tabs=0)                              # focus still on IMAGE after the popup
    c.s.close()

    # reconnect: the history persisted, pinned to the newest message, and the DB holds
    # exactly the 2 posted images (the attached-then-deleted one must not exist)
    c2 = C()
    text = login(c2, uname, pw)
    assert "sent an image" in text.lower(), "newest image should be visible at the bottom of the history"
    import sqlite3
    n = sqlite3.connect("telchatter.db").execute("select count(*) from messages where kind='image'").fetchone()[0]
    print(f"\nimage messages stored in DB: {n}")
    assert n == 2, f"expected 2 posted images, DB has {n}"
    print("\n================ UPLOAD TESTS PASSED ================")
    c2.s.close()


if __name__ == "__main__":
    main()
