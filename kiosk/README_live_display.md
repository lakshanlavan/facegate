# PyQt6 Live-Display Wrapper

A PyQt6 / PyQt6-WebEngine front-end that opens the **live attendance page**
full-screen on the connected door display — preferring a small **7-inch HDMI
panel**. This is **the only production live view**. It does **not** use Google
Chrome / Chromium in any way.

By default it talks **directly to the backend on `http://127.0.0.1:5000/`** (the
port uvicorn binds), **not** `https://localhost/`. The direct port needs no
nginx and no self-signed TLS certificate, so the display comes up even when
nginx is absent/misconfigured — the most common reason the wrapper used to hang
on *"Waiting for backend…"*. To route through nginx instead (optional), set
`LIVE_DISPLAY_URL=https://localhost/` and
`LIVE_DISPLAY_HEALTH_URL=https://localhost/api/health` (the localhost
self-signed cert is still accepted).

> The PyQt live display is **the single production front-end** for the outside
> door display. There is **no Chrome/Chromium kiosk**, **no Chrome fallback**,
> and **no Chrome autostart**. In production it is auto-started on graphical
> login by the systemd **--user** service `codeglofix-live-display.service`
> (`Restart=always`, `After`/`WantedBy=graphical-session.target`). No backend,
> face-recognition, database, admin, nginx, or other application files are
> modified by this wrapper.

## Files

| File | Purpose |
|------|---------|
| `kiosk/live_display.py` | The PyQt6/WebEngine full-screen wrapper (screen selection, health-wait, cert-accept, watchdog). |
| `kiosk/run_live_display.sh` | Launcher: resolves venv, sets Wayland platform for this process, waits for backend health, runs the wrapper once. |
| `kiosk/codeglofix-live-display.service` | The systemd **--user** service template (`__PROJECT_DIR__` placeholder) that auto-starts the PyQt live display on graphical login. |
| `kiosk/CodeGloFix-LiveView.desktop` | Desktop icon **"CodeGloFix Live View"** — a **manual** launcher for `run_live_display.sh` (same PyQt display the service runs). |
| `kiosk/CodeGloFix-Admin.desktop` | Desktop icon **"CodeGloFix Admin"** — a **manual** launcher for `kiosk/open_admin.sh`, which opens `/admin` (login-gated). Never autostarted. |
| `kiosk/open_admin.sh` | Opens the admin page (`/admin`) in a browser. Manual only; login/security unchanged. |
| `requirements-display.txt` | Display deps (`PyQt6`, `PyQt6-WebEngine`) — installed into the app's existing venv. |

## Desktop icons

Two desktop icons are installed; both are **manual launchers** (neither is
autostart):

- **CodeGloFix Live View** (`kiosk/CodeGloFix-LiveView.desktop` →
  `run_live_display.sh`) — opens the same PyQt live attendance display that the
  `--user` service runs in production. Use it to bring the live view up by hand
  (e.g. after a manual exit).
- **CodeGloFix Admin** (`kiosk/CodeGloFix-Admin.desktop` → `kiosk/open_admin.sh`)
  — opens the **`/admin`** page only. It is login-gated and its security is
  **unchanged**. It is **never** autostarted.

## Purpose

- Render the existing live page full-screen on the 7-inch display.
- Wait for `http://127.0.0.1:5000/api/health` before loading the UI.
- Accept the **self-signed localhost certificate** (localhost hosts only) when
  overridden to an `https://localhost/` URL.
- Pick the target screen by priority:
  1. CLI screen index argument, 2. exact **800×480**, 3. exact **1024×600**,
  4. smallest connected display by area, 5. primary (fallback).
- Provide a watchdog that reloads on repeated health failures and exits
  non-zero after repeated reload failures, so the `--user` service
  (`Restart=always`) restarts it automatically.

## Install display dependencies (into the app's existing venv)

Install into the **same venv the backend already uses** — do **not** install
globally. On this machine the running app uses `/opt/digitweb-attendance/venv`:

```bash
/opt/digitweb-attendance/venv/bin/pip install -r \
    /home/auto/Desktop/display/gym_face_access/requirements-display.txt
```

If your deployment uses a different venv (e.g. `/opt/codeglofix/venv`), point
pip at that one instead. `run_live_display.sh` auto-detects the venv in this
order: `$LIVE_DISPLAY_VENV`, `$CODEGLOFIX_VENV`, `$DIGITGYM_VENV`,
`/opt/codeglofix/venv`, `/opt/digitweb-attendance/venv`, `../venv`, `./venv`.

## How to run (manually)

In production the `--user` service starts the display automatically on login.
To launch it by hand (or use the **CodeGloFix Live View** desktop icon):

```bash
cd /home/auto/Desktop/display/gym_face_access
./kiosk/run_live_display.sh          # auto-select target screen
```

Or run the Python directly with a specific venv:

```bash
/opt/digitweb-attendance/venv/bin/python kiosk/live_display.py
```

### Force a specific screen index

`live_display.py` logs the detected screen list with indexes (`[0]`, `[1]`, …).
Pass the index as the first argument:

```bash
./kiosk/run_live_display.sh 1
# or
/opt/digitweb-attendance/venv/bin/python kiosk/live_display.py 1
```

### Exit (door-kiosk policy)

By default this is a **locked kiosk**: a plain **ESC is ignored** so a stray key
press cannot take the live display down. Use the always-available emergency exit
**Ctrl+Alt+Q** on the display's keyboard. To allow plain ESC (bench/testing
only), launch with `LIVE_DISPLAY_ALLOW_ESC=1`. The mouse cursor is never
permanently hidden.

## Logs

`run_live_display.sh` writes to the first of these that is writable:

1. `/var/log/codeglofix/live_display.log`
2. `<project>/kiosk/live_display.log`

Override with `LIVE_DISPLAY_LOG=/path/to/file`. `live_display.py` also logs to
stderr (screen list, selected screen, health status, page-load status, reloads,
errors), which the launcher tees into the same log file.

```bash
tail -f /var/log/codeglofix/live_display.log 2>/dev/null || \
    tail -f kiosk/live_display.log
```

## What success looks like

- The log shows the detected screens, the selected screen + reason,
  `backend healthy — loading http://127.0.0.1:5000/`, then `page revealed`.
  (The live page holds an MJPEG camera stream open, so the browser `load`
  event never "finishes" — the wrapper reveals the page on load-progress
  instead. This is expected and logged.)
- A full-screen window shows the live attendance page on the 7-inch display
  (or the selected screen).
- Ctrl+Alt+Q closes it cleanly (exit code 0); plain ESC is ignored unless
  `LIVE_DISPLAY_ALLOW_ESC=1`.

## What failure means

- `PyQt6 / PyQt6-WebEngine not importable` → deps not installed in that venv;
  run the install command above (never globally).
- `no screens detected by Qt` (exit 3) → Qt cannot reach the display server;
  on Wayland ensure the launcher selected `QT_QPA_PLATFORM=wayland` (it does so
  automatically). Force manually for one run with `QT_QPA_PLATFORM=wayland`.
- Stuck on `Waiting for backend…` → `http://127.0.0.1:5000/api/health` is not
  returning 200. Check the backend service:
  `systemctl status codeglofix-access@$USER --no-pager` and
  `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/api/health`.
  (If you overrode the URL to nginx, check nginx is serving 443 instead.)
- Repeated reload failures → the wrapper exits with code **2** on purpose, so
  the `--user` service restarts it.

## Environment overrides

| Variable | Default | Meaning |
|----------|---------|---------|
| `LIVE_DISPLAY_URL` | `http://127.0.0.1:5000/` | page to load (set to `https://localhost/` to use nginx — optional) |
| `LIVE_DISPLAY_HEALTH_URL` | `http://127.0.0.1:5000/api/health` | health endpoint (set to `https://localhost/api/health` for nginx — optional) |
| `LIVE_DISPLAY_VENV` | (auto) | venv to use |
| `LIVE_DISPLAY_LOG` | (auto) | log file path |
| `HEALTH_WAIT_SECONDS` | `180` | launcher's max health wait |
| `LIVE_DISPLAY_HEALTH_EVERY` | `15` | watchdog period (seconds) |
| `LIVE_DISPLAY_HEALTH_FAILS` | `3` | consecutive fails before reload |
| `LIVE_DISPLAY_MAX_RELOADS` | `5` | reload attempts before exit(2) |
| `LIVE_DISPLAY_ALLOW_ESC` | `0` | `1` lets plain ESC close the window — **testing only** (Ctrl+Alt+Q always exits) |

The defaults point **directly at the backend uvicorn binds**
(`http://127.0.0.1:5000/` and `.../api/health`); nginx/https is optional. The
launcher performs a **health-wait before showing the page**, and the wrapper's
**watchdog reloads on health failure**.

## Production autostart (systemd --user service)

In production the live display is started automatically by the systemd
**--user** service `codeglofix-live-display.service`:

- `Restart=always` — the door display self-heals after a backend or camera
  hiccup, or if the wrapper exits non-zero.
- `After=graphical-session.target` / `WantedBy=graphical-session.target` — it
  starts once the user's graphical session exists and stops with it.

Install renders `__PROJECT_DIR__` (+ venv) into
`~/.config/systemd/user/codeglofix-live-display.service`, runs
`systemctl --user daemon-reload` + `enable`, and starts it when a graphical
session is available.

### Power-cut / boot recovery flow

With **GNOME auto-login** enabled:

1. Power returns → PC boots → auto-login starts the graphical session.
2. `graphical-session.target` is reached → systemd `--user` starts
   `codeglofix-live-display.service`.
3. `run_live_display.sh` resolves the venv, forces the Wayland Qt platform (for
   its own process only), and **waits for `…/api/health`** before loading.
4. `live_display.py` opens the live page full-screen on the 7-inch display.
5. If the backend is unhealthy repeatedly, the wrapper exits non-zero and
   systemd (`Restart=always`, `RestartSec=5`) restarts it.

> Auto-login is required for a fully unattended power-cut restart, because a
> `--user` service only starts once the user's graphical session exists.

## Verify the install

Confirm everything the door display needs with these read-only commands (deps,
venv imports, backend health, the `--user` service, and the detected screens):

```bash
./deploy/verify_pyqt_display.sh
./deploy/verify_pyqt_display_install.sh
systemctl --user status codeglofix-live-display.service --no-pager
```

Check the service logs:

```bash
journalctl --user -u codeglofix-live-display.service -e
tail -f /var/log/codeglofix/live_display.log
```

> **A physical 7-inch panel test is still required** for final hardware proof —
> the verify scripts confirm software wiring, but only real hardware confirms
> the panel is auto-selected and full-screened correctly.

## Removing a previous install

If you need to remove a prior installation, use the non-destructive
`deploy/remove_previous_install.sh`. It **preserves the database and
enrolments** and does not touch application data:

```bash
./deploy/remove_previous_install.sh
```

## Do I still need a primary/secondary monitor setup?

**No.** You do **not** need to configure which monitor is primary/secondary,
mirror/extend layouts, or set positions in GNOME Display settings. With a
single connected display the PyQt live view simply opens full-screen there.
When a 7-inch HDMI panel is attached, `live_display.py` picks and full-screens
it itself, in this order:

1. **CLI screen index** (`run_live_display.sh <index>` / `live_display.py <index>`)
2. exact **800×480** display (typical 7-inch panel)
3. exact **1024×600** display (other small HDMI panels)
4. **smallest** connected display by area
5. **primary** display (fallback; also the single-display case)

So a freshly-imaged PC with the 7-inch panel plugged in works with no manual
display configuration.
