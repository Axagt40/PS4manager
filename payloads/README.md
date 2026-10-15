# payloads/ — what the manager sends to the PS4

Two different things share this folder, and neither is bundled third-party code:

* **`notify.elf`** — built here and shipped with the app: the "connected" notification, below.
* **an installer payload you drop in yourself** — `rpi.bin`, `rpi.elf`, `installer.bin` or
  `installer.elf`: what starts the console-side installer service on port 12800, further down.

## The "connected" notification

`notify.elf` is the payload the app pushes through GoldHEN's BinLoader after a successful
connect, so the console shows:

```
PS4 Manager connected
by Axagt40
```

## Why a payload at all?

A PS4 has no notification API you can reach over the network. A PC can talk to a GoldHEN console
in exactly three ways: **FTP** (files), **BinLoader** (which *executes* whatever you send it — a
payload) and whatever a homebrew app serves itself (e.g. Remote Package Installer on port 12800).
Only a payload can draw a notification, which is why every notification tool in the scene is one.
This is that payload, built from source in this repository — not somebody else's binary.

## What it does, and what it does not do

- Calls `sceKernelSendNotificationRequest` once with the text above, then returns.
- The request is the whole 3120-byte structure the kernel expects: the message starts at offset
  `0x2D` (45) — where the well-known "45 bytes, then your text" snippets put it — and the fields
  before it matter. Those snippets leave the prefix zero and the console then draws **nothing**;
  the toast only appears with `target_id = -1`, `use_icon_image_uri = 1` and an icon uri
  (`cxml://psnotification/tex_icon_system`). `main.c` sets all three, and a test in
  `../test_ps4_manager.py` fails if they ever get lost again.
- Writes one line to the console's log (`PS4 Manager: "connected" notification sent (rc=0)`), so
  "did the payload run, and what did the kernel say?" is answerable over GoldHEN's klog
  (port `3232`) instead of by guessing.
- It writes **nothing** to the console: no file, no setting, no plugin, no system change. It lives
  in the memory of the process GoldHEN's loader starts for it and goes away with it.
- No firmware address is hard-coded. The SDK's `libkernel` stub table resolves the kernel call on
  the console at runtime, so the same file works across GoldHEN firmware versions.
- The app sends it over its own BinLoader connection, so it never consumes the connection the
  manager keeps open (the *Payload ready* pill).
- Opt-in: the switch **Tell the PS4 when the manager connects** in Settings is **off by default**.
  Switch it on and the payload is sent once per connect; leave it off and nothing is sent at all.
- Reality check, measured on a GoldHEN build where this failed: a notification sent by a payload
  does not always render. There the loader reports the payload as *launched successfully* while the
  screen stays clean - this payload behaves exactly like the payload SDK's own reference sample on
  such a build, so it is the loader/shell, not the request. The app logs what happened instead of
  pretending, and GoldHEN's own "Payload received" line is what shows there.

## Rebuild it (with your own text)


```bash
sudo apt install clang-18 lld-18 llvm-18      # the SDK's wrappers need LLVM 18 or newer
wget https://github.com/ps4-payload-dev/sdk/releases/latest/download/ps4-payload-sdk.zip
unzip ps4-payload-sdk.zip -d ~/ps4-payload-sdk
cd payloads/notify && make                   # -> ../notify.elf
```

`make` uses `~/ps4-payload-sdk` unless you point `PS4_PAYLOAD_SDK` at the unzipped folder. Edit
`NOTIFY_TEXT` in `main.c` first if you want different words — the app sends whatever
`payloads/notify.elf` contains, so no text lives in `ps4_manager.py`.

The build is deliberately small: one kernel call, the request built as a constant in `.data` (so
nothing is copied at runtime), no C library, no unwind tables, everything stripped — about 50 KB,
most of which is the SDK's own firmware-independent resolver. `make` prints the exact size.

## Starting the console's installer service (port 12800)

The PKG manager can install a package on the console without touching the console's menus, but it
needs one small service on the console side listening on port 12800. Two things can provide it:

1. the **Remote Package Installer** homebrew app (`FLTZ00003`) — one-time install on the console,
   start it before a transfer; or
2. **an installer payload**, which is what the PC tools in the scene push through GoldHEN's
   BinLoader: the payload leaves the same API listening on 12800, and no console app is involved.

This app ships **no** third-party payload, so option 2 only works with a file you put here:
anything named `rpi.bin`, `rpi.elf`, `installer.bin` or `installer.elf` in this folder is used. The
manager then does the whole setup on its own — it finds 12800 dead, pushes that file through
BinLoader, waits for the service to answer (up to 20 s) and carries on with the install. Nothing is
sent when the folder holds no such file, and it is sent **once per session at most**: GoldHEN's
BinLoader takes a single connection.

Two honest caveats, both GoldHEN-side:

* BinLoader is experimental. On some builds it stops listening after one connection (toggle it on
the console and press **Check console** again), and on some builds a payload sent through it is
reported as *launched successfully* while it never actually starts — measured on exactly such a
build with the SDK's own reference payload as well as this project's. If the port stays dead after
the payload was sent, the log says so, and the homebrew app above is the route that always works.
* Which payload works is entirely the file's business: this project has not written or reviewed it.
Pick one whose licence you are happy with — e.g. the payload in
[marcussacana/DirectPackageInstaller](https://github.com/marcussacana/DirectPackageInstaller) is
GPL-3.0, which is why it is not part of this repository.

## Licence

`notify.elf` is built with the [ps4-payload-dev SDK](https://github.com/ps4-payload-dev/sdk) and is
free software under the **GNU GPL v3 or later** (the header in `main.c` says so). `main.c` here is
ours; the SDK remains a separate download and is not part of this repository. An installer payload
you drop into this folder keeps whatever licence it came with — it is not distributed here.
