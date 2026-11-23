# -*- coding: utf-8 -*-
"""
TelChatter - image conversion: any uploaded raster image (PNG, BMP, GIF, JPEG, ...)
is decoded once at upload time into a small, self-contained binary "art blob" holding
full-resolution ASCII *and* PETSCII character/colour grids, stored in the database.

A static image becomes one frame. An animated GIF is resampled to at most
``gif_max_fps`` and stored as an exact cell-for-cell delta chain - the same idea used
for the author's Retro Recoil intro animation and its live network transport: the
first frame is stored whole, every later frame only stores the cells that actually
changed (a kind of motion compression - unchanging background costs nothing after
frame 1). A cell only counts as "changed" if its perceptual distance in the 16-colour
palette is at least ``motion_threshold`` away from the same cell last frame, which
trims flicker-sized noise out of noisy source GIFs without blurring real motion.

At *display* time (when a client's view reaches an image in the chat history), the
stored full-resolution grid is resampled with nearest-neighbour sampling to whatever
size actually fits that client's current terminal, so the same stored art looks right
on an 80x24 ANSI window or a fixed 40x25 PETSCII screen alike.

Format of the stored blob (all little-endian):
    MAGIC "TCI1"
    H cols, H rows, H nframes, H frame_ms
    per frame:
        1 byte  kind (1 = full, 0 = delta)
        full:   cols*rows bytes chars, cols*rows bytes colours
        delta:  4 byte count N, N x 2-byte cell index, N bytes chars, N bytes colours
"""
import io
import struct

import numpy as np

from tc_render import PAL_C64

MAGIC = b"TCI1"
RAMP = " .:-=+*#%@"
CHAR_ASPECT = 2.0  # a terminal cell is roughly twice as tall as it is wide

SUPPORTED_EXT_HINT = "PNG, BMP, GIF (incl. animated), JPEG, and most other common image formats"


class BadImage(Exception):
    pass


# ---------------------------------------------------------------- decode
def _open_image(raw_bytes):
    try:
        from PIL import Image
    except ImportError as e:
        raise BadImage("server is missing the Pillow library") from e
    try:
        im = Image.open(io.BytesIO(raw_bytes))
        im.load()
    except Exception as e:
        raise BadImage("invalid or unrecognised image file") from e
    return im


def probe_format(raw_bytes):
    """-> (format_name, is_animated) without fully decoding every frame, or raises BadImage."""
    im = _open_image(raw_bytes)
    fmt = (im.format or "?").upper()
    animated = bool(getattr(im, "is_animated", False))
    return fmt, animated


def _fit_dims(img_w, img_h, max_cols, max_rows, char_aspect=CHAR_ASPECT):
    box_w, box_h = max_cols, max_rows * char_aspect
    img_aspect = img_w / max(1, img_h)
    box_aspect = box_w / box_h
    if img_aspect > box_aspect:
        cols = max_cols
        rows = max(1, int(round(max_cols / img_aspect / char_aspect)))
    else:
        rows = max_rows
        cols = max(1, int(round(max_rows * char_aspect * img_aspect)))
    return max(1, min(cols, max_cols)), max(1, min(rows, max_rows))


def _quantize(rgb_rc3, alpha=None):
    """rgb (rows, cols, 3) uint8 -> (char_grid uint8, color_grid uint8) at the 16-colour palette.
    Transparent pixels (alpha below half) are rendered as a plain space (no colour)."""
    lum = (0.299 * rgb_rc3[..., 0].astype(np.float32) + 0.587 * rgb_rc3[..., 1].astype(np.float32)
           + 0.114 * rgb_rc3[..., 2].astype(np.float32))
    idx = np.clip((lum / 255.0 * (len(RAMP) - 1)).round().astype(np.int32), 0, len(RAMP) - 1)
    ramp_ord = np.array([ord(c) for c in RAMP], np.uint8)
    chars = ramp_ord[idx]
    d = ((rgb_rc3[..., None, :].astype(np.int32) - PAL_C64[None, None, :, :]) ** 2).sum(-1)
    colors = d.argmin(-1).astype(np.uint8)
    if alpha is not None:
        chars = np.where(alpha < 128, 32, chars)
    colors = np.where(chars == 32, 0, colors).astype(np.uint8)
    return chars.astype(np.uint8), colors


def _frame_grid(rgba, out_cols, out_rows):
    """One frame (h, w, 4) uint8 RGBA -> (out_rows, out_cols) char/colour arrays, letterboxed
    (resized to fit, centred) so the stored art is exactly out_cols x out_rows."""
    from PIL import Image
    h, w = rgba.shape[:2]
    fit_c, fit_r = _fit_dims(w, h, out_cols, out_rows)
    pil = Image.fromarray(rgba).resize((fit_c, fit_r), Image.LANCZOS)
    small = np.asarray(pil, dtype=np.uint8)
    chars = np.full((out_rows, out_cols), 32, np.uint8)
    colors = np.zeros((out_rows, out_cols), np.uint8)
    c0 = (out_cols - fit_c) // 2
    r0 = (out_rows - fit_r) // 2
    fc, fcol = _quantize(small[..., :3], small[..., 3] if small.shape[-1] == 4 else None)
    chars[r0:r0 + fit_r, c0:c0 + fit_c] = fc
    colors[r0:r0 + fit_r, c0:c0 + fit_c] = fcol
    return chars, colors


def _load_frames(im, max_frames=None):
    """-> (list of (h,w,4) uint8 RGBA frames, list of durations_ms)."""
    from PIL import ImageSequence
    frames, durations = [], []
    for i, frame in enumerate(ImageSequence.Iterator(im)):
        if max_frames and i >= max_frames:
            break
        rgba = frame.convert("RGBA")
        frames.append(np.asarray(rgba, dtype=np.uint8))
        durations.append(max(20, frame.info.get("duration", 100)))
    return frames, durations


def _palette_dist2(a, b):
    """Squared Euclidean distance between two palette indices' RGB, for motion thresholding."""
    pa, pb = PAL_C64[a], PAL_C64[b]
    d = pa.astype(np.int32) - pb.astype(np.int32)
    return int((d * d).sum())


_THRESH_CACHE = {}


def _changed_mask(chars, colors, prev_c, prev_col, threshold):
    """Cells that changed enough to resend, given a perceptual colour threshold (0 = any change)."""
    char_diff = chars != prev_c
    if threshold <= 0:
        color_diff = colors != prev_col
    else:
        key = threshold
        table = _THRESH_CACHE.get(key)
        if table is None:
            # precompute a 16x16 "close enough to skip" table once
            table = np.zeros((16, 16), bool)
            for i in range(16):
                for j in range(16):
                    table[i, j] = _palette_dist2(i, j) <= threshold * threshold * 900  # scaled, tuned by eye
            _THRESH_CACHE[key] = table
        close = table[colors, prev_col]
        color_diff = (colors != prev_col) & ~close
    return char_diff | color_diff


# ---------------------------------------------------------------- bin format
def _write_blob(cols, rows, frames_chars, frames_colors, frame_ms, threshold):
    out = bytearray()
    out += MAGIC
    out += struct.pack("<HHHH", cols, rows, len(frames_chars), min(frame_ms, 65535))
    prev_c = prev_col = None
    for chars, colors in zip(frames_chars, frames_colors):
        if prev_c is None:
            out += b"\x01"
            out += chars.tobytes()
            out += colors.tobytes()
        else:
            mask = _changed_mask(chars, colors, prev_c, prev_col, threshold)
            changed = np.nonzero(mask)
            idxs = (changed[0].astype(np.int64) * cols + changed[1]).astype(np.uint16)
            out += b"\x00"
            out += struct.pack("<I", len(idxs))
            out += idxs.tobytes()
            out += chars.reshape(-1)[idxs].tobytes()
            out += colors.reshape(-1)[idxs].tobytes()
        prev_c, prev_col = chars, colors
    return bytes(out)


def load_blob(data):
    """-> (cols, rows, frame_ms, [(chars, colors), ...]) or None if unreadable/corrupt."""
    if len(data) < 12 or data[:4] != MAGIC:
        return None
    cols, rows, nframes, frame_ms = struct.unpack_from("<HHHH", data, 4)
    n = cols * rows
    off = 12
    frames = []
    prev_c = prev_col = None
    try:
        for _ in range(nframes):
            kind = data[off]
            off += 1
            if kind == 1:
                chars = np.frombuffer(data, np.uint8, n, off).reshape(rows, cols).copy()
                off += n
                colors = np.frombuffer(data, np.uint8, n, off).reshape(rows, cols).copy()
                off += n
            else:
                (cnt,) = struct.unpack_from("<I", data, off)
                off += 4
                idxs = np.frombuffer(data, np.uint16, cnt, off)
                off += cnt * 2
                chs = np.frombuffer(data, np.uint8, cnt, off)
                off += cnt
                cls = np.frombuffer(data, np.uint8, cnt, off)
                off += cnt
                chars, colors = prev_c.copy(), prev_col.copy()
                fc, fk = chars.reshape(-1), colors.reshape(-1)
                fc[idxs] = chs
                fk[idxs] = cls
            frames.append((chars, colors))
            prev_c, prev_col = chars, colors
    except (ValueError, IndexError):
        return None
    return cols, rows, frame_ms, frames


# ---------------------------------------------------------------- public: convert an upload
def convert_upload(raw_bytes, max_cols, max_rows, gif_max_fps=10, motion_threshold=1):
    """Decode an uploaded image's raw bytes and build a stored art blob.

    -> dict(fmt, animated, cols, rows, frame_ms, data) or raises BadImage.
    Both the ASCII-ramp characters and a 16-colour (C64 palette) colour index per
    cell are produced once here; the same blob is reused for both ANSI and PETSCII
    display (PETSCII just maps the ramp characters through its own character set and
    the colour indices through its own colour codes at render time).
    """
    im = _open_image(raw_bytes)
    fmt = (im.format or "?").upper()
    animated = bool(getattr(im, "is_animated", False)) and fmt in ("GIF", "WEBP", "PNG", "APNG")
    w, h = im.size
    if w <= 0 or h <= 0:
        raise BadImage("image has no pixels")
    out_cols, out_rows = _fit_dims(w, h, max_cols, max_rows)

    if not animated:
        rgba = np.asarray(im.convert("RGBA"), dtype=np.uint8)
        chars, colors = _frame_grid(rgba, out_cols, out_rows)
        blob = _write_blob(out_cols, out_rows, [chars], [colors], 0, motion_threshold)
        return dict(fmt=fmt, animated=False, cols=out_cols, rows=out_rows, frame_ms=0, data=blob)

    try:
        raw_frames, durations_ms = _load_frames(im, max_frames=300)
    except BadImage:
        raise
    except Exception as e:                  # corrupt / truncated data is only noticed frame by frame
        raise BadImage("animated image is corrupt or incomplete") from e
    if not raw_frames:
        raise BadImage("animated image has no frames")
    # resample to at most gif_max_fps: pick the subset of source frames whose cumulative
    # original timing crosses each output tick, so fast GIFs are thinned and slow ones kept as-is
    target_dt = 1000.0 / max(1, gif_max_fps)
    picked = []
    t = 0.0
    next_tick = 0.0
    cum = 0.0
    for i, dur in enumerate(durations_ms):
        if cum >= next_tick:
            picked.append(i)
            next_tick += target_dt
        cum += dur
    if not picked:
        picked = [0]
    if picked[-1] != len(raw_frames) - 1:
        picked.append(len(raw_frames) - 1)

    fchars, fcolors = [], []
    for i in picked:
        c, k = _frame_grid(raw_frames[i], out_cols, out_rows)
        fchars.append(c)
        fcolors.append(k)
    frame_ms = int(round(target_dt))
    blob = _write_blob(out_cols, out_rows, fchars, fcolors, frame_ms, motion_threshold)
    return dict(fmt=fmt, animated=True, cols=out_cols, rows=out_rows, frame_ms=frame_ms, data=blob)


# ---------------------------------------------------------------- resample for display
_resample_cache = {}


def _resample_idx(base_n, out_n):
    key = (base_n, out_n)
    idx = _resample_cache.get(key)
    if idx is None:
        idx = np.minimum((np.arange(out_n) * base_n) // max(1, out_n), base_n - 1)
        _resample_cache[key] = idx
    return idx


def resample_frame(chars, colors, out_cols, out_rows):
    """Nearest-neighbour resample a stored (base_rows, base_cols) frame to out_cols x
    out_rows for the viewer's actual terminal / available chat-history width."""
    base_rows, base_cols = chars.shape
    if (base_cols, base_rows) == (out_cols, out_rows):
        return chars, colors
    ci = _resample_idx(base_cols, out_cols)
    ri = _resample_idx(base_rows, out_rows)
    return chars[ri][:, ci], colors[ri][:, ci]


# ---------------------------------------------------------------- upload transport helpers
def looks_truncated(raw):
    """True when `raw` begins like a PNG / GIF / JPEG / BMP but does not yet END like one.

    A slow or bursty client (SyncTERM pasting a file at modem speed, a Wi-Fi modem) can pause
    mid-file for longer than the quiet period. Calling that "finished" would hand half a file
    to the decoder and reject a good image, so a visibly unfinished known format earns a
    longer grace period. Formats we cannot judge return False."""
    n = len(raw)
    if n < 8:
        return False
    head = bytes(raw[:8])
    tail = bytes(raw[-16:])
    if head.startswith(b"\x89PNG"):
        return b"IEND" not in tail
    if head.startswith((b"GIF87a", b"GIF89a")):
        return not tail.rstrip(b"\x00").endswith(b"\x3b")
    if head.startswith(b"\xff\xd8"):
        return not tail.rstrip(b"\x00").endswith(b"\xff\xd9")
    if head.startswith(b"BM") and n >= 6:
        declared = int.from_bytes(bytes(raw[2:6]), "little")
        return 0 < declared > n
    return False


def _strip_telnet(raw):
    """Undo telnet framing: IAC IAC -> one 0xFF data byte; drop real IAC commands, option
    negotiations and subnegotiations. A bare IAC followed by a non-command byte is kept."""
    IAC, SB, SE = 255, 250, 240
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        b = raw[i]
        if b != IAC or i + 1 >= n:
            out.append(b)
            i += 1
            continue
        c = raw[i + 1]
        if c == IAC:
            out.append(IAC)
            i += 2
        elif c in (251, 252, 253, 254) and i + 2 < n:
            i += 3
        elif c == SB:
            j = raw.find(bytes([IAC, SE]), i + 2)
            if j < 0:
                out.append(b)
                i += 1
            else:
                i = j + 2
        elif 241 <= c <= 249:
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def upload_candidates(raw):
    """The ways the captured bytes may need to be read, most likely first.

    exactly as received (clients that paste a file as "fast ASCII input"), or telnet-unescaped
    (a standards-following binary client doubles every 0xFF) - whichever fits the data is
    tried first - then the same with NVT "CR NUL" collapsed to CR. Duplicates are skipped."""
    import re
    seen, out = set(), []
    base = bytes(raw)
    iac = _strip_telnet(base)
    runs = re.findall(rb"\xff+", base)
    doubled = bool(runs) and all(len(r) % 2 == 0 for r in runs)    # every FF came as an FF FF pair
    first, second = (iac, base) if doubled else (base, iac)
    for cand in (first, second, base.replace(b"\r\x00", b"\r"), iac.replace(b"\r\x00", b"\r")):
        if cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def convert_upload_any(raw_bytes, max_cols, max_rows, gif_max_fps=10, motion_threshold=1, attempts=None):
    """convert_upload() that retries the alternative readings from upload_candidates(), so the
    telnet layer's quirks never turn a good image into 'invalid file format'.

    If `attempts` is a list, every candidate's (length, error) is appended to it in the order
    tried, win or lose - this is what the raw-traffic log shows on a rejected upload, so a
    report of "invalid file format" carries exactly which readings of the bytes were tried
    and what each decoder said, instead of only the last one."""
    last = None
    for cand in upload_candidates(raw_bytes):
        try:
            result = convert_upload(cand, max_cols, max_rows, gif_max_fps, motion_threshold)
            if attempts is not None:
                attempts.append((len(cand), "ok"))
            return result
        except Exception as e:              # BadImage, or whatever else a decoder raises
            last = e
            if attempts is not None:
                attempts.append((len(cand), f"{type(e).__name__}: {e}"))
    raise BadImage("invalid or unrecognised image file") from last
