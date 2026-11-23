#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""In-process unit tests (no server): the admin account rules and the upload byte handling."""
import os, random, re, tempfile, io

import tc_db
import tc_images


def fresh_db(names=("Room 1",)):
    d = tempfile.mkdtemp(prefix="tc-unit-")
    return tc_db.Db(os.path.join(d, "t.db"), default_room_names=list(names))


def test_admin_account():
    db = fresh_db()
    db.ensure_admin_account("admin", "admin1234")
    a = db.get_account("admin")
    assert a and a["is_operator"]
    assert db.authenticate("admin", ["admin1234"]) == "admin"
    assert db.authenticate("admin", ["wrong-pass"]) is None
    print("[OK] admin account is created from the config (admin / admin1234 by default)")
    for name in ("admin", "Admin", "ADMIN"):
        ok, _ = db.create_account(name, "password1")
        assert not ok, name
    ok, _ = db.create_account("someone", "password1")
    assert ok and not db.get_account("someone")["is_operator"]
    print("[OK] the admin name cannot be registered; normal signups are never operators")
    db.ensure_admin_account("admin", "brand-new-pw-9")
    assert db.authenticate("admin", ["admin1234"]) is None
    assert db.authenticate("admin", ["brand-new-pw-9"]) == "admin"
    print("[OK] editing admin_password in the config takes effect on restart")
    db.ensure_admin_account("boss", "boss-pass-1")
    assert db.get_account("boss")["is_operator"] and not db.get_account("admin")["is_operator"]
    print("[OK] renaming the admin in the config moves the operator role (old admin demoted)")


def test_legacy_operator_demoted():
    db = fresh_db()
    db.conn.execute("INSERT INTO accounts(username,pw,created_at,last_login,name_color,is_operator) "
                    "VALUES ('first', ?, 0, 0, -1, 1)", (tc_db.hash_password("firstpass1"),))
    db.conn.commit()
    db.ensure_admin_account("admin", "admin1234")
    assert not db.get_account("first")["is_operator"]
    print("[OK] an operator left over from the old 'first account' rule is demoted")


def test_existing_name_not_hijacked():
    db = fresh_db()
    ok, _ = db.create_account("boss", "bosspass1")
    assert ok
    db.ensure_admin_account("boss", "something-else-1")
    assert not db.get_account("boss")["is_operator"]
    assert db.authenticate("boss", ["bosspass1"]) == "boss"
    print("[OK] an existing normal account is never silently promoted or re-passworded")


def make_noisy_png(seed, w=96, h=64):
    from PIL import Image
    rnd = random.Random(seed)
    im = Image.new("RGB", (w, h))
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(w * h)])
    b = io.BytesIO(); im.save(b, "PNG")
    return b.getvalue()


def test_upload_bytes():
    from PIL import Image
    png = make_noisy_png(1)
    while len(re.findall(rb"\xff[\xf0-\xff]", png)) < 3:
        png = make_noisy_png(random.randrange(10**6))
    n_amb = len(re.findall(rb"\xff[\xf0-\xff]", png))
    kw = dict(max_cols=160, max_rows=80)
    assert tc_images.convert_upload_any(png, **kw)["fmt"] == "PNG"
    print(f"[OK] PNG with {n_amb} FF-F*/FF-FF byte pairs decodes as received")
    doubled = png.replace(b"\xff", b"\xff\xff")
    assert tc_images.convert_upload_any(doubled, **kw)["fmt"] == "PNG"
    print("[OK] the same PNG from a client that doubles every 0xFF (RFC 854) decodes too")
    assert tc_images.upload_candidates(png)[0] == png
    assert tc_images.upload_candidates(doubled)[0] == png
    frames = []
    for i in range(8):
        f = Image.new("RGB", (80, 50), (30, 30, 80))
        for y in range(17, 23):
            for x in range(5 + i * 7, 11 + i * 7):
                f.putpixel((x, y), (255, 255, 0))
        frames.append(f)
    b = io.BytesIO(); frames[0].save(b, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    agif = b.getvalue()
    art = tc_images.convert_upload_any(agif.replace(b"\xff", b"\xff\xff"), **kw)
    assert art["fmt"] == "GIF" and art["animated"]
    try:
        tc_images.convert_upload(agif[:-120], **kw)
    except tc_images.BadImage:
        pass
    else:
        raise AssertionError("a cut-off animated GIF must be BadImage")
    print("[OK] animated GIF from a 0xFF-doubling client decodes; a cut-off one is a clean BadImage")
    assert tc_images.looks_truncated(png[: len(png) // 2]) and not tc_images.looks_truncated(png)
    b = io.BytesIO(); Image.new("RGB", (20, 20), (9, 9, 200)).save(b, "GIF"); gif = b.getvalue()
    assert tc_images.looks_truncated(gif[:-10]) and not tc_images.looks_truncated(gif)
    b = io.BytesIO(); Image.new("RGB", (20, 20), (9, 200, 9)).save(b, "JPEG"); jpg = b.getvalue()
    assert tc_images.looks_truncated(jpg[:-20]) and not tc_images.looks_truncated(jpg)
    b = io.BytesIO(); Image.new("RGB", (20, 20), (200, 9, 9)).save(b, "BMP"); bmp = b.getvalue()
    assert tc_images.looks_truncated(bmp[:-30]) and not tc_images.looks_truncated(bmp)
    assert not tc_images.looks_truncated(b"NOT AN IMAGE AT ALL" * 5)
    print("[OK] unfinished PNG / GIF / JPEG / BMP are recognised as unfinished; garbage is not")
    for junk in (b"NOT AN IMAGE JUST RANDOM BYTES" * 20, png[: len(png) // 2]):
        try:
            tc_images.convert_upload_any(junk, **kw)
        except tc_images.BadImage:
            pass
        else:
            raise AssertionError("garbage / half a file must still be rejected")
    print("[OK] garbage and half-files are still rejected")


class _FakeWriter:
    def __init__(self):
        self.out = bytearray()

    def write(self, b):
        self.out += bytes(b)

    async def drain(self):
        pass


def _bare_session():
    """A Session with just enough state to exercise present() and _parse_zmodem()."""
    import tc_session
    s = tc_session.Session.__new__(tc_session.Session)
    s.closed = False
    s.ip = "127.0.0.1"
    s.rawlog = tc_session.NULL_RAWLOG
    s.writer = _FakeWriter()
    s.zmodem = None
    s._zm_carry = b""
    s._zm_known_opts = frozenset((0, 1, 3, 24, 31, 39))
    s.keys = __import__("collections").deque()
    s.negotiated = []
    s.on_negotiate = lambda c, o: s.negotiated.append((c, o))
    s.on_sb = lambda d: s.negotiated.append(("SB", bytes(d)))
    return s


def test_nothing_drawn_during_zmodem():
    """The bug from the SyncTERM log: a screen redraw sent right after our ZRINIT made SyncTERM
    reject the header. While a transfer is attached, present() must put NOTHING on the wire."""
    import asyncio
    from tc_render import Grid
    s = _bare_session()
    seen = []

    class Presenter:
        def encode(self, g, force=False):
            seen.append(1)
            return b"\x1b[2Jscreen"

    s.presenter = Presenter()
    s.skipped = 0
    s.cfg = None
    # control: with no transfer attached the screen IS written (so the check below proves something)
    asyncio.run(s.present(Grid(24, 80), force=True))
    assert bytes(s.writer.out) == b"\x1b[2Jscreen" and not s.closed, (bytes(s.writer.out), s.closed)
    s.writer.out.clear()
    seen.clear()
    s.zmodem = object()                                   # a transfer is attached
    asyncio.run(s.present(Grid(24, 80), force=True))
    assert bytes(s.writer.out) == b"" and not seen, "no screen data may be sent during a ZMODEM transfer"
    asyncio.run(s.present(Grid(24, 80)))                  # the non-forced path too
    assert bytes(s.writer.out) == b"" and not seen, "no screen data may be sent during a ZMODEM transfer"
    print("[OK] no screen update is ever written while a ZMODEM transfer is attached")


def test_zmodem_telnet_unframing():
    class Fake:
        def __init__(self):
            self.got = bytearray()

        def feed(self, d):
            self.got += d
            return b""

    s = _bare_session()
    s.zmodem = Fake()
    # IAC IAC -> one 0xFF (what an RFC 854 client sends for a data byte)
    s._parse_zmodem(b"a\xff\xffb")
    assert bytes(s.zmodem.got) == b"a\xffb"
    # an IAC split across two reads is carried over, not misread
    s.zmodem.got.clear()
    s._parse_zmodem(b"x\xff")
    s._parse_zmodem(b"\xffy")
    assert bytes(s.zmodem.got) == b"x\xffy"
    # an un-doubled 0xFF followed by an ordinary byte is DATA, not a 2-byte telnet command to swallow
    s.zmodem.got.clear()
    s._parse_zmodem(b"\x01\xff\x8a\x02\xff\x41\x03")
    assert bytes(s.zmodem.got) == b"\x01\xff\x8a\x02\xff\x41\x03", bytes(s.zmodem.got)
    # a genuine renegotiation / NAWS subnegotiation mid-transfer is still honoured and stripped
    s.zmodem.got.clear()
    s._parse_zmodem(b"A\xff\xfd\x1fB\xff\xfa\x1f\x00\x50\x00\x18\xff\xf0C")
    assert bytes(s.zmodem.got) == b"ABC", bytes(s.zmodem.got)
    assert (253, 31) in s.negotiated and any(n[0] == "SB" for n in s.negotiated if isinstance(n, tuple))
    print("[OK] telnet framing during a transfer: IAC IAC undone, real negotiations honoured, lone 0xFF kept as data")


if __name__ == "__main__":
    test_nothing_drawn_during_zmodem()
    test_zmodem_telnet_unframing()
    test_admin_account()
    test_legacy_operator_demoted()
    test_existing_name_not_hijacked()
    test_upload_bytes()
    print("\n================ UNIT TESTS PASSED ================")
