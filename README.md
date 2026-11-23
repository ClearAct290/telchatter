# TelChatter

**A telnet chat server with rooms, accounts and inline pictures - for modern ASCII/ANSI
terminals *and* real Commodore 64 hardware.**

![license: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)
![python: 3.10+](https://img.shields.io/badge/python-3.10%2B-green)

Connect with nothing but `telnet` (or a retro terminal such as SyncTERM, or a C64 with a
WiFi modem) and chat in themed rooms. Everyone can post text *and* images - including
animated GIFs - which the server converts to coloured ASCII (ANSI clients) or PETSCII
(C64) and resizes live to fit each viewer's screen.

TelChatter is a single Python program with two small dependencies. It follows the same
design as the author's other telnet projects (TelTube, Retro Recoil): the server does all
the rendering and streams **only the characters that changed**, so it stays light on slow
links and on the C64.

---

## Features

- **Auto-detects your client** - C64 PETSCII (40x25) or regular ASCII/ANSI (any size,
  resizes with your window). If it can't tell, it asks once.
- **Accounts** with salted password hashes (scrypt, PBKDF2 fallback) - no plain-text
  passwords on the host.
- **Preset logins** - if your telnet client passes a username and password up front it logs
  you in automatically, or creates the account for you if it doesn't exist yet.
- **Chat rooms** - 3 by default (`Room 1`..`Room 3`), renamable; add or remove rooms from
  the in-app Admin menu. Each user gets a bright, high-contrast name colour.
- **Scrollback** - scroll back 65 messages by default, configurable up to 750, with a
  scroll bar.
- **Home readme** - a host-editable page (plain `readme.txt`) with chat rules and TelChatter
  branding by default.
- **Status bar** - controls, UTC clock, users online and your time online. 30-minute idle
  timeout and 6-hour hard session limit (both configurable).
- **Images in chat** - PNG, BMP, GIF, JPEG and anything else [Pillow](https://pillow.readthedocs.io)
  can read, uploaded over **ZMODEM** (a real, checksummed file transfer - not pasted
  keystrokes). Converted once on upload to ASCII **and** PETSCII at up to 160x80 cells, then
  resized live to each viewer.
- **Animated GIFs** - resampled to at most 10 fps, then motion-compressed: the first frame is
  stored in full and later frames only store the cells that changed (above a threshold),
  so a small moving object on a still background is far smaller than raw frames (about 86% in a synthetic test with a moving dot).
- **Easy to host** - `pip install -r requirements.txt`, run one file, one INI config.

---

## Quick start

Requires **Python 3.10+** (developed on 3.12).

```sh
git clone <your-repo-url> TelChatter
cd TelChatter
pip install -r requirements.txt     # pillow + numpy

python3 tc_server.py                # or ./start.sh  /  start.bat on Windows
```

Then connect:

```sh
telnet localhost 2323
```

On first start TelChatter writes `tc_config.ini`, `readme.txt` and `telchatter.db` next to
the program. Open port **2323** (or whatever you set) on your firewall/router to host for
others.

> **Admin account:** only one account ever gets the *Admin* sidebar entry (edit the readme,
> add / rename / remove rooms) - the one named in `[security] admin_username`. Out of the box
> that is **`admin` / `admin1234`**. **Change `admin_password` in `tc_config.ini` before you
> put the server on the internet** (the server prints a warning on every start while the
> default is still set). Nobody can register the admin name, and being the first to sign up
> gives no special rights. See [Admin account](#admin-account).

Use another config file with `python3 tc_server.py -c /path/to/other.ini`.

---

## Connecting

| Client | Notes |
| --- | --- |
| Any `telnet` / PuTTY / SyncTERM / etc. | Window size is picked up automatically (telnet NAWS). Use an 8-bit-clean terminal; colour is 24-bit by default (`[display] color_mode` for 256/16-colour terminals). |
| Commodore 64 (WiFi modem, 1541 Ultimate, ...) | Detected from the terminal type, or choose **2 = C64 PETSCII** on the first menu. Uses the real C64 palette with reverse-video cells; the C64 link has its own, much smaller bandwidth budget. |

**How the terminal is detected.** A terminal type of `C64` / `PETSCII` / `CBM` selects PETSCII
at once. Anything else is *asked*: the server sends a cursor-position request (`ESC[6n`) and
only a real ANSI terminal answers it, so a window-size reply or an "ansi" terminal name is
not trusted by itself (WiFi modems and C64 telnet programs send those too, which used to put
raw `[38;2;...` escape codes on a C64 screen). If the terminal stays silent you get a one-screen
plain-text menu - `1 = Generic ASCII`, `2 = C64 PETSCII` - that every client can display.
If a C64's cursor-left / cursor-up key arrives while the session still thinks it is ANSI (before
login), the session switches itself to PETSCII.

### Logging in

Without presets you are asked to **Log In** or **Create Account**.

- **Create account**: username (3-16 letters, digits, `_`), password (6+ characters),
  confirm, *Create*. You then land on the login screen with the username already filled in
  and just press *Login*. If the username is taken the screen stays put with an empty
  username box.
- **Preset login**: clients that answer the telnet `NEW-ENVIRON` option with a `USER` and a
  `PASSWORD` (or `PASS`) variable are logged in automatically:
  - existing account + right password -> straight to the home screen
  - no such account -> created silently, then straight to the home screen
  - username taken but wrong password -> create-account screen with an empty username box

  Failed attempts count towards the same lock-out as the login screen. Passwords may
  contain spaces (usernames may not).

---

## Controls

| Key | Action |
| --- | --- |
| Arrow keys | Navigate the sidebar (Readme / rooms / Admin) and menus; in a room, UP / DOWN in the text box switch rooms (DOWN only while the box is empty) |
| `TAB` | Next field / button (text box -> SEND -> IMAGE) |
| `ENTER` | Select, send, confirm |
| `[` / `]` | Scroll history up / down - **only when you are not typing** (box empty, or focus on SEND / IMAGE). While typing they are ordinary characters |
| `Q` (or `q`) | Disconnect (asks first) - when not typing or uploading. In a room the text box has focus at first (so `q` is just a letter); `TAB` to SEND / IMAGE and `Q` quits |
| `SPACE` | Types a space in text boxes; presses the focused SEND / IMAGE button |
| `ESC` | Back / cancel |

While scrolling (not typing) the history area gets an outline instead of a filled highlight,
with a scroll bar on its right edge.

---

## Chat rooms

Pick a room in the sidebar. Messages show `[HH:MM] name: text` with the name in the user's
colour. The input row has three parts: the **text box**, **SEND** and **IMAGE**.

### Sending an image (ZMODEM)

Uploads use the **ZMODEM** file-transfer protocol - the same one SyncTERM, Qodem, mTelnet,
Tera Term, `lrzsz` and most BBS terminals speak. Unlike pasting a file as simulated
keystrokes, ZMODEM is a real binary transfer: every block is checksummed and a damaged one
is automatically re-sent, so an image either arrives exactly or the transfer says why it
didn't. There is **no input lock**: the protocol itself knows when the file is done, and
`ESC` cancels at any time before the transfer starts.

1. Move to **IMAGE** and press `ENTER`, then confirm *Ready to upload?*
2. TelChatter now waits and announces that it is ready to receive. Start a ZMODEM upload
   from your terminal:
   - **SyncTERM:** open its upload dialog (the *Upload File* menu entry / its upload hotkey),
     choose **ZModem**, pick the image. Some terminals open this dialog by themselves when
     they see TelChatter's invitation.
   - **other terminals:** their "Send file (ZMODEM)" command; with `sz` it is `sz picture.png`.
3. A byte counter shows progress while it arrives. When the transfer ends the file is
   converted to ASCII + PETSCII art.
4. The IMAGE button changes to **IMAGE OK** and the box says *Image uploaded*.
   - press **SEND** to post it (any text in the box is posted first), or
   - press **IMAGE** again (and confirm) to delete it.
5. A transfer that fails shows **Upload did not complete** with the reason; a file that
   transferred fine but is not a readable image shows an **Invalid file format** pop-up.
   Either way you are back in the room.

Only the first file of a batch is used; a file larger than `[images] max_upload_bytes` is
refused before any data is sent. The telnet connection should be in binary (8-bit) mode,
which TelChatter negotiates on connect - a client is free to send raw 8-bit data without
doubling `0xFF` once binary mode is on (doubling it is a non-binary-mode convention), and
TelChatter undoes the doubling for a client that does it anyway; either way the byte
arrives correctly, and ZMODEM's own per-block CRC catches anything that still goes wrong.

> **`[zmodem] escape_ctrl` stays `false`.** Turning it on asks the sender to escape every
> control byte (the ESCCTL flag). Real-world testing with SyncTERM showed it do the
> opposite of what you'd expect: SyncTERM treated the `CR` / `LF` / `XON` bytes that end
> every ZMODEM header as "unescaped control characters", dropped them, and rejected the
> header outright (`HEX header not terminated with LF` / `INVALID HEADER`). The classic
> header every real `rz` sends (no ESCCTL) is what every terminal expects.
>
> **Nothing is drawn on screen from the moment the invitation goes out until the transfer
> ends.** A terminal that recognises the invitation takes over the connection for its own
> upload dialog at that point, so a status screen sent partway through was read as garbage
> ZMODEM data and broke the header after it. The terminal shows its own transfer progress
> meanwhile; TelChatter redraws its own screen once the transfer is over.

Images are shown at most 22 character rows tall (fewer on short screens) so one picture
can't make the history slow to scroll.

---

## Configuration

`tc_config.ini` (written with defaults on first run; delete a line to get its default;
restart after editing).

| Section | Key | Default | Meaning |
| --- | --- | --- | --- |
| `[server]` | `host`, `port` | `0.0.0.0`, `2323` | listen address |
| | `name` | `TelChatter` | shown in the banner and default readme |
| | `max_clients`, `max_clients_per_ip` | `100`, `4` | connection limits |
| | `default_cols/rows` | `80`, `24` | size until the client reports its own |
| | `commodore_cols/rows` | `40`, `25` | PETSCII screen size |
| | `negotiate_wait` | `0.3` | seconds to wait for telnet option negotiation |
| `[session]` | `idle_timeout_minutes` | `30` | disconnect after this long without input |
| | `max_session_hours` | `6` | hard limit even with activity |
| `[display]` | `color_mode` | `truecolor` | `truecolor` \| `256` \| `16` (ANSI clients) |
| `[rooms]` | `count`, `names` | `3`, `Room 1, Room 2, Room 3` | **first-run seed only**; afterwards use the Admin menu |
| | `scrollback_default` | `65` | messages a client can scroll back through |
| | `scrollback_max` | `750` | upper limit for the above |
| `[images]` | `enabled` | `true` | turn image uploads off entirely |
| | `upload_timeout_seconds` | `120` | give up on a transfer that never starts or finishes |
| | `max_upload_bytes` | `8000000` | refuse larger files |
| | `max_art_cols/rows` | `160`, `80` | stored art resolution (cells) |
| | `gif_max_fps` | `10` | animated GIFs are resampled to at most this |
| | `motion_threshold` | `1` | min. change for a cell to be re-sent in GIF deltas (higher = smaller, rougher) |
| `[zmodem]` | `escape_ctrl` | `false` | ask the sender to escape every control byte (ESCCTL) - leave off, see below |
| | `rz_prefix` | `false` | also print `rz` + CR before the first ZRINIT, for a client that needs it |
| `[video]` | `rate_limit_bytes`, `commodore_rate_limit_bytes` | `400000`, `4500` | per-client bytes/second budgets |
| | `delta_gap_merge`, `full_refresh_seconds` | `3`, `20.0` | delta-encoder tuning |
| `[security]` | `admin_username`, `admin_password` | `admin`, `admin1234` | the one operator account - **change the password** |
| | `login_max_attempts`, `login_lockout_seconds` | `6`, `60` | brute-force lock-out |
| `[paths]` | `database`, `readme` | `telchatter.db`, `readme.txt` | relative to the config file |
| `[logging]` | `level`, `file` | `INFO`, *(console)* | |
| | `raw_enabled`, `raw_file` | `true`, `raw_data.log` | raw-traffic debug log (below) |
| | `raw_max_dump_bytes` | `256` | bytes shown per hex dump line before truncating the middle |

**Readme:** edit `readme.txt` while the server runs - it is re-read each time someone opens
the page. (The operator can also edit it from *Admin -> Edit README*.)

### Raw-traffic debug log

`[logging] raw_enabled` is **on by default**, writing to `raw_data.log` next to the config
file. It is plain text, one line per event, each stamped with the time and the client's IP:

- `recv` - every inbound byte chunk during normal telnet/keyboard traffic, hex + ASCII
- `ttype` - the terminal-type string the client reported
- `term` - which way auto-detection decided (`-> ansi` / `-> petscii`) and why (TTYPE seen,
  whether NAWS ever arrived, whether the ANSI cursor-position probe was answered)
- `zmodem-start` / `zmodem-recv` / `zmodem-stop` and `zmodem:` - an image upload as it
  happens: the bytes the terminal sent (hex + ASCII, long payloads show head and tail),
  and the protocol conversation in words (`rx ZFILE`, `tx ZRPOS`, `file complete`, any bad
  CRC and the re-request, an abort and its reason)
- `zmodem: upload-result` - `ok` with the decoded format/size, or the reason the transfer
  did not complete, or on an `invalid` image: the decoder's exact exception message and a
  hex dump of the start and end of the file it was given

This is the first place to look for a report like *"my upload fails"*: it shows exactly
what the terminal sent and what the server answered, instead of guessing blind. Turn it off (`raw_enabled = false`) on a busy
public server, since it is a plain-text log of every connection's traffic and will grow
continuously; it is not rotated automatically.

### Admin account

`[security] admin_username` / `admin_password` define the operator account. On every start the
server makes sure that account exists and that its password matches the config, so **to
change the admin password, edit `admin_password` and restart**. Only that account sees the
*Admin* sidebar entry and can use the operator menu: edit the README, rename / add / remove rooms.

- The admin name is reserved: nobody can sign up as it, in any capitalisation.
- Signing up first gives no special rights. A database from an older version, where the first
  account was the operator, has that flag removed on the next start.
- If an ordinary account already has the configured admin name, it is **not** promoted and its
  password is left alone; a warning is logged.
- Wrong-password attempts on the admin account count towards the brute-force lock-out.

---

## Running it for real

Telnet is **unencrypted** - passwords and chat travel in clear text, like any classic BBS.
Don't reuse a password you care about.

A minimal systemd unit:

```ini
[Unit]
Description=TelChatter
After=network.target

[Service]
WorkingDirectory=/opt/TelChatter
ExecStart=/usr/bin/python3 /opt/TelChatter/tc_server.py
Restart=on-failure
User=telchatter

[Install]
WantedBy=multi-user.target
```

Back up `telchatter.db` (SQLite: accounts, messages and the stored art).

---

## How it works

| File | Role |
| --- | --- |
| `tc_server.py` | entry point, shared state, accept loop, login throttling, admin account setup |
| `tc_session.py` | per-connection telnet I/O: negotiation (NAWS, TTYPE, NEW-ENVIRON), key parsing, ZMODEM byte feed, timeouts |
| `tc_screens.py` | every screen: terminal choice, login/create account, readme, rooms, upload flow, admin |
| `tc_render.py` | character grid + presenter; ANSI and PETSCII encoders that send only changed cells |
| `tc_zmodem.py` | ZMODEM receiver (CRC-16/32, hex + binary frames, ZDLE escapes, re-request of a bad block) - pure protocol, no I/O |
| `tc_images.py` | image -> ASCII/PETSCII art, GIF frame sampling, motion compression, live resampling |
| `tc_db.py` | SQLite: accounts (hashed), rooms, messages, image blobs |
| `tc_config.py` | INI loader with built-in defaults |

---

## Tests

```sh
python3 tests/run_tests.py
```

Starts a throwaway server and drives it with scripted telnet clients: account creation and
login, chat, ZMODEM image upload (PNG, JPEG, BMP, animated GIF; CRC-16 and CRC-32;
streaming, ZCRCQ and windowed senders; a damaged block that must be re-sent; a large image;
a terminal that aborts mid-transfer; attach / delete / post; invalid-file rejection), GIF playback,
PETSCII rendering, preset logins, bracket/scroll key handling. These are scripted clients - see
*Status* below.

## Status

Tested with the scripted clients above on Linux / Python 3.12. The ZMODEM receiver is
tested against an independently written ZMODEM sender, and its CRC and header framing
match known real `sz`/`rz` frames byte for byte. It has also been tried against real
SyncTERM, which turned up two real bugs now fixed and covered by tests: SyncTERM rejected
a ZRINIT sent with the ESCCTL flag (see the `escape_ctrl` note above), and a status-screen
update sent partway through a transfer corrupted the ZMODEM stream from SyncTERM's point
of view (now nothing is drawn from the invitation to the end of the transfer). C64
hardware itself is still untried. If an upload misbehaves, `raw_data.log` shows the whole
conversation (see *Raw-traffic debug log*).

---

## License

Copyright (C) 2026 ClearAct290-Reddit

TelChatter is free software: you can redistribute it and/or modify it under the terms of the
**GNU General Public License v3.0** (or, at your option, any later version). See
[`LICENSE`](LICENSE). It comes with ABSOLUTELY NO WARRANTY.
