#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the TelChatter end-to-end tests against a throwaway copy of the server.

    python tests/run_tests.py

Starts a fresh server (port 2323) in a temp folder, then drives it with scripted telnet
clients: account creation, login, chat, image + animated-GIF upload, invalid files,
PETSCII rendering, preset (NEW-ENVIRON) logins and keyboard handling.
Needs the same packages as the server (pillow, numpy)."""
import os, re, shutil, socket, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def make_assets(dst):
    from PIL import Image
    im = Image.new("RGB", (40, 30)); px = im.load()
    for y in range(30):
        for x in range(40):
            px[x, y] = (x * 6 % 256, y * 8 % 256, (x + y) * 3 % 256)
    im.save(os.path.join(dst, "test_static.png"))
    frames = []
    for i in range(10):                              # small dot moving over a static background
        f = Image.new("RGB", (80, 50), (30, 30, 80)); fp = f.load()
        dx = 5 + (i * 7) % 70
        for y in range(17, 23):
            for x in range(max(0, dx - 3), min(80, dx + 3)):
                fp[x, y] = (255, 255, 0)
        frames.append(f)
    frames[0].save(os.path.join(dst, "test_anim.gif"), save_all=True,
                   append_images=frames[1:], duration=100, loop=0)
    with open(os.path.join(dst, "test_invalid.bin"), "wb") as f:
        f.write(b"NOT AN IMAGE JUST RANDOM BYTES" * 20)


def port_open(port=2323):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def main():
    if port_open():
        sys.exit("Port 2323 is already in use - stop the running server first.")
    tmp = tempfile.mkdtemp(prefix="telchatter-test-")
    for d in (ROOT, HERE):
        for f in os.listdir(d):
            if (f.startswith(("tc_", "test_", "zm_"))) and f.endswith(".py"):
                shutil.copy(os.path.join(d, f), tmp)
    make_assets(tmp)
    unit_failed = False
    for unit in ("test_unit.py", "test_zmodem_unit.py"):
        print(f"--- {unit} " + "-" * (60 - len(unit)))
        u = subprocess.run([sys.executable, unit], cwd=tmp, capture_output=True, text=True, timeout=240)
        print("\n".join(l for l in u.stdout.splitlines() if l.startswith("[OK]") or "PASSED" in l) or u.stdout[-600:])
        if u.returncode != 0:
            unit_failed = True
            print(u.stderr[-800:])
    log = open(os.path.join(tmp, "server.log"), "w")
    server = subprocess.Popen([sys.executable, "tc_server.py"], cwd=tmp, stdout=log, stderr=subprocess.STDOUT)
    failed = ["unit tests"] if unit_failed else []
    try:
        for _ in range(50):
            if port_open():
                break
            time.sleep(0.2)
        else:
            sys.exit("server did not start; see " + os.path.join(tmp, "server.log"))

        def run(script, *args):
            print(f"\n--- {script} " + "-" * (60 - len(script)))
            r = subprocess.run([sys.executable, script, *args], cwd=tmp, capture_output=True, text=True, timeout=240)
            lines = [l for l in r.stdout.splitlines() if l.startswith("[OK]") or "PASSED" in l]
            print("\n".join(lines) if lines else r.stdout[-600:])
            if r.returncode != 0:
                print(r.stderr[-800:]); failed.append(script)
            return r.stdout

        out = run("test_account.py")
        m = re.search(r"Created test account: (\S+) / (\S+)", out)
        if m:
            run("test_upload.py", m.group(1), m.group(2))     # posts a PNG + an animated GIF
            run("test_render.py")                              # needs those images in the room
        else:
            failed.append("test_account.py (no account created)")
        run("test_preset.py")
        run("test_detect.py")                                  # C64 vs ANSI auto-detection
        run("test_admin.py")                                   # only the configured admin gets Admin
        run("test_input.py")
        run("test_upload_robust.py")                           # FF bytes, slow clients, JPEG/BMP
    finally:
        server.terminate()
        try: server.wait(5)
        except subprocess.TimeoutExpired: server.kill()
        log.close()
    text = open(os.path.join(tmp, "server.log")).read()
    if "Traceback" in text:
        failed.append("server.log contains a traceback"); print(text[-1500:])
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + ("ALL TESTS PASSED" if not failed else "FAILED: " + ", ".join(failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
