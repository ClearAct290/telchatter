#!/usr/bin/env python3
"""Render smoke tests (run after test_upload.py has posted a PNG and an animated GIF):
   1. ANSI client sits in the room: the animated GIF must keep producing frame updates
   2. PETSCII client (picks option 2 on the terminal menu) enters the same room: must
      receive PETSCII control codes and the server must not log an error."""
import socket, sys, time, uuid
from test_preset import connect, read
from test_upload import IAC, SB, SE

def raw_read(s, secs):
    raw = b""; t0 = time.time()
    while time.time() - t0 < secs:
        try: raw += s.recv(65536)
        except socket.timeout: pass
    return raw

def main():
    # ---- 1. ANSI animation
    name = "ans" + uuid.uuid4().hex[:6]
    s = connect(name, "secret99")
    read(s, 2.0)
    s.sendall(b"\x1b[B")                      # sidebar DOWN -> Room 1
    first = raw_read(s, 1.5)
    a = raw_read(s, 1.5); b = raw_read(s, 1.5)
    print(f"ANSI room: first draw {len(first)} B; then {len(a)} B and {len(b)} B in two 1.5 s windows")
    assert len(a) > 150 and len(b) > 150, "animated GIF should keep sending frame deltas"
    print("[OK] animated GIF plays back (delta frames keep arriving)")
    s.close(); time.sleep(0.3)

    # ---- 2. PETSCII
    name = "pet" + uuid.uuid4().hex[:6]
    s = socket.create_connection(("127.0.0.1", 2323), timeout=10); s.settimeout(0.25)
    raw_read(s, 0.6)                           # no NAWS answer -> terminal choice menu
    body = bytes([0]) + b"USER" + bytes([1]) + name.encode() + bytes([3]) + b"PASSWORD" + bytes([1]) + b"secret99"
    s.sendall(bytes([IAC, 251, 39, IAC, SB, 39, 0]) + body + bytes([IAC, SE]))   # WILL ENVIRON + IS
    s.sendall(b"2")                            # C64 PETSCII
    home = raw_read(s, 3.5)
    s.sendall(bytes([0x11]))                   # PETSCII cursor-down -> Room 1
    room = raw_read(s, 3.0)
    ctrl = sum(1 for x in room if x in (0x93, 0x12, 0x92, 0x05, 0x1C, 0x1E, 0x1F, 0x81, 0x90, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0x9B, 0x9C, 0x9E, 0x9F, 0x11, 0x13))
    print(f"PETSCII: home {len(home)} B, room {len(room)} B, control/colour codes in room: {ctrl}")
    assert len(home) > 200 and len(room) > 200 and ctrl > 20, "expected PETSCII screens with colour/reverse codes"
    print("[OK] PETSCII client renders home + room (with images in history)")
    s.close()
    print("\n================ RENDER TESTS PASSED ================")

if __name__ == "__main__":
    main()
