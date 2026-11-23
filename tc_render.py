# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# TelChatter - Copyright (C) 2026 ClearAct290-Reddit
"""
TelChatter - terminal rendering.

* Grid        rows x cols of (char, fg, bg) where fg/bg are C64-palette indices 0-15
* Presenter   turns a Grid into bytes for an ANSI terminal or a PETSCII (Commodore)
              terminal, sending only the cells that actually changed since the last
              frame that was actually sent (exact compare - a cell is either identical
              to what the client already has, or it is resent in full; nothing fades
              or half-updates).  PETSCII deltas move with cursor codes; ANSI deltas
              move with cursor-position / cursor-forward escapes.

This module - the Grid/Presenter shape, the delta-only transport idea, and the
PETSCII/ANSI colour tables - is adapted from the author's own Retro Recoil project
(GPLv2+, itself adapted from TelTube, GPLv2+). See LICENSE / GITHUB_README.md.
"""
import time

import numpy as np

# ---------------------------------------------------------------- palette
(BLACK, WHITE, RED, CYAN, PURPLE, GREEN, BLUE, YELLOW, ORANGE, BROWN, LRED, DGREY, MGREY,
 LGREEN, LBLUE, LGREY) = range(16)

PAL_C64 = np.array([
    (0, 0, 0), (255, 255, 255), (136, 0, 0), (170, 255, 238), (204, 68, 204),
    (0, 204, 85), (0, 0, 170), (238, 238, 119), (221, 136, 85), (102, 68, 0),
    (255, 119, 119), (51, 51, 51), (119, 119, 119), (170, 255, 102), (0, 136, 255),
    (187, 187, 187)], np.int32)

# Bright, high-contrast colours used to assign each chatter a distinct name colour.
# WHITE/BLACK/DGREY/MGREY are left out (too low-contrast or reserved for UI chrome).
NAME_COLORS = [RED, CYAN, GREEN, YELLOW, ORANGE, LRED, LGREEN, LBLUE, LGREY, PURPLE, BLUE, BROWN]

PETSCII_COLOR = [144, 5, 28, 159, 156, 30, 31, 158, 129, 149, 150, 151, 152, 153, 154, 155]
PET_CLEAR, PET_HOME = 0x93, 0x13
PET_REV_ON, PET_REV_OFF = 0x12, 0x92
PET_DOWN, PET_UP, PET_RIGHT, PET_LEFT = 0x11, 0x91, 0x1D, 0x9D
PET_DEL = 0x14

# ASCII chars that are a different glyph (or missing) in unshifted PETSCII
_PET_SUBST = {ord("\\"): ord("/"), ord("^"): ord("@"), ord("_"): ord("-"), ord("|"): ord(":"),
              ord("{"): ord("("), ord("}"): ord(")"), ord("~"): ord("-"), ord("`"): ord("'"),
              ord('"'): ord("'")}


def _pet_char(o):
    if o == 0:
        return 32
    if 97 <= o <= 122:
        o -= 32
    o = _PET_SUBST.get(o, o)
    return o if 32 <= o <= 93 else 63


PET_LUT = np.array([_pet_char(o) for o in range(256)], np.uint8)


def to_petscii_bytes(s):
    return bytes(PET_LUT[ord(c) if ord(c) < 256 else 63] for c in s)


_XTERM16 = np.array([(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205), (0, 205, 205),
                     (229, 229, 229), (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0), (92, 92, 255),
                     (255, 0, 255), (0, 255, 255), (255, 255, 255)], np.int32)


def _nearest(rgb, table):
    return int(((table - np.array(rgb)) ** 2).sum(1).argmin())


def _cube256(rgb):
    r, g, b = rgb
    lv = [0, 95, 135, 175, 215, 255]
    ci = [min(range(6), key=lambda i: abs(lv[i] - c)) for c in (r, g, b)]
    cube = 16 + 36 * ci[0] + 6 * ci[1] + ci[2]
    cube_rgb = np.array([lv[i] for i in ci])
    gray_i = min(23, max(0, int(round((sum(rgb) / 3 - 8) / 10))))
    gv = 8 + 10 * gray_i
    if ((np.array(rgb) - gv) ** 2).sum() < ((np.array(rgb) - cube_rgb) ** 2).sum():
        return 232 + gray_i
    return cube


def build_sgr(mode):
    """(fg_strings, bg_strings) - SGR parameter text for the 16 C64 colours."""
    fg, bg = [], []
    for i in range(16):
        r, g, b = [int(v) for v in PAL_C64[i]]
        if mode == "truecolor":
            fg.append(f"38;2;{r};{g};{b}")
            bg.append(f"48;2;{r};{g};{b}")
        elif mode == "256":
            n = _cube256((r, g, b))
            fg.append(f"38;5;{n}")
            bg.append(f"48;5;{n}")
        else:
            n = _nearest((r, g, b), _XTERM16)
            fg.append(str(30 + n if n < 8 else 90 + n - 8))
            bg.append(str(40 + n if n < 8 else 100 + n - 8))
    return fg, bg


# ---------------------------------------------------------------- grid
class Grid:
    def __init__(self, rows, cols, fg=WHITE, bg=BLACK):
        self.rows, self.cols = rows, cols
        self.ch = np.full((rows, cols), 32, np.int16)
        self.fg = np.full((rows, cols), fg, np.int16)
        self.bg = np.full((rows, cols), bg, np.int16)

    def put(self, r, c, ch, fg=None, bg=None):
        if 0 <= r < self.rows and 0 <= c < self.cols:
            self.ch[r, c] = ord(ch) if isinstance(ch, str) else ch
            if fg is not None:
                self.fg[r, c] = fg
            if bg is not None:
                self.bg[r, c] = bg

    def text(self, r, c, s, fg=None, bg=None):
        if not 0 <= r < self.rows:
            return
        for i, ch in enumerate(s):
            x = c + i
            if 0 <= x < self.cols:
                self.ch[r, x] = ord(ch)
                if fg is not None:
                    self.fg[r, x] = fg
                if bg is not None:
                    self.bg[r, x] = bg

    def center(self, r, s, fg=None, bg=None, c0=0, width=None):
        w = self.cols - c0 if width is None else width
        self.text(r, c0 + max(0, (w - len(s)) // 2), s, fg, bg)

    def fill(self, r, c, h, w, bg=None, fg=None, ch=32):
        r0, c0 = max(r, 0), max(c, 0)
        r1, c1 = min(r + h, self.rows), min(c + w, self.cols)
        if r1 <= r0 or c1 <= c0:
            return
        self.ch[r0:r1, c0:c1] = ch
        if bg is not None:
            self.bg[r0:r1, c0:c1] = bg
        if fg is not None:
            self.fg[r0:r1, c0:c1] = fg

    def box(self, r, c, h, w, fg, bg=BLACK):
        self.fill(r, c, h, w, bg=bg, fg=fg)
        for x in range(c + 1, c + w - 1):
            self.put(r, x, "-", fg, bg)
            self.put(r + h - 1, x, "-", fg, bg)
        for y in range(r + 1, r + h - 1):
            self.put(y, c, ":", fg, bg)
            self.put(y, c + w - 1, ":", fg, bg)
        for (y, x) in ((r, c), (r, c + w - 1), (r + h - 1, c), (r + h - 1, c + w - 1)):
            self.put(y, x, "+", fg, bg)

    def vline(self, r, c, h, ch, fg, bg=BLACK):
        for y in range(r, r + h):
            self.put(y, c, ch, fg, bg)

    def blit(self, r0, c0, sub_ch, sub_fg, sub_bg):
        """Paste numpy sub-arrays (rows, cols) of char/fg/bg at (r0, c0), clipped to the grid."""
        h, w = sub_ch.shape
        rr0, cc0 = max(r0, 0), max(c0, 0)
        rr1, cc1 = min(r0 + h, self.rows), min(c0 + w, self.cols)
        if rr1 <= rr0 or cc1 <= cc0:
            return
        sr0, sc0 = rr0 - r0, cc0 - c0
        sr1, sc1 = sr0 + (rr1 - rr0), sc0 + (cc1 - cc0)
        self.ch[rr0:rr1, cc0:cc1] = sub_ch[sr0:sr1, sc0:sc1]
        self.fg[rr0:rr1, cc0:cc1] = sub_fg[sr0:sr1, sc0:sc1]
        self.bg[rr0:rr1, cc0:cc1] = sub_bg[sr0:sr1, sc0:sc1]

    def copy(self):
        g = Grid.__new__(Grid)
        g.rows, g.cols = self.rows, self.cols
        g.ch, g.fg, g.bg = self.ch.copy(), self.fg.copy(), self.bg.copy()
        return g


# ---------------------------------------------------------------- presenter
class Presenter:
    """Encodes Grids into terminal bytes with exact delta compression.

    The previous grid is only replaced when a frame is actually *sent*, so a frame that
    is dropped (client too slow / bandwidth budget) is simply folded into the next delta.
    """

    def __init__(self, petscii, color_mode="truecolor", rate=400000, gap=3, full_refresh=20.0, wrap=True):
        self.petscii = petscii
        self.mode = color_mode
        self.rate = float(rate)
        self.gap = gap
        self.full_refresh = full_refresh
        self.wrap = wrap
        self.cap = max(2000.0, self.rate * 0.5)
        self.budget = self.cap
        self.t_budget = time.monotonic()
        self.reset()
        self.set_color_mode(color_mode)

    def set_color_mode(self, mode):
        self.mode = mode
        self.fg_sgr, self.bg_sgr = build_sgr(mode)
        self._sgr = {}
        if self.petscii:
            self._move_cost, self._char_cost, self._color_cost = 2.0, 1.0, 1.0
        else:
            self._move_cost = 5.0
            self._char_cost = 1.0
            self._color_cost = float(len(self._both(WHITE, BLACK)))
        self.reset()

    def reset(self):
        self.prev = None
        self.last_full = 0.0
        self.p_rev = False
        self.p_color = None
        self.a_cur = None
        self._pend_cur = None

    def _refill(self):
        now = time.monotonic()
        self.budget = min(self.cap, self.budget + (now - self.t_budget) * self.rate)
        self.t_budget = now

    def ready(self, backlog=0):
        self._refill()
        return self.budget > self.rate * 0.02 and backlog < max(4000, self.rate * 0.5)

    def _limit_mask(self, mask, fg, bg, ch_changed, byte_budget):
        """Pick which changed cells get sent this frame under a byte budget, by packing
        whole contiguous runs (same runs the encoder would emit) greedily by score-per-byte,
        so a plain-coloured area is sent solid instead of a scattered half-update."""
        rows, cols = mask.shape
        move_c, char_c, color_c = self._move_cost, self._char_cost, self._color_cost
        runs = []
        total_cost = 0.0
        for r in np.nonzero(mask.any(axis=1))[0].tolist():
            idxs = np.nonzero(mask[r])[0]
            if idxs.size == 0:
                continue
            gaps = np.diff(idxs) > self.gap + 1
            splits = np.nonzero(gaps)[0] + 1
            row_chg = ch_changed[r]
            pair_id = fg[r].astype(np.int32) * 16 + bg[r].astype(np.int32)
            for grp in np.split(idxs, splits):
                c0, c1 = int(grp[0]), int(grp[-1]) + 1
                length = c1 - c0
                switches = int(np.count_nonzero(np.diff(pair_id[c0:c1]))) if length > 1 else 0
                cost = move_c + length * char_c + switches * color_c
                score = float(row_chg[c0:c1].sum()) * 2.0 + length
                total_cost += cost
                runs.append((score / cost, cost, r, c0, c1))
        if total_cost <= byte_budget:
            return mask
        runs.sort(key=lambda t: -t[0])
        out = np.zeros_like(mask)
        spent = 0.0
        for _density, cost, r, c0, c1 in runs:
            if spent + cost > byte_budget:
                continue
            out[r, c0:c1] = True
            spent += cost
        return out

    def _both(self, f, b):
        k = f * 16 + b
        s = self._sgr.get(k)
        if s is None:
            s = self._sgr[k] = f"\x1b[{self.fg_sgr[f]};{self.bg_sgr[b]}m"
        return s

    # -- public
    def encode(self, g, force=False):
        """Return bytes to send (b'' if nothing changed) or None to skip this frame."""
        self._refill()
        now = time.monotonic()
        prev = self.prev
        full = (prev is None or prev[0].shape != g.ch.shape or force == "full"
                or now - self.last_full > self.full_refresh)
        byte_budget = None
        if not full and not force:
            if self.budget < self.rate * 0.02:
                return None
            byte_budget = self.budget * 0.9
        if self.petscii:
            data, state, cells = self._pet(g, prev, full, byte_budget)
        else:
            data, state, cells = self._ansi(g, prev, full, byte_budget)
        if data is None:
            self.prev = state
            return b""
        n = len(data)
        if full and not force and n > self.budget and self.budget < self.cap * 0.9:
            return None
        self.budget -= n
        self.prev = state
        if full:
            self.last_full = now
        return data

    # -- ANSI
    def _ansi(self, g, prev, full, byte_budget=None):
        state = (g.ch, g.fg, g.bg)
        rows, cols = g.ch.shape
        pre = ""
        if full:
            mask = np.ones((rows, cols), bool)
            pre = "\x1b[0m\x1b[?7l\x1b[2J\x1b[H"
            cur = None
        else:
            mask = (g.ch != prev[0]) | (g.bg != prev[2]) | ((g.fg != prev[1]) & (g.ch != 32))
            cur = self.a_cur
        mask[rows - 1, cols - 1] = False
        if not full and not mask.any():
            return None, state, 0
        if byte_budget is not None and not full:
            sel = self._limit_mask(mask, g.fg, g.bg, g.ch != prev[0], byte_budget)
            if sel is not mask:
                mask = sel
                state = (np.where(mask, g.ch, prev[0]), np.where(mask, g.fg, prev[1]), np.where(mask, g.bg, prev[2]))
        out = [pre]
        gap = self.gap
        fgs_s, bgs_s = self.fg_sgr, self.bg_sgr
        both = self._both
        for r in np.nonzero(mask.any(axis=1))[0].tolist():
            idxs = np.nonzero(mask[r])[0].tolist()
            chs, fgs, bgs = g.ch[r].tolist(), g.fg[r].tolist(), g.bg[r].tolist()
            n = len(idxs)
            i = 0
            x = -1
            while i < n:
                s0 = idxs[i]
                if x < 0:
                    out.append(f"\x1b[{r + 1};{s0 + 1}H")
                elif s0 > x:
                    d = s0 - x
                    out.append("\x1b[C" if d == 1 else f"\x1b[{d}C")
                x = s0
                while True:
                    c, f, b = chs[x], fgs[x], bgs[x]
                    if c == 32 and cur is not None:
                        f = cur[0]
                    if cur is None:
                        out.append(both(f, b))
                    elif f != cur[0] or b != cur[1]:
                        if f == cur[0]:
                            out.append(f"\x1b[{bgs_s[b]}m")
                        elif b == cur[1]:
                            out.append(f"\x1b[{fgs_s[f]}m")
                        else:
                            out.append(both(f, b))
                    cur = (f, b)
                    out.append(chr(c))
                    x += 1
                    while i < n and idxs[i] < x:
                        i += 1
                    if i >= n:
                        break
                    nxt = idxs[i]
                    k = nxt - x
                    if k == 0:
                        continue
                    if k <= gap:
                        ok = True
                        for y in range(x, nxt):
                            if bgs[y] != cur[1] or (chs[y] != 32 and fgs[y] != cur[0]):
                                ok = False
                                break
                        if ok:
                            while x < nxt:
                                out.append(chr(chs[x]))
                                x += 1
                            continue
                    break
        self._pend_cur = cur
        return "".join(out).encode("ascii", "replace"), state, int(mask.sum())

    # -- PETSCII
    def _pet(self, g, prev, full, byte_budget=None):
        rows, cols = g.ch.shape
        rev = g.bg != BLACK
        color = np.where(rev, g.bg, g.fg).astype(np.int16)
        ch = g.ch
        color = np.where((ch == 32) & ~rev, 0, color)
        state = (ch.copy(), color, rev)
        if full:
            mask = np.ones((rows, cols), bool)
        else:
            mask = (ch != prev[0]) | (color != prev[1]) | (rev != prev[2])
        mask[rows - 1, cols - 1] = False
        if not mask.any():
            return None, state, 0
        if byte_budget is not None and not full:
            sel = self._limit_mask(mask, color, color, ch != prev[0], byte_budget)
            if sel is not mask:
                mask = sel
                state = (np.where(mask, ch, prev[0]), np.where(mask, color, prev[1]), np.where(mask, rev, prev[2]))
        out = bytearray([PET_HOME])
        pos = (0, 0)
        p_rev, p_color = self.p_rev, self.p_color
        for r in np.nonzero(mask.any(axis=1))[0].tolist():
            idxs = np.nonzero(mask[r])[0].tolist()
            chs = PET_LUT[np.clip(ch[r], 0, 255)].tolist()
            cols_l, revs = color[r].tolist(), rev[r].tolist()
            for x in idxs:
                if pos is None:
                    out.append(PET_HOME)
                    pos = (0, 0)
                dr, dc = r - pos[0], x - pos[1]
                if abs(dr) + abs(dc) > 1 + r + x:
                    out.append(PET_HOME)
                    dr, dc = r, x
                if dr:
                    out.extend(bytes([PET_DOWN if dr > 0 else PET_UP]) * abs(dr))
                if dc:
                    out.extend(bytes([PET_RIGHT if dc > 0 else PET_LEFT]) * abs(dc))
                rv = revs[x]
                if rv != p_rev:
                    out.append(PET_REV_ON if rv else PET_REV_OFF)
                    p_rev = rv
                cv = cols_l[x]
                if (rv or chs[x] != 32) and cv != p_color:
                    out.append(PETSCII_COLOR[cv])
                    p_color = cv
                out.append(chs[x])
                if x == cols - 1:
                    pos = None
                else:
                    pos = (r, x + 1)
        self.p_rev, self.p_color = p_rev, p_color
        return bytes(out), state, int(mask.sum())


def shorten(name, n):
    """Cut the middle out of a long name and put a '.' there so it fits n chars."""
    if n <= 0:
        return ""
    if len(name) <= n:
        return name
    if n == 1:
        return name[0]
    left = (n - 1) // 2
    right = n - 1 - left
    return name[:left] + "." + name[-right:] if right else name[:left] + "."
