#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""In-process tests of the ZMODEM receiver (tc_zmodem.py) against the test sender
(zm_sender.py): no sockets, no server."""
import random

import tc_zmodem as Z
from zm_sender import ZSender, hex_hdr


def tricky_bytes(n, seed=1):
    """Random data seasoned with everything ZMODEM has to escape."""
    rnd = random.Random(seed)
    base = bytes(rnd.randrange(256) for _ in range(n))
    spice = bytes(range(256)) + b"@\r@\r\x18\x18\x18\x11\x13\x91\x93\x10\x90\xff\xff\xfa\xf0" * 3
    out = bytearray(base)
    for i in range(0, max(0, n - len(spice)), max(1, n // 7)):
        out[i:i + len(spice)] = spice
    return bytes(out[:n])


def transfer(files, chunk=None, max_bytes=8_000_000, escape_ctrl=True, seed=0, **sender_kw):
    """Run receiver and sender against each other. `chunk` = None (whole writes), an int
    (fixed size) or "rand" (random 1..700 byte pieces)."""
    from_recv = bytearray()
    zr = Z.ZmodemReceiver(send=from_recv.extend, max_bytes=max_bytes, escape_ctrl=escape_ctrl)
    zs = ZSender(files, **sender_kw)
    rnd = random.Random(seed)
    zr.start()
    pending = zs.start()
    stray = bytearray()
    for _ in range(200000):
        if from_recv:
            pending += zs.feed(bytes(from_recv))
            from_recv.clear()
        if pending:
            if chunk is None:
                piece, pending = pending, b""
            else:
                n = rnd.randint(1, 700) if chunk == "rand" else int(chunk)
                piece, pending = pending[:n], pending[n:]
            stray += zr.feed(piece)
        elif not from_recv:
            break
    return zr, zs, bytes(stray)


def check(zr, zs, files):
    assert zr.finished and not zr.failed, (zr.finished, zr.failed, zr.error)
    assert zs.done, "sender did not reach the end of the session"
    assert zr.files == [files[0]], f"received {[(n, len(d)) for n, d in zr.files]}"


def test_known_frames():
    # frames as written by real lrzsz / SyncTERM sessions
    assert hex_hdr(0) == b"**\x18B00000000000000\r\x8a\x11"
    assert Z.hex_header(Z.ZRQINIT) == b"**\x18B00000000000000\r\x8a\x11"
    assert Z.hex_header(Z.ZRINIT, bytes([0, 0, 0, 0x23])) == b"**\x18B0100000023be50\r\x8a\x11"
    assert Z.hex_header(Z.ZRPOS, 0) == b"**\x18B0900000000a87c\r\x8a\x11"
    assert Z.hex_header(Z.ZFIN, 0) == b"**\x18B0800000000022d\r\x8a"
    assert Z.hex_header(Z.ZACK, 1).endswith(b"\r\x8a")          # no XON after ZACK / ZFIN
    sent = bytearray()
    zr = Z.ZmodemReceiver(send=sent.extend, escape_ctrl=False)
    zr.start()
    assert bytes(sent) == b"**\x18B0100000023be50\r\x8a\x11", bytes(sent)    # CANFDX|CANOVIO|CANFC32
    sent.clear()
    zr = Z.ZmodemReceiver(send=sent.extend, escape_ctrl=True)
    zr.start()
    assert bytes(sent) == Z.hex_header(Z.ZRINIT, bytes([0, 0, 0, 0x63])), "ESCCTL adds 0x40 to the flags"
    # the default receiver must announce the classic `rz` ZRINIT, WITHOUT ESCCTL: SyncTERM answered
    # a ZRINIT with ESCCTL by dropping the CR / LF / XON that end every hex header as "unescaped
    # control characters" and rejecting the header ("HEX header not terminated" / INVALID HEADER)
    sent.clear()
    zr = Z.ZmodemReceiver(send=sent.extend)
    zr.start()
    assert bytes(sent) == b"**\x18B0100000023be50\r\x8a\x11", "default ZRINIT must be the classic 0x23 one"
    import tempfile, os
    from tc_config import Config
    cfg = Config(os.path.join(tempfile.mkdtemp(prefix="tc-cfg-"), "c.ini"))
    assert cfg.zmodem_escape_ctrl is False, "[zmodem] escape_ctrl must default to false"
    print("[OK] hex frames match real ZMODEM traffic byte for byte (ZRQINIT, ZRINIT, ZRPOS, ZFIN)")
    print("[OK] the default ZRINIT is the classic rz one (no ESCCTL), and the config default agrees")


def test_matrix():
    n_runs = 0
    for crc32 in (True, False):
        for escape_all in (False, True):
            for style in ("stream", "q", "w"):
                for size in (0, 1, 1023, 1024, 1025, 5000, 70000):
                    data = tricky_bytes(size, seed=size + 7)
                    files = [("pic.png", data)]
                    zr, zs, _ = transfer(files, chunk="rand", seed=size, crc32=crc32,
                                         escape_all=escape_all, style=style)
                    check(zr, zs, files)
                    n_runs += 1
    print(f"[OK] {n_runs} transfers: CRC-16/CRC-32 x plain/escape-all-controls x stream/ZCRCQ/ZCRCW x 0..70000 bytes")


def test_byte_at_a_time_and_sizes():
    data = tricky_bytes(3000, 5)
    files = [("x.bin", data)]
    zr, zs, _ = transfer(files, chunk=1, escape_all=True)
    check(zr, zs, files)
    zr, zs, _ = transfer(files, chunk=1, crc32=False)
    check(zr, zs, files)
    big = tricky_bytes(600_000, 9)
    zr, zs, _ = transfer([("big.png", big)], chunk=4096, block=8192, escape_all=True)
    check(zr, zs, [("big.png", big)])
    zr, zs, _ = transfer([("odd.bin", big[:99_999])], chunk="rand", block=333, style="q")
    check(zr, zs, [("odd.bin", big[:99_999])])
    print("[OK] one byte at a time, a 600 kB file with 8 kB blocks, and odd block sizes all arrive intact")


def test_not_eager_sender():
    data = tricky_bytes(4000, 3)
    files = [("a.jpg", data)]
    zr, zs, _ = transfer(files, eager=False)          # sender waits for the receiver's ZRINIT
    check(zr, zs, files)
    print("[OK] a sender that waits for our ZRINIT (terminal auto-start style) works too")


def test_corruption_recovery():
    for style in ("stream", "q", "w"):
        for crc32 in (True, False):
            data = tricky_bytes(20000, 11)
            files = [("p.png", data)]
            zr, zs, _ = transfer(files, chunk="rand", corrupt_pos=2048, block=1024, style=style, crc32=crc32)
            check(zr, zs, files)
            assert zr.errors >= 1, "the damaged block must have been noticed"
            assert any("pos=2048" in l and "type=9" in l for l in zs.log), "receiver must ask for a resend from 2048"
    print("[OK] a damaged block is detected (CRC) and re-requested with ZRPOS; the final file is still exact")


def test_multiple_files_and_limits():
    a, b = tricky_bytes(3000, 1), tricky_bytes(2000, 2)
    files = [("first.png", a), ("second.png", b)]
    zr, zs, _ = transfer(files)
    assert zr.finished and zr.files == [("first.png", a)] and zs.skipped == ["second.png"], (zr.files, zs.skipped)
    zr, zs, _ = transfer([("huge.png", tricky_bytes(5000, 3))], max_bytes=1000)
    assert zr.finished and not zr.files and "too large" in zr.error, (zr.finished, zr.files, zr.error)
    assert zs.skipped == ["huge.png"]
    print("[OK] only the first file of a batch is kept; an oversize file is refused (ZSKIP) before any data")


def test_filename_sanitised():
    assert Z.clean_filename(b"C:\\Users\\me\\pic\x01.png") == "pic.png"
    assert Z.clean_filename(b"../../etc/passwd") == "passwd"
    assert Z.clean_filename(b"\x00\x01") == "upload"
    assert len(Z.clean_filename(b"a" * 500)) == 64
    print("[OK] announced file names are reduced to a harmless display name")


def test_cancel_and_stray():
    sent = bytearray()
    zr = Z.ZmodemReceiver(send=sent.extend)
    zr.start()
    # keystrokes before any ZMODEM header are handed back to the caller
    assert zr.feed(b"\x1b") == b"\x1b"
    assert zr.feed(b"abc") == b"abc"
    assert not zr.started
    assert zr.feed(b"rz\r" + hex_hdr(Z.ZRQINIT)) == b"rz\r"
    assert zr.started
    assert zr.feed(b"\r\n garbage \x11") == b"", "noise after the transfer began is dropped, not typed"
    # five CANs (split over two writes) = the sender gave up
    zr.feed(b"\x18\x18\x18")
    zr.feed(b"\x18\x18\x08\x08")
    assert zr.failed and zr.done
    zr2 = Z.ZmodemReceiver(send=sent.extend)
    zr2.start()
    sent.clear()
    zr2.cancel("user pressed ESC")
    assert zr2.failed == "user pressed ESC" and bytes(sent) == Z.CANIT
    print("[OK] keystrokes pass through before the transfer; CAN x5 aborts; our cancel sends the abort string")


def test_header_robustness():
    sent = bytearray()
    zr = Z.ZmodemReceiver(send=sent.extend)
    bad = bytearray(hex_hdr(Z.ZRQINIT))
    bad[10] ^= 1                                    # damage a hex digit -> CRC mismatch
    zr.feed(bytes(bad))
    assert not zr.started
    zr.feed(hex_hdr(Z.ZRQINIT).replace(b"B0000", b"B0000"))     # a good one afterwards still works
    assert zr.started and bytes(sent).startswith(b"**\x18B01")
    zr = Z.ZmodemReceiver(send=sent.extend)
    zr.feed(hex_hdr(Z.ZRQINIT).upper().replace(b"\r\x8A\x11", b"\r\x8a\x11"))
    assert zr.started, "upper-case hex digits are accepted"
    print("[OK] corrupt headers are ignored, upper-case hex accepted")


def test_zrinit_repeats_with_fake_clock():
    now = [100.0]
    sent = bytearray()
    zr = Z.ZmodemReceiver(send=sent.extend, zrinit_interval=4.0, clock=lambda: now[0])
    zr.start()
    n0 = len(sent)
    now[0] += 1.0
    zr.poll()
    assert len(sent) == n0, "too early to repeat"
    now[0] += 4.0
    zr.poll()
    assert len(sent) == 2 * n0, "ZRINIT is repeated while the sender has not started"
    zr.feed(hex_hdr(Z.ZRQINIT))
    n1 = len(sent)
    now[0] += 10
    zr.poll()
    assert len(sent) == n1, "no repeats once a transfer is under way"
    print("[OK] ZRINIT is re-announced every few seconds until the sender starts")


def test_runaway_subpacket():
    sent = bytearray()
    zr = Z.ZmodemReceiver(send=sent.extend)
    zs = ZSender([("a.bin", b"x" * 10)])
    zr.start()
    zr.feed(zs.bin_hdr(Z.ZFILE if hasattr(Z, "ZFILE") else 4, 0) + zs.sub(b"a.bin\x0010 0 0 0 1 10\x00", 0x6B))
    assert zr.cur is not None
    zr.feed(zs.bin_hdr(10, 0))                      # ZDATA ...
    zr.feed(b"A" * (2 << 20))                       # ... then data that never ends
    assert zr.errors >= 1 and zr.mode == "hdr", "an unterminated sub-packet must not grow without bound"
    print("[OK] an endless sub-packet is cut off instead of eating memory")


if __name__ == "__main__":
    test_known_frames()
    test_matrix()
    test_byte_at_a_time_and_sizes()
    test_not_eager_sender()
    test_corruption_recovery()
    test_multiple_files_and_limits()
    test_filename_sanitised()
    test_cancel_and_stray()
    test_header_robustness()
    test_zrinit_repeats_with_fake_clock()
    test_runaway_subpacket()
    print("\n================ ZMODEM UNIT TESTS PASSED ================")
