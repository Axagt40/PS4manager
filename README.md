# PS4 Manager

A small desktop app for a **jailbroken PS4 over GoldHEN's FTP server**. Right now it manages
**plugins** (no FileZilla, no renaming `plugins.ini` to `.txt`, no manual editing) and browses the
console's **files**; it is built to grow further PS4 tools, so the name stays general.

```
python3 ps4_manager.py      # start the app
```

Everything the app writes (`ps4_manager_config.json`, `backups/`, `title_cache.json` and the
local `plugins.ini` copy) stays next to the script and is ignored by git; the tests live here
too. The `PS4-Manager.sh` shortcut in the parent folder is only a local launcher for the same
command and is not part of this repository.

It remembers your PS4's address, connects on startup and shows every plugin from
`/data/GoldHEN/plugins.ini` grouped into **Default (every game)** and one group per title id
(e.g. `CUSA12345 — Your Game`, names are read from your console). A click on the
**State** column enables/disables a plugin, and the change is written back to the console.

---

## Features

**Plugin tool**
- Every plugin listed with its real state (`● Enabled` / `○ Disabled`) and full path on the PS4.
- Click the *State* cell (or the name, or press `space`) to toggle → writes `=true` / `=false`.
- Entries that are missing from `/data/GoldHEN/plugins/` are flagged with `⚠` so you notice
  broken entries before GoldHEN starts complaining.
- **Add plugin** dialog with the two scope checkboxes: *Default — every game* or
  *Game specific — one title*. For a game you either pick from your installed games or type a
  new title id; the plugin file can be picked from the console's plugins folder, uploaded from
  your PC, or typed by hand. (Verified against a real console: the display name comes from each
  game's `param.sfo`.)
- Enable all / disable all, remove entries, right-click menu (enable, disable, remove, download
  from the console, copy path), `F5`/`Ctrl-R` to reload, `Ctrl-S` to save.
- Comments, blank lines, section order and any exotic lines in `plugins.ini` are preserved —
  a toggle changes only the value it has to change.

**Plugin files**
- Browse, upload, download, delete, create folders — all over FTP.

**Safe by default**
- Automatic local backup of `plugins.ini` before every upload (including a `-before` copy),
  browsable and restorable from the *Backups* dialog.
- Settings has *Save changes automatically*: toggles can go straight to the console, with a
  debounce so a flurry of clicks becomes one upload.

**Saved PS4 (auto-connect)**
- Settings has a **Saved PS4** card: fill in address/port/user/password once and press
  **Save settings** — they are written to `ps4_manager_config.json` and every later start connects
  on its own, with nothing to type.
- The card always states what the next start will do, e.g.
  *“✔ Next start: connects automatically to 192.168.1.50:2121 as anonymous.”* or a warning when
  *Connect automatically on startup* is switched off.
- Addresses you used before appear as one-click **Saved PS4s** buttons; **Forget saved PS4** deletes the
  stored address, password and history in one go.
- The details are kept even if you just type them and close the window, and the header **Connect**
  button saves whatever is in the form before dialling.
- The password (only needed for non-anonymous logins) is stored as plain text in the config file and
  the card says so.

**Comforts**
- Dark, minimal flat theme built with the standard library (no Qt, no pip).
- All network and file work runs on background threads — the window never freezes, transfers
  show progress in the status bar and every hiccup is logged (*Show log*). A dropped connection
  is re-established automatically and the command retried.
- *Find my PS4 on the network* scans your subnet for an open FTP port and fills the IP in.
- *Open local …* / *Save copy …* to edit a `plugins.ini` offline.

## Getting started

1. On the PS4: run GoldHEN and enable its **FTP server** (GoldHEN settings menu).
2. Run `python3 ps4_manager.py`.
3. In **Settings** type your PS4's IP (the address shown on the console), press
   **Find my PS4 on the network** if you don't know it, then **Save & connect**.
   Default port `2121`, user `anonymous`, empty password — that's what GoldHEN expects.
4. Next time the app starts it connects by itself — that is the *Saved PS4* card working. If it ever
   stops, check that **Connect automatically on startup** is still on there.

Typical paths (the defaults):

| What | Path on the PS4 |
| --- | --- |
| plugins.ini | `/data/GoldHEN/plugins.ini` |
| plugins | `/data/GoldHEN/plugins/` |

> GoldHEN reads `plugins.ini` when it starts, so restart GoldHEN (or at least the game) after
> changing something.

## Requirements

- Python 3.8 or newer with Tkinter. **There is nothing to `pip install`.**
- If your Python is missing Tkinter (some Linux distros ship it separately) the script offers to
  install it for you on first run (`apt`/`dnf`/`yum`/`zypper`/`pacman`/`apk`/`brew`, asking for
  your password via `sudo`), then restarts itself. Use `--no-install` to disable that.

## Command line

```
python3 ps4_manager.py [--host 192.168.1.50] [--port 2121] [--ini /data/GoldHEN/plugins.ini]
                       [--local plugins.ini] [--no-connect] [--no-install] [--version]
```

## Files it creates

| File | Purpose |
| --- | --- |
| `ps4_manager_config.json` | your saved FTP settings (delete it to start over) |
| `backups/plugins-<timestamp>.ini` | automatic backups of plugins.ini |
| `title_cache.json` | title id → game name cache |

They live next to the script (falling back to `~/.config/ps4_plugin_manager/` if that folder is
not writable).

## Tests

```bash
# offline: parsers, plugins.ini round-trips, SFO, config (no console needed)
python3 -m unittest test_ps4_manager.py

# against a real console (skipped unless PS4_HOST is set)
PS4_HOST=192.168.1.50 python3 -m unittest -v test_live_ps4.py
```

The live tests only touch a scratch folder (`/data/GoldHEN/_ps4manager_selftest`) that they
create and remove, and they assert that `plugins.ini` and the plugins folder are untouched.
`python3 ps4_manager.py --selftest` builds the whole UI once and exits — handy for a quick
sanity check after editing.

**The tests never show a window.** Both suites build the real Tk UI and then `withdraw()` it, so
running them does not pop anything up over whatever you are doing. (If you *do* want to watch the
UI being driven, use a throwaway virtual display, which needs no code change:
`xvfb-run -a python3 -m unittest test_live_ps4.py` - screenshots taken inside it land in `/tmp`.)

## Notes / troubleshooting

- *Connection refused / timed out* — the FTP server in GoldHEN must be enabled, the PS4 and PC
  must be on the same network, and the IP must be the one the console reports.
- *`=true` / `=false`* is understood by GoldHEN 2.3 and newer. Older builds just load every
  listed plugin; disabling then means removing the line.
- Console folders that contain images/`param.sfo` may not be readable over FTP (GoldHEN answers
  `550`); game names fall back to the title id and nothing else breaks.
- Big bogus sizes: GoldHEN's FTP reports nonsense sizes for a few very large files — cosmetic only.
