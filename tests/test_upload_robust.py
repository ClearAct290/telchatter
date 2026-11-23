#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""ZMODEM upload robustness, end to end through a real server:
dense images full of 0xFF bytes, every sender style (CRC-16 / CRC-32, plain / escape-all,
stream / ZCRCQ / ZCRCW, sender that waits for our ZRINIT), a damaged block that must be
re-sent, a large image, a terminal that aborts mid-transfer, a cut-off PNG that transfers
fine but is not a valid image, and the server's ZRINIT invitation itself."""
import io, os, random, re, sqlite3, time, uuid
from test_preset import connect
from test_upload import C, strip_ansi, wait_for, open_upload_dialog
from zm_client import zmodem_send, Unframer


def noisy(fmt, seed, w=96, h=64):
    from PIL import Image
    rnd = random.Random(seed)
    im = Image.new("RGB", (w, h))
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(w * h)])
    b = io.BytesIO(); im.save(b, fmt)
    return b.getvalue()


def png_with_ff_pairs():
    for seed in range(1, 500):
        data = noisy("PNG", seed)
        if len(re.findall(rb"\xff[\xf0-\xff]", data)) >= 3:
            return data
    raise SystemExit("could not build a test PNG")


def animated_gif():
    from PIL import Image
    frames = []
    for i in range(10):
        f = Image.new("RGB", (80, 50), (30, 30, 80))
        for y in range(17, 23):
            for x in range(5 + i * 6, 11 + i * 6):
                f.putpixel((x, y), (255, 255, 0))
        frames.append(f)
    b = io.BytesIO(); frames[0].save(b, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return b.getvalue()


# During a transfer the only thing the server may send is ZMODEM hex headers
HEADERS_ONLY = re.compile(rb"(?:\*\*\x18B[0-9a-f]{14}\r\x8a\x11?)*")


def png_with_unescaped_ff():
    """A PNG with plenty of 0xFF bytes but none that a NON-escaping client could confuse with
    telnet framing (no FF FF, no FF + telnet command byte)."""
    for seed in range(1, 3000):
        data = noisy("PNG", seed)
        if data.count(b"\xff") >= 10 and not re.search(rb"\xff[\xf0-\xff]", data):
            return data
    raise SystemExit("could not build a test PNG")


def n_images(user):
    return sqlite3.connect("telchatter.db").execute(
        "select count(*) from messages where kind='image' and username=?", (user,)).fetchone()[0]


def send_and_post(c, user, name, data, label, tabs=2, **kw):
    """Whole flow for a file that must be accepted: dialog -> ZMODEM -> 'image uploaded' -> SEND."""
    posted_before = n_images(user)
    open_upload_dialog(c, tabs)
    t0 = time.time()
    zs = zmodem_send(c, [(name, data)], **kw)
    assert zs.done, f"{label}: transfer did not finish (aborted={zs.aborted}) {zs.log[-5:]}"
    assert HEADERS_ONLY.fullmatch(bytes(zs.server_bytes)), (
        f"{label}: the server sent something other than ZMODEM headers during the transfer: "
        f"{bytes(zs.server_bytes)[:120]!r}")
    text, el = wait_for(c, "image uploaded", 10, f"{label}: waiting for 'image uploaded'")
    assert "IMAGE UPLOADED" in text.upper(), f"{label}: file was rejected:\n{text}"
    before = n_images(user)
    assert before == posted_before, f"{label}: the transfer alone must NOT post the image (stray keys?)"
    c.step(b"\r\n", 1.2, "SEND")
    assert n_images(user) == before + 1, f"{label}: image was not posted"
    print(f"[OK] {label} ({len(data)} B) -> accepted and posted in {time.time() - t0:.1f}s")


def main():
    name = "up" + uuid.uuid4().hex[:6]
    c = C.__new__(C); c.s = connect(name, "secret99")
    c.recv_for(2.0)
    c.step(b"\x1b[B", 1.0, "DOWN -> Room 1")

    png = png_with_ff_pairs()
    n = len(re.findall(rb"\xff[\xf0-\xff]", png))
    print(f"\n[test PNG: {len(png)} bytes, {n} FF-F*/FF-FF pairs]")

    # 0) what the server actually says when the dialog opens: a ZRINIT hex header (no stray text)
    for _ in range(2):
        c.step(b"\t", 0.2, "tab")
    c.step(b"\r\n", 0.8, "ENTER on IMAGE")
    c.s.sendall(b"\r\n")
    raw = c.recv_for(1.2)
    un = Unframer().feed(raw)
    classic = b"**\x18B0100000023be50\r\x8a\x11"
    assert classic in un, f"server should announce the classic rz ZRINIT (no ESCCTL): {un[-60:]!r}"
    assert un.endswith(classic), (
        "NOTHING may follow the ZRINIT: SyncTERM starts its own transfer on seeing it and rejected "
        f"the header as INVALID when a screen redraw came right after it. Tail was: {un[-60:]!r}")
    assert b"rz\r" not in un, "no stray 'rz' text by default (rz_prefix = false)"
    print("[OK] the classic ZRINIT is the last thing on the wire - no screen redraw follows it")
    # back out: ESC (nothing started yet) and go on with the real transfers
    c.s.sendall(b"\x1b")
    wait_for(c, "type a message", 4, "ESC back to the room")

    send_and_post(c, name, "photo.png", png, "PNG full of FF bytes, CRC-32, escape-all, streaming",
                  tabs=0)      # focus is still on IMAGE after the ESC above
    send_and_post(c, name, "photo2.png", png, "same PNG, CRC-16, minimal escaping", crc32=False, escape_all=False)
    send_and_post(c, name, "photo3.png", png, "same PNG, windowed (ZCRCW per block, 256 B blocks)",
                  style="w", block=256)
    send_and_post(c, name, "photo4.png", png, "same PNG, ZCRCQ style", style="q", block=512)
    send_and_post(c, name, "photo5.png", png, "sender that waits for our ZRINIT", eager=False)
    send_and_post(c, name, "photo6.png", png, "sender without the 'rz' text", rz_prefix=False)
    send_and_post(c, name, "photo7.png", png_with_unescaped_ff(),
                  "PNG from a client that does not double 0xFF (tolerated)", raw_binary=True)
    send_and_post(c, name, "photo8.png", png, "same PNG, escape-all sender (a client that asks for ESCCTL)",
                  escape_all=True)
    send_and_post(c, name, "photo.jpg", noisy("JPEG", 7), "JPEG")
    send_and_post(c, name, "photo.bmp", noisy("BMP", 8), "BMP")
    send_and_post(c, name, "anim.gif", animated_gif(), "animated GIF")

    big = noisy("PNG", 11, 400, 300)
    print(f"[big PNG {len(big)} bytes]")
    send_and_post(c, name, "big.png", big, "large dense PNG")

    # a block damaged in transit is detected by its CRC and re-sent
    send_and_post(c, name, "damaged.png", png, "PNG with one damaged block (retransmitted)",
                  block=512, corrupt_pos=1024)

    # a PNG that stops half-way transfers perfectly but is not a valid image
    open_upload_dialog(c, 2)
    half = png[: len(png) // 2]
    zs = zmodem_send(c, [("half.png", half)])
    assert zs.done
    text, el = wait_for(c, "invalid file format", 6, "half a PNG")
    assert "INVALID FILE FORMAT" in text.upper(), text
    c.recv_for(3.0)
    print("[OK] half a PNG transfers but is rejected as 'Invalid file format'")

    # the terminal gives up mid-transfer (5 x CAN): clean message, back in the room
    open_upload_dialog(c, 0)
    zs = zmodem_send(c, [("cut.png", big)], block=512, stop_after_bytes=1)
    c.s.sendall(b"\x18" * 5)
    text, el = wait_for(c, "upload did not complete", 6, "terminal aborts mid-transfer")
    assert "UPLOAD DID NOT COMPLETE" in text.upper(), text
    c.recv_for(3.2)
    print("[OK] a transfer cancelled by the terminal ends with 'Upload did not complete'")
    c.step(b"\x1b[A", 0.5, "focus text box (UP)")
    c.s.close()

    n_img = n_images(name)
    assert n_img == 13, f"expected 13 posted images, found {n_img}"
    print(f"\nposted images in DB: {n_img}")
    print("\n================ UPLOAD ROBUSTNESS TESTS PASSED ================")


if __name__ == "__main__":
    main()
