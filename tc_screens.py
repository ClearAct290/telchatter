# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# TelChatter - Copyright (C) 2026 ClearAct290-Reddit
"""TelChatter - screens (telnet "pages"). Each page_* returns the next state tuple."""
import asyncio
import os
import time

from tc_session import Nav, STATE_TABLE, PETSCII_TTYPES
from tc_render import (Grid, shorten, WHITE, BLACK, RED, LRED, YELLOW, GREEN, CYAN, ORANGE,
                       DGREY, MGREY, LGREY, LGREEN, BLUE, PURPLE, BROWN, NAME_COLORS)
from tc_db import USERNAME_RE
import tc_images
import tc_zmodem

NAME = "TELCHATTER"

DEFAULT_README = """\
Welcome to {server_name}!

This is a TelChatter bulletin board. Please keep things civil:

  - Be respectful. No harassment, hate speech, or targeted abuse.
  - No illegal content, including images.
  - Keep uploaded images appropriate for a general audience.
  - What you post is visible to everyone in the room, indefinitely.
  - The operators can remove content or access at their discretion.

Use the sidebar to pick a room. Have fun, and be nice to each other!

-- Powered by TelChatter - telnet chat with images, for ASCII and C64 --
"""


# ==================================================================== small helpers
def center_x(cols, w):
    return max(0, (cols - w) // 2)


def draw_logo(g, row=1):
    if g.cols >= 34 and g.rows >= 10:
        w = 30
        x = center_x(g.cols, w)
        g.text(row, x, "+" + "=" * (w - 2) + "+", CYAN, BLACK)
        g.text(row + 1, x, "|" + " " * (w - 2) + "|", CYAN, BLACK)
        g.text(row + 1, x + 6, "* TELCHATTER *", YELLOW, BLACK)
        g.text(row + 2, x, "+" + "=" * (w - 2) + "+", CYAN, BLACK)
        return row + 3
    g.center(row, NAME, YELLOW, BLACK)
    g.center(row + 1, "-" * len(NAME), DGREY, BLACK)
    return row + 2


def panel(g, r, c, h, w, title="", fg=DGREY, tfg=YELLOW):
    g.box(r, c, h, w, fg, BLACK)
    if title and w > len(title) + 6:
        g.text(r, c + 2, f" {title} ", tfg, BLACK)


def draw_header(g, title):
    g.fill(0, 0, 1, g.cols, bg=BLACK)
    g.center(0, f"[ {title} ]", YELLOW, BLACK)
    g.text(1, 0, "-" * g.cols, DGREY, BLACK)


def draw_field_box(g, r, c, w, label, value, focused, masked=False, color=WHITE):
    g.text(r, c, label, LGREY if not focused else YELLOW, BLACK)
    bx, by = c, r + 1
    fg = YELLOW if focused else DGREY
    g.box(by, bx, 3, w, fg, BLACK)
    shown = ("*" * len(value)) if masked else value
    g.text(by + 1, bx + 2, shown[-(w - 4):], color, BLACK)
    if focused:
        g.put(by + 1, bx + 2 + min(len(shown), w - 4), "_", YELLOW, BLACK)


def draw_button(g, r, c, label, focused):
    fg = BLACK if focused else YELLOW
    bg = YELLOW if focused else BLACK
    g.text(r, c, f"[{label}]", fg, bg)


def draw_link(g, r, c, label, focused):
    g.text(r, c, label, BLACK if focused else CYAN, YELLOW if focused else BLACK)


def text_char(k):
    """The character a typing field should insert for key `k`, or None if it is not text.
    The session reports the space bar as the key name "SPACE" (so buttons can react to it),
    which is not a single printable character - without this, spaces were silently dropped."""
    if k == "SPACE":
        return " "
    if isinstance(k, str) and len(k) == 1 and k.isprintable():
        return k
    return None


def is_quit_key(k):
    """Q in either case: a terminal in lower-case mode sends 'q', not 'Q'."""
    return k in ("Q", "q")


async def get_key_or_idle(session, timeout=None):
    try:
        return await session.get_key(timeout)
    except ConnectionError:
        raise


async def confirm_overlay(session, question, seconds=5.0, default_no=True, backdrop=None):
    sel = 1 if default_no else 0
    end = time.monotonic() + seconds
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        g = session.new_grid()
        if backdrop is not None:
            backdrop(g)
        w = max(len(question) + 4, 24)
        r0 = max(0, session.rows // 2 - 2)
        c0 = center_x(session.cols, w)
        g.box(r0, c0, 5, w, YELLOW, BLACK)
        g.center(r0 + 1, question, WHITE, BLACK, c0 + 1, w - 2)
        yes = "[ YES ]" if sel != 0 else "[>YES<]"
        no = "[ NO ]" if sel != 1 else "[>NO<]"
        g.text(r0 + 3, c0 + w // 2 - len(yes) - 1, yes, GREEN if sel == 0 else LGREY, BLACK)
        g.text(r0 + 3, c0 + w // 2 + 1, no, RED if sel == 1 else LGREY, BLACK)
        await session.present(g)
        k = await get_key_or_idle(session, min(0.5, max(0.05, remaining)))
        if k in ("LEFT", "RIGHT", "UP", "DOWN", "TAB"):
            sel = 1 - sel
        elif k == "ENTER":
            return sel == 0
        elif k == "ESC":
            return False


async def message_overlay(session, lines, seconds=None):
    """Show a boxed message; wait for ENTER/ESC, or auto-dismiss after `seconds`."""
    end = None if seconds is None else time.monotonic() + seconds
    w = max(max((len(l) for l in lines), default=0) + 4, 20)
    w = min(w, session.cols - 2)
    r0 = max(0, session.rows // 2 - len(lines) // 2 - 1)
    c0 = center_x(session.cols, w)
    while True:
        g = session.new_grid()
        g.box(r0, c0, len(lines) + 4, w, RED, BLACK)
        for i, line in enumerate(lines):
            g.center(r0 + 1 + i, line, WHITE, BLACK, c0 + 1, w - 2)
        g.center(r0 + len(lines) + 2, "press ENTER", DGREY, BLACK, c0 + 1, w - 2)
        await session.present(g)
        if end is None:
            timeout = None
        else:
            timeout = end - time.monotonic()
            if timeout <= 0:
                return                      # auto-dismiss: time is up
        k = await get_key_or_idle(session, timeout)
        if k in ("ENTER", "ESC"):
            return


# ==================================================================== terminal auto-detect
async def page_auto_detect(session):
    """Guess ASCII vs PETSCII from telnet negotiation. A genuine Commodore BBS terminal
    answers TTYPE (if at all) with something C64-flavoured and essentially never answers
    NAWS (no resizable window concept); a modern terminal almost always answers NAWS.
    If neither signal is conclusive, ask once in plain text."""
    session.chosen = True
    t = session.ttype_seen
    if any(h in t for h in PETSCII_TTYPES):
        # the client says outright that it is a Commodore terminal (some C64 telnet
        # programs also report a 40x25 window size, so NAWS must not veto this)
        session.set_petscii()
        session.clear_raw()
        return ("post_detect",)
    # Everything else: a window-size reply or an "xterm"/"ansi" terminal name is NOT proof of
    # an ANSI screen - WiFi modems and C64 telnet programs answer those too, and treating
    # them as ANSI is what dumped raw "[38;2;..." escape codes onto a real C64. Ask the
    # terminal itself: only something that can really interpret escape codes answers the
    # cursor-position request. No answer -> the plain-text 1/2 menu, which every client shows.
    if await _probe_ansi(session):
        session.set_ansi()
        session.clear_raw()
        return ("post_detect",)
    return ("choose_terminal",)


async def _probe_ansi(session, wait=1.2):
    """Send ESC[6n (report cursor position) and wait for the ESC[row;colR reply that only a
    real ANSI/VT terminal produces. A C64 (or anything that ignores escapes) stays silent."""
    session.cpr_seen = False
    session.write_raw(b"\x1b[6n")
    await session.flush()
    end = time.monotonic() + wait
    while time.monotonic() < end and not session.cpr_seen:
        await asyncio.sleep(0.03)
    return session.cpr_seen


async def page_choose_terminal(session):
    """Fallback when auto-detection is ambiguous. Plain 7-bit text + CR/LF only: no
    escape codes, no colour, no cursor movement, no clear-screen, so every client shows
    it correctly before we know what the client can do."""
    session.chosen = True
    sel = 0

    def draw():
        lines = ["", NAME, "",
                 ("> " if sel == 0 else "  ") + "1 = Generic ASCII (flexible size)",
                 ("> " if sel == 1 else "  ") + "2 = C64 PETSCII (40x25)",
                 "", "Press 1 or 2, or arrows + ENTER", ""]
        session.write_raw(("\r\n".join(lines) + "\r\n").encode("ascii", "replace"))

    while True:
        draw()
        await session.flush()
        while True:
            k = await session.get_key()
            if k in ("1", "2"):
                sel = int(k) - 1
                break
            if k in ("UP", "DOWN", "LEFT", "RIGHT", "TAB"):
                sel = 1 - sel
                break
            if k == "ENTER":
                break
        if k in ("1", "2", "ENTER"):
            if sel == 1:
                session.set_petscii()
            else:
                session.set_ansi()
            session.clear_raw()
            return ("post_detect",)


async def page_post_detect(session):
    """Right after we know ANSI vs PETSCII: if the telnet client handed over a preset
    username + password (NEW-ENVIRON USER / PASSWORD), use them -

      * account exists and the password matches -> straight to the home screen
      * account does not exist                  -> create it silently, then home
      * username taken (password differs)       -> create-account screen, username empty

    With no preset, ask whether to log in or create an account."""
    if session.environ_will and not (session.preset_user and session.preset_pass):
        # the client speaks NEW-ENVIRON but its answer may still be in flight
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not (session.preset_user and session.preset_pass):
            await asyncio.sleep(0.05)
    preset = session.preset_user, session.preset_pass
    if preset[0] and preset[1]:
        if session.app.login_locked(session.ip, preset[0]):
            return ("login_or_create",)      # the same brute-force lock-out as the login screen
        db = session.app.db
        uname = await asyncio.to_thread(db.authenticate, preset[0], [preset[1]])
        if uname is None:
            if await asyncio.to_thread(db.username_taken, preset[0]):
                # wrong password for a real account (including the admin account): count it
                # towards the same lock-out as the login screen, never try to "create" it
                session.app.login_failed(session.ip, preset[0])
                return ("create_account", "")
            ok, msg = await asyncio.to_thread(db.create_account, preset[0], preset[1])
            if ok:
                return await _finish_login(session, preset[0])
            return ("create_account", "")
        return await _finish_login(session, uname)
    return ("login_or_create",)


async def page_login_or_create(session):
    items = ["Log In", "Create Account"]
    sel = 0
    while True:
        g = session.new_grid()
        r = draw_logo(g, 1)
        g.center(r + 1, "No saved login found.", LGREY, BLACK)
        g.center(r + 2, "What would you like to do?", LGREY, BLACK)
        base = r + 5
        for i, label in enumerate(items):
            rr = base + i * 2
            w = 20
            c0 = center_x(session.cols, w)
            if i == sel:
                g.fill(rr, c0, 1, w, bg=YELLOW, fg=BLACK)
                g.text(rr, c0, ">", BLACK, YELLOW)
                g.center(rr, label.upper(), BLACK, YELLOW, c0, w)
            else:
                g.center(rr, label.upper(), CYAN, BLACK, c0, w)
        g.center(session.rows - 1, "ARROWS move  ENTER select  ESC quit", DGREY, BLACK)
        await session.present(g)
        k = await session.get_key()
        if k in ("UP", "DOWN", "LEFT", "RIGHT", "TAB"):
            sel = 1 - sel
        elif k == "ESC":
            raise ConnectionError("quit")
        elif k == "ENTER":
            return ("login", "") if sel == 0 else ("create_account", "")


# ==================================================================== login / create account
async def _finish_login(session, uname):
    db = session.app.db
    acct = await asyncio.to_thread(db.get_account, uname)
    session.username = uname
    session.is_operator = bool(acct and acct["is_operator"])
    session.name_color = (acct["name_color"] if acct and acct["name_color"] is not None and acct["name_color"] >= 0
                          else _auto_color(uname))
    session.app.online.add(session)
    return ("readme",)


def _auto_color(username):
    h = sum(ord(c) for c in username)
    return NAME_COLORS[h % len(NAME_COLORS)]


async def page_login(session, prefill_user=""):
    db = session.app.db
    username, password = prefill_user, ""
    show_pw = False
    focus = 0
    error = ""
    N_FIELDS = 5
    while True:
        g = session.new_grid()
        r = draw_logo(g, 0) + 1
        w = min(session.cols - 4, 40)
        c0 = center_x(session.cols, w)
        draw_field_box(g, r, c0, w, "USERNAME", username, focus == 0)
        draw_field_box(g, r + 4, c0, w, "PASSWORD", password, focus == 1, masked=not show_pw)
        box = "[X]" if show_pw else "[ ]"
        g.text(r + 8, c0, box + " show password", YELLOW if focus == 2 else LGREY, BLACK)
        draw_button(g, r + 10, c0, "LOGIN", focus == 3)
        draw_link(g, r + 12, c0, "Don't? Create One", focus == 4)
        if error:
            g.center(r + 14, error, RED, BLACK)
        g.center(session.rows - 1, "ARROWS/TAB move  ENTER select  ESC quit", DGREY, BLACK)
        await session.present(g)
        k = await session.get_key()
        if k is None:
            continue
        if k == "UP":
            focus = (focus - 1) % N_FIELDS
        elif k in ("DOWN", "TAB"):
            focus = (focus + 1) % N_FIELDS
        elif k == "BACKSPACE":
            if focus == 0:
                username = username[:-1]
            elif focus == 1:
                password = password[:-1]
        elif k == "SPACE" and focus == 2:
            show_pw = not show_pw
        elif k == "ESC":
            raise ConnectionError("quit at login")
        elif k == "ENTER":
            if focus == 2:
                show_pw = not show_pw
            elif focus == 4:
                return ("create_account", username)
            elif focus in (0, 1):
                focus += 1
            elif focus == 3:
                if not username:
                    error = "Enter a username"
                else:
                    locked = session.app.login_locked(session.ip, username)
                    if locked:
                        error = f"Too many tries - wait {int(locked) + 1}s"
                    else:
                        cands = [password]
                        if session.petscii:
                            cands += [x for x in (password.swapcase(), password.lower(), password.upper())
                                      if x not in cands]
                        uname = await asyncio.to_thread(db.authenticate, username, cands)
                        if uname is None:
                            session.app.login_failed(session.ip, username)
                            error = "Wrong username or password"
                        else:
                            session.app.login_ok(session.ip, username)
                            return await _finish_login(session, uname)
        else:
            ch = text_char(k)
            if ch is None:
                continue
            if focus == 0 and ch != " " and len(username) < 16:     # usernames never contain spaces
                username += ch
            elif focus == 1 and len(password) < 32:
                password += ch


async def page_create_account(session, prefill_user=""):
    db = session.app.db
    username, password, confirm = prefill_user, "", ""
    show_pw = False
    focus = 0
    error = ""
    N_FIELDS = 6
    while True:
        g = session.new_grid()
        r = draw_logo(g, 0)
        w = min(session.cols - 4, 40)
        c0 = center_x(session.cols, w)
        draw_field_box(g, r, c0, w, "USERNAME", username, focus == 0)
        draw_field_box(g, r + 4, c0, w, f"PASSWORD ({__import__('tc_db').PASSWORD_MIN}+ chars)",
                       password, focus == 1, masked=not show_pw)
        draw_field_box(g, r + 8, c0, w, "CONFIRM PASSWORD", confirm, focus == 2, masked=not show_pw)
        box = "[X]" if show_pw else "[ ]"
        g.text(r + 12, c0, box + " show password", YELLOW if focus == 3 else LGREY, BLACK)
        draw_button(g, r + 13, c0, "CREATE", focus == 4)
        draw_link(g, r + 14, c0, "Already? Login", focus == 5)
        if error:
            g.center(r + 16, error, RED, BLACK)
        g.center(session.rows - 1, "ARROWS/TAB move  ENTER select  ESC quit", DGREY, BLACK)
        await session.present(g)
        k = await session.get_key()
        if k is None:
            continue
        if k == "UP":
            focus = (focus - 1) % N_FIELDS
        elif k in ("DOWN", "TAB"):
            focus = (focus + 1) % N_FIELDS
        elif k == "BACKSPACE":
            if focus == 0:
                username = username[:-1]
            elif focus == 1:
                password = password[:-1]
            elif focus == 2:
                confirm = confirm[:-1]
        elif k == "SPACE" and focus == 3:
            show_pw = not show_pw
        elif k == "ESC":
            raise ConnectionError("quit at create account")
        elif k == "ENTER":
            if focus == 3:
                show_pw = not show_pw
            elif focus == 5:
                return ("login", username)
            elif focus in (0, 1, 2):
                focus += 1
            elif focus == 4:
                if password != confirm:
                    error = "Passwords don't match"
                elif not username:
                    error = "Enter a username"
                else:
                    ok, msg = await asyncio.to_thread(db.create_account, username, password)
                    if not ok:
                        error = msg
                        if "taken" in msg.lower():
                            username = ""  # per spec: stay on screen, username box emptied
                    else:
                        await message_overlay(session, ["ACCOUNT CREATED"], seconds=1.6)
                        return ("login", username)
        else:
            ch = text_char(k)
            if ch is None:
                continue
            if focus == 0 and ch != " " and len(username) < 16:     # usernames never contain spaces
                username += ch
            elif focus == 1 and len(password) < 32:
                password += ch
            elif focus == 2 and len(confirm) < 32:
                confirm += ch


# ==================================================================== shared chrome: sidebar + status bar
def _sidebar_items(session):
    rooms = session.app.room_list()
    items = [("readme", "Readme")]
    for r in rooms:
        items.append((("room", r["id"]), shorten(r["name"], 16)))
    if session.is_operator:
        items.append(("admin", "Admin"))
    return items


def _fmt_hms(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def draw_sidebar(g, session, sel_key, top, left, height, width):
    panel(g, top, left, height, width, "", MGREY)
    items = _sidebar_items(session)
    for i, (key, label) in enumerate(items):
        rr = top + 1 + i
        if rr >= top + height - 1:
            break
        active = (key == sel_key)
        if active:
            g.fill(rr, left + 1, 1, width - 2, bg=YELLOW, fg=BLACK)
            g.text(rr, left + 2, label[:width - 3], BLACK, YELLOW)
        else:
            col = ORANGE if key == "admin" else CYAN
            g.text(rr, left + 2, label[:width - 3], col, BLACK)
    return items


def draw_status_bar(g, session, controls_hint):
    """Bottom line: controls on the left, users online / time online / UTC clock on the
    right.  Narrow screens (C64 40 columns) get a compact clock and as many whole
    control hints as fit."""
    row = g.rows - 1
    g.text(row - 1, 0, "-" * g.cols, DGREY, BLACK)
    online_n = len(session.app.online)
    utc = time.strftime("%H:%M:%S UTC", time.gmtime())
    uptime = _fmt_hms(session.uptime_seconds())
    if g.cols >= 80:
        right = f"ONLINE:{online_n}  TIME ON:{uptime}  {utc}"
    else:
        right = f"{online_n} on  {uptime}  {utc[:5]}Z"
    room = max(0, g.cols - len(right) - 2)
    left = ""
    for item in (x for x in controls_hint.split("  ") if x):
        cand = item if not left else left + "  " + item
        if len(cand) > room:
            break
        left = cand
    g.text(row, 0, left, DGREY, BLACK)
    g.text(row, max(0, g.cols - len(right)), right, LGREY, BLACK)


# ==================================================================== home / readme
def ensure_readme_file(cfg):
    """Write the default readme next to the server on first run so the host can simply
    edit the file (it is re-read every time someone opens the Readme page)."""
    path = cfg.paths_readme
    if path and not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(DEFAULT_README.format(server_name=cfg.server_name))
        except OSError:
            pass


def _load_readme(session):
    try:
        with open(session.cfg.paths_readme, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if text.strip():
            return text
    except OSError:
        pass
    custom = session.app.db.get_setting("readme", "")      # only used if the file can't be written
    if custom:
        return custom
    return DEFAULT_README.format(server_name=session.cfg.server_name)


async def page_readme(session):
    text = _load_readme(session)
    lines = []
    for para in text.split("\n"):
        lines.append(para)
    scroll = 0
    sidebar_w = min(18, max(12, session.cols // 5))
    content_w = session.cols - sidebar_w - 1
    while True:
        g = session.new_grid()
        draw_header(g, session.cfg.server_name)
        body_top, body_h = 2, g.rows - 4
        draw_sidebar(g, session, "readme", body_top, 0, body_h, sidebar_w)
        cx = sidebar_w + 1
        panel(g, body_top, cx, body_h, content_w, "README")
        inner_w = content_w - 4
        wrapped = []
        for line in lines:
            if not line:
                wrapped.append("")
                continue
            words = line.split(" ")
            cur = ""
            for w in words:
                trial = (cur + " " + w).strip()
                if len(trial) > inner_w:
                    wrapped.append(cur)
                    cur = w
                else:
                    cur = trial
            wrapped.append(cur)
        max_scroll = max(0, len(wrapped) - (body_h - 2))
        scroll = max(0, min(scroll, max_scroll))
        for i in range(body_h - 2):
            idx = scroll + i
            if idx < len(wrapped):
                g.text(body_top + 1 + i, cx + 2, wrapped[idx][:inner_w], LGREY, BLACK)
        draw_status_bar(g, session, "ARROWS nav  [ ] scroll  ENTER select  Q disconnect")
        await session.present(g)
        k = await get_key_or_idle(session, 1.0)
        if k is None:
            if session.idle_seconds() > float(session.cfg.session_idle_timeout_minutes) * 60:
                raise ConnectionError("idle timeout")
            if session.uptime_seconds() > float(session.cfg.session_max_session_hours) * 3600:
                raise ConnectionError("max session time")
            continue
        if session.uptime_seconds() > float(session.cfg.session_max_session_hours) * 3600:
            raise ConnectionError("max session time")
        if k == "[":
            scroll = max(0, scroll - (body_h - 3))
        elif k == "]":
            scroll = min(max_scroll, scroll + (body_h - 3))
        elif k == "DOWN":
            return await _sidebar_nav(session, "readme", +1)
        elif k == "UP":
            return await _sidebar_nav(session, "readme", -1)
        elif k == "ENTER":
            return ("readme",)  # no-op re-select; stays here
        elif is_quit_key(k):
            if await confirm_overlay(session, "Disconnect?"):
                raise ConnectionError("user quit")


async def _sidebar_nav(session, current_key, direction):
    items = _sidebar_items(session)
    keys = [k for k, _ in items]
    try:
        i = keys.index(current_key)
    except ValueError:
        i = 0
    i = (i + direction) % len(keys)
    nk = keys[i]
    return _state_for_key(nk)


def _state_for_key(key):
    if key == "readme":
        return ("readme",)
    if key == "admin":
        return ("admin_menu",)
    if isinstance(key, tuple) and key[0] == "room":
        return ("room", key[1])
    return ("readme",)


# ==================================================================== chat room
class _RoomLayout:
    """Geometry for one room screen, computed from the session's current size."""
    def __init__(self, session):
        self.sidebar_w = min(18, max(12, session.cols // 5))
        self.cx = self.sidebar_w + 1
        self.content_w = session.cols - self.cx
        self.body_top = 2
        self.input_h = 4
        self.body_h = session.rows - 3 - self.input_h
        self.hist_inner_w = self.content_w - 4   # inside the box, minus the scrollbar column
        self.scrollbar_col = self.cx + self.content_w - 2


def _wrap_message(prefix, body, width):
    """Wrap one chat line (username prefix + body) to `width` columns; the prefix only
    appears on the first physical line, continuation lines are indented to match."""
    indent = " " * min(len(prefix), max(0, width - 10))
    first_avail = max(4, width - len(prefix))
    cont_avail = max(4, width - len(indent))
    words = body.split(" ")
    out_lines = []
    cur = ""
    avail = first_avail
    is_first = True
    for w in words:
        while len(w) > avail:  # a single very long "word" (e.g. a URL) gets hard-broken
            piece = w[:avail]
            out_lines.append((prefix if is_first else indent) + piece)
            is_first = False
            w = w[avail:]
            avail = cont_avail
        trial = (cur + " " + w).strip() if cur else w
        if len(trial) > avail:
            out_lines.append((prefix if is_first else indent) + cur)
            is_first = False
            avail = cont_avail
            cur = w
        else:
            cur = trial
    out_lines.append((prefix if is_first else indent) + cur)
    return out_lines, len(prefix if out_lines[:1] else indent)


def _build_room_lines(session, messages, width, max_art_h=22):
    """Turn stored messages into a flat list of render rows - exactly ONE screen row each,
    so scrolling and "pinned to the newest message" work in rows:
       ("text", line, color, prefix_len) | ("art_header", line, color, 0) |
       ("art", image_id, row_index, disp_w, disp_h)
    An image contributes disp_h "art" rows, each knowing its own row_index into the
    (live-resampled) frame so only the visible slice is drawn and animated."""
    db = session.app.db
    rows = []
    for m in messages:
        who = m["username"]
        color = m["name_color"] if m["name_color"] in range(16) else WHITE
        ts = time.strftime("%H:%M", time.localtime(m["created_at"]))
        prefix = f"[{ts}] {who}: "
        if m["kind"] == "text":
            wrapped, _ = _wrap_message(prefix, m["body"] or "", width)
            for i, line in enumerate(wrapped):
                plen = len(prefix) if i == 0 else 0
                rows.append(("text", line, color, plen))
        else:
            rows.append(("art_header", f"[{ts}] {who} sent an image" +
                         (f": {m['body']}" if m["body"] else ""), color, 0))
            art = _load_art(db, m["image_id"])
            if art is None:
                rows.append(("text", "(image unavailable)", DGREY, 0))
                continue
            art_cols, art_rows, _ms, _frames, _anim = art
            disp_w, disp_h = _art_display_size(art_cols, art_rows, width, max_art_h)
            for r in range(disp_h):
                rows.append(("art", m["image_id"], r, disp_w, disp_h))
    return rows


_ART_CACHE = {}  # image_id -> loaded blob tuple, small LRU-ish cache


def _load_art(db, image_id):
    cached = _ART_CACHE.get(image_id)
    if cached is not None:
        return cached
    row = db.get_image(image_id)
    if not row:
        return None
    loaded = tc_images.load_blob(row["data"])
    if loaded is None:
        return None
    cols, rows_, frame_ms, frames = loaded
    result = (cols, rows_, frame_ms, frames, bool(row["animated"]))
    if len(_ART_CACHE) > 40:
        _ART_CACHE.pop(next(iter(_ART_CACHE)))
    _ART_CACHE[image_id] = result
    return result


def _art_display_size(art_cols, art_rows, max_w, max_h_cells=22):
    """How big to draw one piece of art inline: fit within the available width, and cap
    the height so one image can't force endless scrolling through a single picture."""
    if art_cols <= max_w:
        disp_w = art_cols
        disp_h = art_rows
    else:
        disp_w = max_w
        disp_h = max(1, round(art_rows * (max_w / art_cols)))
    if disp_h > max_h_cells:
        disp_w = max(1, round(disp_w * (max_h_cells / disp_h)))
        disp_h = max_h_cells
    return disp_w, disp_h


async def page_room(session, room_id):
    db = session.app.db
    room = session.app.room_by_id(room_id)
    if room is None:
        return ("readme",)
    session.room_id = room_id
    typed = ""
    input_focus = 0  # 0 = text box, 1 = send, 2 = upload
    pending_art = None  # converted upload waiting in the input box until SEND (IMAGE again deletes it)
    scroll = 0        # 0 = pinned to bottom (newest); >0 = that many rows scrolled up
    error = ""
    last_count = -1
    layout = _RoomLayout(session)
    cached_rows = []
    anim_t0 = time.monotonic()

    def rebuild():
        nonlocal cached_rows, last_count
        limit = min(int(session.cfg.rooms_scrollback_max), max(int(session.cfg.rooms_scrollback_default), 1))
        msgs = db.recent_messages(room_id, limit)
        cached_rows = _build_room_lines(session, msgs, layout.hist_inner_w,
                                        max(6, min(22, layout.body_h - 4)))
        last_count = db.message_count(room_id)

    rebuild()

    while True:
        new_layout = _RoomLayout(session)
        if ((new_layout.hist_inner_w != layout.hist_inner_w) or (new_layout.body_h != layout.body_h)
                or (db.message_count(room_id) != last_count)):
            layout = new_layout
            rebuild()
        layout = new_layout

        g = session.new_grid()
        draw_header(g, f"{session.cfg.server_name} - {room['name']}")
        draw_sidebar(g, session, ("room", room_id), layout.body_top, 0, layout.body_h + layout.input_h, layout.sidebar_w)
        panel(g, layout.body_top, layout.cx, layout.body_h, layout.content_w, room["name"])

        visible_h = layout.body_h - 2
        total = len(cached_rows)
        max_scroll = max(0, total - visible_h)
        scroll = max(0, min(scroll, max_scroll))
        start = max(0, total - visible_h - scroll)
        end = min(total, start + visible_h)

        # typing/highlighting (not scrolling) gets an outline instead of full-colour
        # highlight per spec; we treat "focus is on the input row" as that state
        outline_mode = input_focus != 0 or not typed   # scroll mode (not typing): outline the history
        if outline_mode:
            for x in range(layout.cx + 1, layout.cx + layout.content_w - 1):
                g.put(layout.body_top, x, "-", CYAN, BLACK)
                g.put(layout.body_top + layout.body_h - 1, x, "-", CYAN, BLACK)

        now = time.monotonic()
        animating = False
        frame_cache = {}   # image_id -> resampled (chars, colors) for this pass
        np = tc_images.np if hasattr(tc_images, "np") else __import__("numpy")
        for i in range(start, end):
            screen_row = layout.body_top + 1 + (i - start)
            row = cached_rows[i]
            kind = row[0]
            if kind == "text":
                _, line, color, plen = row
                if plen:
                    ts_end = line.find("] ") + 2
                    name_end = line.find(": ", ts_end) + 1
                    g.text(screen_row, layout.cx + 2, line[:ts_end], DGREY, BLACK)
                    g.text(screen_row, layout.cx + 2 + ts_end, line[ts_end:name_end], color, BLACK)
                    g.text(screen_row, layout.cx + 2 + name_end, line[name_end:layout.hist_inner_w], LGREY, BLACK)
                else:
                    g.text(screen_row, layout.cx + 2, line[:layout.hist_inner_w], LGREY, BLACK)
            elif kind == "art_header":
                _, line, color, _ = row
                g.text(screen_row, layout.cx + 2, line[:layout.hist_inner_w], color, BLACK)
            elif kind == "art":
                _, image_id, ri, disp_w, disp_h = row
                art = _load_art(db, image_id)
                if art is None:
                    continue
                art_cols, art_rows, frame_ms, frames, animated = art
                if image_id not in frame_cache:
                    if animated and frame_ms > 0 and len(frames) > 1:
                        fi = int(((now - anim_t0) * 1000.0 / frame_ms)) % len(frames)
                        animating = True
                    else:
                        fi = 0
                    chars, colors = frames[fi]
                    frame_cache[image_id] = tc_images.resample_frame(chars, colors, disp_w, disp_h)
                rchars, rcolors = frame_cache[image_id]
                ch = rchars[ri:ri + 1].astype("int16")
                fg = rcolors[ri:ri + 1].astype("int16")
                g.blit(screen_row, layout.cx + 2, ch, fg, np.zeros_like(fg))

        # scrollbar
        if total > visible_h:
            bar_h = max(1, int(visible_h * visible_h / total))
            bar_pos = int((visible_h - bar_h) * (1 - scroll / max(1, max_scroll)))
            for y in range(visible_h):
                ch = "#" if bar_pos <= y < bar_pos + bar_h else ":"
                g.put(layout.body_top + 1 + y, layout.scrollbar_col, ch,
                     YELLOW if ch == "#" else DGREY, BLACK)

        # input area: 3 options - text box, send, image upload
        in_top = layout.body_top + layout.body_h
        panel(g, in_top, layout.cx, layout.input_h, layout.content_w, "")
        box_w = layout.content_w - 2 - 10 - 10 - 2
        g.text(in_top, layout.cx + 2, "TYPE A MESSAGE:", YELLOW if input_focus == 0 else LGREY, BLACK)
        fgc = YELLOW if input_focus == 0 else DGREY
        g.box(in_top + 1, layout.cx + 1, 2, box_w, fgc, BLACK)
        shown = typed[-(box_w - 3):]
        g.text(in_top + 1, layout.cx + 2, shown, WHITE, BLACK)
        if input_focus == 0:
            g.put(in_top + 1, layout.cx + 2 + min(len(shown), box_w - 3), "_", YELLOW, BLACK)
        send_x = layout.cx + 1 + box_w + 1
        draw_button(g, in_top + 1, send_x, "SEND", input_focus == 1)
        up_x = send_x + 9
        draw_button(g, in_top + 1, up_x, "IMAGE OK" if pending_art else "IMAGE", input_focus == 2)
        if error:
            g.text(in_top + 2, layout.cx + 2, error[:layout.content_w - 4], RED, BLACK)
        elif pending_art:
            kind = "GIF" if pending_art["animated"] else pending_art["fmt"]
            g.text(in_top + 2, layout.cx + 2,
                   f"Image uploaded ({kind}). SEND posts it - IMAGE deletes it."[:layout.content_w - 4],
                   GREEN, BLACK)

        if input_focus == 0:
            hint = "ENTER send  TAB buttons  [ ] scroll  UP/DOWN rooms"
        else:
            hint = "ARROWS/TAB move  ENTER select  [ ] scroll  Q disconnect"
        draw_status_bar(g, session, hint)
        await session.present(g)

        timeout = 0.15 if animating else 1.0   # 1 s tick keeps the status-bar clock / online timer live
        k = await get_key_or_idle(session, timeout)
        if k is None:
            if session.idle_seconds() > float(session.cfg.session_idle_timeout_minutes) * 60:
                raise ConnectionError("idle timeout")
            if session.uptime_seconds() > float(session.cfg.session_max_session_hours) * 3600:
                raise ConnectionError("max session time")
            continue
        if session.uptime_seconds() > float(session.cfg.session_max_session_hours) * 3600:
            raise ConnectionError("max session time")

        typing = input_focus == 0
        if k == "SPACE" and not typing:
            k = "ENTER"                        # space bar presses the focused SEND / IMAGE button
        if is_quit_key(k) and not typing:
            if await confirm_overlay(session, "Disconnect?"):
                raise ConnectionError("user quit")
            continue
        # [ and ] scroll only when you are not typing (box empty, or focus on SEND / IMAGE);
        # otherwise they are ordinary characters you can put in a message.
        can_scroll = input_focus != 0 or not typed
        if k in ("[", "]") and can_scroll:
            step = max(3, visible_h // 2)
            scroll = min(max_scroll, scroll + step) if k == "[" else max(0, scroll - step)
            continue
        if k == "TAB":
            input_focus = (input_focus + 1) % 3
            continue
        if k == "UP":
            if input_focus == 0:
                return await _sidebar_nav(session, ("room", room_id), -1)
            input_focus = (input_focus - 1) % 3
            continue
        if k == "DOWN":
            if input_focus == 0 and not typed:
                return await _sidebar_nav(session, ("room", room_id), +1)
            input_focus = (input_focus + 1) % 3
            continue
        if k in ("LEFT", "RIGHT"):
            input_focus = (input_focus + (1 if k == "RIGHT" else -1)) % 3
            continue
        if k == "BACKSPACE" and input_focus == 0:
            typed = typed[:-1]
            continue
        if k == "ESC":
            continue
        if k == "ENTER":
            if input_focus == 1 or (input_focus == 0 and (typed or pending_art)):
                body = typed.strip()
                if body or pending_art:
                    if body:
                        db.add_text_message(room_id, session.username, session.name_color, body[:500])
                    if pending_art:
                        art = pending_art
                        image_id = await asyncio.to_thread(
                            db.store_image, session.username, art["fmt"], art["animated"],
                            art["cols"], art["rows"], art["frame_ms"], art["data"])
                        db.add_image_message(room_id, session.username, session.name_color, image_id)
                        pending_art = None
                    typed = ""
                    scroll = 0
                    error = ""
                    input_focus = 0
                continue
            if input_focus == 2:
                if pending_art:
                    # "press the box again to delete it"
                    if await confirm_overlay(session, "Delete uploaded image?", seconds=8.0, default_no=True):
                        pending_art = None
                    continue
                status, art = await page_image_upload(session, room_id)
                if status == "invalid":
                    error = "Invalid file format"
                elif status == "ok":
                    pending_art = art
                    error = ""
                    input_focus = 1
                else:
                    error = ""
                scroll = 0
                continue
            continue
        ch = text_char(k)
        if input_focus == 0 and ch is not None and len(typed) < 480:
            typed += ch
            error = ""


# ==================================================================== image upload
async def page_image_upload(session, room_id):
    """The full upload flow, over real ZMODEM rather than "paste the file as fast ASCII
    input": that trick went through the same path as ordinary typing, so a client could
    rewrite line endings or otherwise mangle the bytes with no way for us to tell: it
    worked until the moment it didn't, with no useful error. ZMODEM block-checksums every
    chunk and automatically re-sends a damaged one (see tc_zmodem.py), and because it is a
    real binary transfer rather than simulated keystrokes, the telnet BINARY mode already
    negotiated for this connection actually applies to it. There is no "inputs locked for
    N seconds" step any more: the protocol itself knows when the file is finished, so
    ESC cancels immediately, any time before the transfer completes.

    Returns (status, art) where status is "ok", "cancelled" or "invalid"; art is the
    converted image dict only for "ok".  The caller keeps it in the input box as
    "uploaded" until the user sends it or deletes it."""
    if not session.cfg.images_enabled:
        await message_overlay(session, ["Image uploads are disabled", "on this server"], seconds=2.5)
        return "cancelled", None

    def _intro(g):
        draw_header(g, "UPLOAD IMAGE (ZMODEM)")
        g.center(session.rows // 2 - 6, "Send a picture from your terminal over ZMODEM.", WHITE, BLACK)
        g.center(session.rows // 2 - 5, "(PNG, BMP, GIF/animated GIF, JPEG, ...)", DGREY, BLACK)
        g.center(session.rows // 2 + 4, "SyncTERM opens its ZMODEM upload window by itself;", DGREY, BLACK)
        g.center(session.rows // 2 + 5, "if it does not, use Upload -> ZModem.", DGREY, BLACK)

    g = session.new_grid()
    _intro(g)
    await session.present(g, force=True)
    if not await confirm_overlay(session, "Ready to upload?", seconds=15.0, default_no=False, backdrop=_intro):
        return "cancelled", None

    max_bytes = int(session.cfg.images_max_upload_bytes)
    timeout_s = float(session.cfg.images_upload_timeout_seconds)
    escape_ctrl = bool(session.cfg.zmodem_escape_ctrl)
    rz_prefix = bool(session.cfg.zmodem_rz_prefix)

    def _zlog(msg):
        session.rawlog.log(session.ip, "zmodem", msg)

    recv = tc_zmodem.ZmodemReceiver(send=session.write_raw, max_bytes=max_bytes,
                                     escape_ctrl=escape_ctrl, log=_zlog)

    # Draw the waiting screen FIRST. From the moment our ZRINIT goes out, the wire belongs
    # to ZMODEM: a terminal such as SyncTERM starts its own transfer window on seeing it and
    # reads every following byte as protocol, so a screen redraw sent after the header
    # (ESC [ ...) made it reject the header as INVALID. Nothing but protocol frames is sent
    # until the transfer is over - the terminal shows its own progress meanwhile.
    g = session.new_grid()
    draw_header(g, "UPLOAD IMAGE (ZMODEM)")
    g.center(session.rows // 2 - 1, "Waiting for your terminal to send...", YELLOW, BLACK)
    g.center(session.rows // 2 + 1, "ESC cancels", DGREY, BLACK)
    await session.present(g, force=True)
    await session.flush()
    if rz_prefix:
        session.write_raw(b"rz\r")        # optional extra "rz" text some terminals watch for
    await session.start_zmodem(recv)

    start = time.monotonic()
    user_cancelled = False
    try:
        while not recv.done:
            if session.closed:
                raise ConnectionError("closed during upload")
            now = time.monotonic()
            if now - start > timeout_s:
                recv.cancel("timed out waiting for the transfer")
                break
            recv.poll()
            k = session.try_key()
            if k is not None and k[0] == "ESC" and not recv.started:
                user_cancelled = True
                recv.cancel("cancelled before the transfer began")
                break
            await asyncio.sleep(0.05)
        if recv.finished and not recv.over_and_out:
            # sz-style senders end with "OO" right after the final handshake: take it in
            # before detaching, so it cannot show up as typed keys afterwards
            t_oo = time.monotonic() + 0.5
            while not recv.over_and_out and time.monotonic() < t_oo and not session.closed:
                await asyncio.sleep(0.03)
    finally:
        session.stop_zmodem()
        session.keys.clear()           # anything left over came from the transfer, not the user
        session.presenter.reset()      # the terminal drew its own transfer window: repaint everything next
        session.last_input_at = time.monotonic()   # the user was busy uploading: not idle

    if user_cancelled:
        _zlog("upload-result: cancelled by the user before the transfer began")
        return "cancelled", None
    if recv.failed or not recv.files:
        reason = recv.failed or recv.error or "no file was received"
        _zlog(f"upload-result: not completed: {reason}")
        if recv.started or recv.failed:
            await message_overlay(session, ["Upload did not complete", reason[:48]], seconds=3.0)
            return "invalid", None
        return "cancelled", None

    name, raw = recv.files[0]
    _zlog(f"upload-result: received {name!r} {len(raw)}B via ZMODEM; converting...")
    attempts = []
    try:
        art = await asyncio.to_thread(
            tc_images.convert_upload_any, raw,
            int(session.cfg.images_max_art_cols), int(session.cfg.images_max_art_rows),
            int(session.cfg.images_gif_max_fps), int(session.cfg.images_motion_threshold),
            attempts)
    except Exception as e:                   # BadImage or anything the decoder chokes on
        tried = "; ".join(f"[{i}] {n}B -> {err}" for i, (n, err) in enumerate(attempts))
        _zlog(f"upload-result: invalid: {type(e).__name__}: {e}\n"
              f"    raw: {session.rawlog.hexdump(raw)}\n"
              f"    candidates tried: {tried or '(none)'}")
        await message_overlay(session, ["Invalid file format"], seconds=2.5)
        return "invalid", None
    _zlog(f"upload-result: ok: fmt={art.get('fmt')} animated={art.get('animated')} "
          f"cols={art.get('cols')} rows={art.get('rows')}")
    return "ok", art


# ==================================================================== admin
async def page_admin_menu(session):
    if not session.is_operator:
        return ("readme",)
    items = ["Edit README", "Rename a Room", "Add Room", "Remove Room", "Back"]
    sel = 0
    while True:
        g = session.new_grid()
        draw_header(g, "ADMIN")
        draw_sidebar(g, session, "admin", 2, 0, g.rows - 4, min(18, max(12, session.cols // 5)))
        cx = min(18, max(12, session.cols // 5)) + 1
        panel(g, 2, cx, g.rows - 4, session.cols - cx, "OPERATOR MENU")
        for i, label in enumerate(items):
            rr = 4 + i * 2
            if i == sel:
                g.fill(rr, cx + 2, 1, 24, bg=YELLOW, fg=BLACK)
                g.text(rr, cx + 2, "> " + label, BLACK, YELLOW)
            else:
                g.text(rr, cx + 2, "  " + label, CYAN, BLACK)
        draw_status_bar(g, session, "ARROWS nav  ENTER select  Q disconnect")
        await session.present(g)
        k = await session.get_key()
        if k in ("UP",):
            sel = (sel - 1) % len(items)
        elif k in ("DOWN", "TAB"):
            sel = (sel + 1) % len(items)
        elif is_quit_key(k):
            if await confirm_overlay(session, "Disconnect?"):
                raise ConnectionError("user quit")
        elif k == "ESC":
            return ("readme",)
        elif k == "ENTER":
            if items[sel] == "Back":
                return ("readme",)
            if items[sel] == "Edit README":
                return ("admin_edit_readme",)
            if items[sel] == "Rename a Room":
                return ("admin_rename_room",)
            if items[sel] == "Add Room":
                return ("admin_add_room",)
            if items[sel] == "Remove Room":
                return ("admin_remove_room",)


async def _text_entry_screen(session, title, initial="", max_len=2000, multiline=False):
    """A simple full-screen text box editor; ENTER on a single-line field submits,
    on a multi-line field inserts a newline (use a trailing blank-line + ENTER, or
    ESC, to submit a multi-line body - kept intentionally simple for a BBS admin tool)."""
    text = initial
    while True:
        g = session.new_grid()
        draw_header(g, title)
        w = session.cols - 4
        h = session.rows - 5
        panel(g, 2, 2, h, w, "")
        inner_w = w - 4
        lines = text.split("\n") if multiline else [text]
        shown_lines = []
        for line in lines:
            while len(line) > inner_w:
                shown_lines.append(line[:inner_w])
                line = line[inner_w:]
            shown_lines.append(line)
        for i, line in enumerate(shown_lines[-(h - 2):]):
            g.text(3 + i, 4, line, WHITE, BLACK)
        hint = "ENTER=save  ESC=cancel" if not multiline else "ESC=save & exit  (plain text)"
        g.center(session.rows - 1, hint, DGREY, BLACK)
        await session.present(g, force=True)
        k = await session.get_key()
        if k == "ESC":
            return text
        if k == "ENTER":
            if multiline:
                text += "\n"
            else:
                return text
        elif k == "BACKSPACE":
            text = text[:-1]
        else:
            ch = text_char(k)
            if ch is not None and len(text) < max_len:
                text += ch


async def page_admin_edit_readme(session):
    if not session.is_operator:
        return ("readme",)
    db = session.app.db
    current = _load_readme(session)
    new_text = await _text_entry_screen(session, "EDIT README (ESC to save)", current, max_len=4000, multiline=True)
    try:
        with open(session.cfg.paths_readme, "w", encoding="utf-8") as f:
            f.write(new_text)
    except OSError:
        db.set_setting("readme", new_text)
    return ("admin_menu",)


async def page_admin_rename_room(session):
    if not session.is_operator:
        return ("readme",)
    rooms = session.app.room_list()
    if not rooms:
        return ("admin_menu",)
    sel = 0
    while True:
        g = session.new_grid()
        draw_header(g, "RENAME A ROOM")
        for i, r in enumerate(rooms):
            rr = 3 + i * 2
            marker = ">" if i == sel else " "
            col = YELLOW if i == sel else CYAN
            g.text(rr, 3, f"{marker} {r['name']}", col, BLACK)
        g.center(session.rows - 1, "ARROWS select  ENTER rename  ESC back", DGREY, BLACK)
        await session.present(g, force=True)
        k = await session.get_key()
        if k == "UP":
            sel = (sel - 1) % len(rooms)
        elif k in ("DOWN", "TAB"):
            sel = (sel + 1) % len(rooms)
        elif k == "ESC":
            return ("admin_menu",)
        elif k == "ENTER":
            new_name = await _text_entry_screen(session, f"NEW NAME FOR '{rooms[sel]['name']}'",
                                                initial=rooms[sel]["name"], max_len=24)
            new_name = new_name.strip()[:24]
            if new_name:
                session.app.db.rename_room(rooms[sel]["id"], new_name)
                session.app.refresh_rooms()
            return ("admin_menu",)


async def page_admin_add_room(session):
    if not session.is_operator:
        return ("readme",)
    name = await _text_entry_screen(session, "NEW ROOM NAME", initial="", max_len=24)
    name = name.strip()[:24]
    if name:
        session.app.db.add_room(name)
        session.app.refresh_rooms()
    return ("admin_menu",)


async def page_admin_remove_room(session):
    if not session.is_operator:
        return ("readme",)
    rooms = session.app.room_list()
    if not rooms:
        return ("admin_menu",)
    sel = 0
    while True:
        g = session.new_grid()
        draw_header(g, "REMOVE A ROOM")
        for i, r in enumerate(rooms):
            rr = 3 + i * 2
            marker = ">" if i == sel else " "
            col = YELLOW if i == sel else CYAN
            g.text(rr, 3, f"{marker} {r['name']}", col, BLACK)
        g.center(session.rows - 1, "ARROWS select  ENTER remove  ESC back", DGREY, BLACK)
        await session.present(g, force=True)
        k = await session.get_key()
        if k == "UP":
            sel = (sel - 1) % len(rooms)
        elif k in ("DOWN", "TAB"):
            sel = (sel + 1) % len(rooms)
        elif k == "ESC":
            return ("admin_menu",)
        elif k == "ENTER":
            if await confirm_overlay(session, f"Delete '{rooms[sel]['name']}'?"):
                session.app.db.remove_room(rooms[sel]["id"])
                session.app.refresh_rooms()
            return ("admin_menu",)


# ==================================================================== state table
STATE_TABLE.update({
    "auto_detect": page_auto_detect,
    "choose_terminal": page_choose_terminal,
    "post_detect": page_post_detect,
    "login_or_create": page_login_or_create,
    "login": page_login,
    "create_account": page_create_account,
    "readme": page_readme,
    "room": page_room,
    "admin_menu": page_admin_menu,
    "admin_edit_readme": page_admin_edit_readme,
    "admin_rename_room": page_admin_rename_room,
    "admin_add_room": page_admin_add_room,
    "admin_remove_room": page_admin_remove_room,
})
