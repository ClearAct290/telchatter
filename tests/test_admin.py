#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Only the configured admin account (default admin / admin1234) gets the Admin menu."""
import time, uuid
from test_preset import connect, read


def main():
    s = connect("first" + uuid.uuid4().hex[:5], "secret99")
    t = read(s, 2.5)
    assert "Welcome" in t or "README" in t.upper(), t[:300]
    assert "Admin" not in t, f"regular user must not get the Admin sidebar entry:\n{t[:600]}"
    for _ in range(5):
        s.sendall(b"\x1b[B"); t += read(s, 0.5)
    assert "OPERATOR MENU" not in t.upper() and "Admin" not in t
    print("[OK] a normal user (first to register) gets no Admin entry and cannot reach the admin menu")
    s.close(); time.sleep(0.3)

    s = connect("Admin", "wrong-guess-1")
    t = read(s, 2.5)
    assert "CONFIRM PASSWORD" in t.upper() and "Welcome" not in t, t[:300]
    print("[OK] 'admin' with a wrong password is not let in and cannot be registered over")
    s.close(); time.sleep(0.3)

    s = connect("admin", "admin1234")
    t = read(s, 2.5)
    assert "Welcome" in t or "README" in t.upper(), t[:300]
    assert "Admin" in t, f"admin account should get the Admin sidebar entry:\n{t[:600]}"
    for _ in range(4):
        s.sendall(b"\x1b[B"); t = read(s, 0.7)
    assert "OPERATOR MENU" in t.upper(), t[:400]
    assert "Edit README" in t and "Add Room" in t
    print("[OK] admin / admin1234 logs in, sees Admin in the sidebar and reaches the operator menu")
    s.close()
    print("\n================ ADMIN TESTS PASSED ================")


if __name__ == "__main__":
    main()
