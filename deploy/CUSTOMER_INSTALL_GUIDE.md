# CodeGloFix — Customer PC Install Guide

Clean production installation of the CodeGloFix face-access system on a new Ubuntu
PC. After install the customer sees **only two desktop icons** — *CodeGloFix Live
View* and *CodeGloFix Admin* — never the program source. The live attendance
display is a **PyQt fullscreen app**; in production it **auto-starts on graphical
login** via the systemd `--user` service `codeglofix-live-display.service`
(`Restart=always`, tied to `graphical-session.target`). There is no browser/kiosk
live view.

---

## 0. Production layout

| Path | Holds | Owner / perms |
|---|---|---|
| `/opt/codeglofix/app` | application code | root:root, read-only to app |
| `/opt/codeglofix/venv` | Python virtualenv | run user |
| `/var/lib/codeglofix` | `gym.db`, `enrolments/shared/faces/*.npz`, `gate_config.json` | run user, `750` |
| `/var/backups/codeglofix` | backups | run user, `750` |
| `/var/log/codeglofix` | `backend.log`, `live_display.log` | run user, `750` |
| `/etc/codeglofix-access.env` | secrets + data paths | **root:root `600`** |
| `~/Desktop` | only the two launcher icons | user |

The app **binds `127.0.0.1:5000` only**; nginx terminates HTTPS on `:443`.

---

> **Before you start:** follow `docs/CUSTOMER_DEPLOYMENT_WORKFLOW.md` (the 8-step
> on-site order). Run `./tools/preinstall_audit.sh` first; if it reports a previous
> install, clean it with `./deploy/remove_previous_install.sh` (it backs up any
> database and never deletes it). Print `CUSTOMER_PREINSTALL_CHECKLIST.pdf` and
> `CUSTOMER_INSTALL_REPORT_TEMPLATE.pdf` for the engineer/customer sign-off.

## 1. Install on the new PC

1. Copy the install package (this project folder) to the new PC, e.g. to
   `~/Downloads/codeglofix-package/` — it does **not** stay here, it gets copied
   into `/opt`.
2. Run the installer as the **normal desktop user** (it calls `sudo` itself). The
   live view is **PyQt only** — no flag is needed:
   ```bash
   cd ~/Downloads/codeglofix-package/deploy
   ./install_customer_pc.sh
   ```
3. Answer the prompts:
   - confirm install,
   - set the **admin password** (used for the admin panel login).
4. When it finishes, delete the downloaded package so the source is not lying
   around:
   ```bash
   rm -rf ~/Downloads/codeglofix-package
   ```

The installer verifies dependencies, copies code to `/opt/codeglofix/app`, builds
the venv, creates the data/log/backup dirs, writes `/etc/codeglofix-access.env`,
installs the backend systemd service + backup timer + logrotate + nginx, installs
the `codeglofix-live-display.service` user unit for the PyQt display, and places the
two desktop icons (*Live View* + *Admin*). At the end it **runs a verification pass**
(service active, live page `/` → 200, admin → 401, app bound to `127.0.0.1:5000`
only, **database created and healthy**, desktop launchers present, no old DigitGym
units enabled) and exits non-zero if any check fails.

### First start, the database, and the “install page”

The database is **created automatically the first time the service starts** — you
do **not** create `gym.db` by hand. On a brand-new PC there is no
`/var/lib/codeglofix/gym.db` until `codeglofix-access@<user>` has started once;
the app then builds it, creates all tables (`members`, `payments`,
`membership_plans`, `access_logs`, `system_settings`), and seeds the default
membership plans and settings.

- **Before the first service start:** an empty/no-database state is *normal*. If a
  page complains there is no data yet, just let the service start.
- **After the service has started:** `/var/lib/codeglofix/gym.db` **must** exist.
  If it does not, the install verification prints **"Database initialization
  failed."** and the installer exits non-zero — check
  `journalctl -u codeglofix-access@$USER -n 60 --no-pager`.

Re-run all checks at any time without changing anything:
```bash
./deploy/install_customer_pc.sh --verify-only
```

### No-hardware install (no camera / relay / 7-inch display yet)

You can install and fully verify on a plain desktop with nothing attached:
```bash
./deploy/install_customer_pc.sh --test-mode-no-hardware
```
The service still starts, the database is still created, live page `/` → 200 and
admin → 401 still pass, and a missing camera or relay does **not** stop the service.

---

## 1a. Supported Ubuntu & Python versions

CodeGloFix runs on **Ubuntu 22.04, 24.04, and 26.x**. The **operating system** and
the **app runtime Python** are decoupled: the OS may ship any Python, but for
production reliability the app's own venv uses **Python 3.10–3.12 (3.12 preferred)**.

> **Production policy:** the supported app runtime is **Python 3.10 / 3.11 / 3.12**.
> **Python 3.14 is EXPERIMENTAL only** — `insightface 0.7.3` (the last release, no
> cp314 wheel) may fail to build on it — and is **never used unless you explicitly
> opt in** with `--allow-experimental-python`.

| Ubuntu | OS default Python | App runtime CodeGloFix uses | Status |
|---|---|---|---|
| 22.04 LTS | 3.10 | **3.10** (`requirements.txt`, pinned) | ✅ Production (reference-verified) |
| 24.04 LTS | 3.12 | **3.12** (`requirements.txt`, pinned) | ✅ Production |
| 26.x | 3.14 | **3.12** — installed from official repos (`requirements.txt`) | ✅ Production |
| 26.x | 3.14 | 3.14 only with `--allow-experimental-python` (`requirements-py314.txt`) | ⚠ Experimental |

**What the installer does, in order (no PPAs — official repos only):**
1. Look for an existing **Python 3.10/3.11/3.12** (prefers 3.12).
2. On Ubuntu 26 where none is present, **install `python3.12` from the official
   Ubuntu repos** and use that.
3. Build the venv, install requirements, and run the import smoke test
   (`fastapi, uvicorn, cv2, numpy, insightface, onnxruntime, serial`).
4. **Python 3.13/3.14 are NOT tried by default.** They are attempted only as a
   last resort, and only if you pass `--allow-experimental-python`, with the
   warning: *"Python 3.14 is experimental for InsightFace. Production recommended
   runtime is Python 3.12."*

**Per-runtime requirements (automatic).** Production runtimes (3.10–3.12) use the
exact pinned `requirements.txt` (verified on 3.10 — **22.04/24.04 unchanged**). The
experimental 3.13/3.14 path uses `requirements-py314.txt`, which keeps the same
packages but relaxes the native stack to cp314 floors. `insightface==0.7.3` has no
cp314 wheel and **builds from source** there (the installer adds `build-essential`
+ `python3.14-dev` for 3.13+) — this is the main reason 3.14 stays experimental.

**Installer options:**
```bash
./install_customer_pc.sh                              # production: Python 3.10–3.12 only
./install_customer_pc.sh --production-python          # same, explicit (never 3.14)
./install_customer_pc.sh --python python3.12          # force a specific interpreter
./install_customer_pc.sh --allow-experimental-python  # also try 3.13/3.14 (last resort)
./install_customer_pc.sh --wheelhouse /path/wheelhouse  # offline install (see §1b)
```

**If no production runtime can be built**, the installer stops cleanly and tells you
to install Python 3.12:
```bash
sudo apt install python3.12 python3.12-venv python3.12-dev   # official repos, no PPA
./install_customer_pc.sh --python python3.12
```
If even that is impossible (e.g. no network, no 3.12 in repos), use the **offline
runtime bundle** (§1b).

**Install record:** `/var/lib/codeglofix/install_info.txt` records the Ubuntu version,
the chosen Python, the **runtime tier** (production / experimental), the requirements
file, the install date, and full `pip freeze`.

---

## 1b. Offline runtime bundle (air-gapped or repeatable installs)

For a guaranteed, repeatable Python 3.12 runtime — independent of what Ubuntu 26
ships and of PyPI availability at install time — pre-build a **wheelhouse** on a
known-good machine and install from it with no internet.

**On a known-good builder PC** (same Ubuntu major + CPU arch as the customer,
Python 3.12 installed):
```bash
cd <package>/deploy
./build_wheelhouse.sh python3.12 ../requirements.txt
# → ./wheelhouse/  (all .whl/.tar.gz)  +  wheelhouse/requirements.lock.txt (frozen)
```

**On the customer PC** (copy the `wheelhouse/` folder over, e.g. via USB):
```bash
./install_customer_pc.sh --python python3.12 --wheelhouse /path/to/wheelhouse
```
The installer then runs `pip install --no-index --find-links <wheelhouse> -r
requirements.txt` — no network, identical versions every time. Because manylinux
wheels (numpy, onnxruntime, opencv) are platform-specific, **build the wheelhouse
on the same Ubuntu major version and architecture** as the target.

---

## 2. Environment / secrets

All config lives in `/etc/codeglofix-access.env` (root `600`). Edit with:
```bash
sudo nano /etc/codeglofix-access.env
sudo systemctl restart codeglofix-access@$USER     # apply changes
```
Key values: `ADMIN_PASSWORD`, `SESSION_SECRET` (auto-generated), `VIDEO_SOURCE`,
and the `CODEGLOFIX_*` data paths (leave as installed). Lower live-display CPU with
`STREAM_INTERVAL_SECONDS=0.20` and `JPEG_QUALITY=50`.

---

## 3. Camera setup

**Connect the camera before installing** (so the hardware check can validate it):
plug the USB webcam into a USB port (a powered USB hub if the run is long) and
confirm Linux sees it:
```bash
ls /dev/video*                     # at least /dev/video0 should appear
v4l2-ctl --list-devices            # optional: shows the model
```
Then set the source:
- USB webcam: `VIDEO_SOURCE=0` (or `1`, `2`…).
- Specific device: `VIDEO_SOURCE=/dev/video0`.
- IP camera: `VIDEO_SOURCE=rtsp://user:pass@<ip>/stream`.

After changing it: `sudo systemctl restart codeglofix-access@$USER`. Confirm frames:
```bash
curl -k -o /dev/null -w '%{http_code}\n' https://localhost/snapshot   # 200 when camera live
```

---

## 4. Relay setup

**Connect the ESP32 relay controller before installing.** Plug it into a USB
port. It presents a USB serial port and is auto-discovered (PING/PONG). Confirm
Linux sees the serial device:
```bash
ls /dev/serial/by-id/* /dev/ttyACM* /dev/ttyUSB*   # one of these should appear
```
Test it from the admin panel (Settings → **Relay Test**, login required) or with
the hardware check below. If the controller is unplugged/replugged the keepalive
thread reconnects automatically. `relay_auto_connect` and `relay_port` (`auto`)
live in `gate_config.json` (`/var/lib/codeglofix/gate_config.json`).

**Baud rate:** the production controller firmware runs at **115200** baud, which
is the default. To override, set `RELAY_BAUDRATE=115200` in
`/etc/codeglofix-access.env` (or edit `relay_baudrate` in `gate_config.json`).

---

## 4a. Hardware validation (camera + ESP32 relay)

With the camera and relay connected, validate both during install:
```bash
./install_customer_pc.sh --hardware-check
```
This runs the camera + relay checks after the service is installed and **before
the final PASS**:

- **Camera** — opens `VIDEO_SOURCE`, captures one frame, saves it to
  `/var/lib/codeglofix/hardware_test/camera_test.jpg`, and prints `CAMERA: PASS/FAIL`.
- **Relay serial** — discovers the USB serial port and does a `PING`/`PONG`
  handshake at 115200 baud, printing `RELAY SERIAL: PASS/FAIL`.
- **Relay TEST pulse** — only after you confirm `This will pulse the relay…`
  (the door may release). Add `--yes-relay-test` to skip the prompt in scripts.
  The door is **never** pulsed without confirmation.

If any hardware check fails, the installer exits non-zero with
**HARDWARE VALIDATION FAILED**. Run the same checks any time, standalone:
```bash
/opt/codeglofix/venv/bin/python /opt/codeglofix/app/tools/hardware_check.py --all
/opt/codeglofix/venv/bin/python /opt/codeglofix/app/tools/hardware_check.py --camera
/opt/codeglofix/venv/bin/python /opt/codeglofix/app/tools/hardware_check.py --relay
/opt/codeglofix/venv/bin/python /opt/codeglofix/app/tools/hardware_check.py --relay-test-confirm
```

**Hardware can also be tested from the Admin panel** (login required):
- **Camera live view** — confirms the camera feed.
- **Relay Test** button — pulses the door relay once.
- **Emergency lock** — forces the door locked.

A full install report is written to **`/var/lib/codeglofix/install_report.txt`**
(install date, Ubuntu/Python versions, service/nginx/DB status, camera + relay
results, live page/admin endpoint codes, and the final PASS/FAIL).

### Troubleshooting camera / relay failures

| Symptom | Likely cause & fix |
|---|---|
| `CAMERA: FAIL — camera not found` | No `/dev/video*`. Reseat the USB cable / use a powered hub; check the webcam works in `cheese`. |
| `CAMERA: FAIL — permission denied` | Service user not in the `video` group. The installer adds it; **log out/in or reboot**, then re-run `--hardware-check`. |
| `CAMERA: FAIL — cannot capture frame` | Camera opened but gave an empty frame — another app is using it, or the cable is marginal. Close other apps, reseat, retry. |
| `CAMERA: FAIL — wrong VIDEO_SOURCE` | `VIDEO_SOURCE` points at a file/placeholder. Set it to `0` or `/dev/video0` in `/etc/codeglofix-access.env`. |
| `RELAY SERIAL: FAIL — no serial ports` | ESP32 not plugged in / not enumerated. Reseat USB; check `ls /dev/ttyACM* /dev/ttyUSB*`. |
| `RELAY SERIAL: FAIL — permission denied` | Service user not in the `dialout` group. The installer adds it; **log out/in or reboot**, then retry. |
| `RELAY SERIAL: FAIL — no PONG` | Wrong baud (must be **115200**), wrong firmware, or it is not the relay. Confirm the firmware answers `PING` with `PONG`. |

---

## 5. 7-inch outside display (PyQt live view)

The outside display is the **PyQt fullscreen live view**. In production it
**auto-starts on graphical login** via the `codeglofix-live-display.service` user
unit (`Restart=always`) — you do **not** launch a browser and there is **no**
primary/secondary display setup to do.

1. **Connect the 7" HDMI display before boot/login.** The display must be present
   at login so the service can target it. With a single display, PyQt goes
   fullscreen on that screen. When several are connected the 7" is **auto-selected
   by priority**: explicit CLI index → an `800x480` panel → `1024x600` → the
   smallest screen → the primary. No manual "set as primary" step is required.
2. Enable **GNOME auto-login** so the live view opens unattended after power-on:
   - *Settings → Users → unlock → Automatic Login = On* for the run user, **or**
     edit `/etc/gdm3/custom.conf`:
     ```ini
     [daemon]
     AutomaticLoginEnable=true
     AutomaticLogin=<your-username>
     ```
     then `sudo systemctl restart gdm3` (will log you in).
3. Reboot. On login the PyQt live display starts automatically, waits for the
   backend to be ready, and shows **only** the gate camera + the member's own
   Granted/Denied result — never admin controls or member data. If it crashes the
   `Restart=always` user unit brings it straight back.

> **Exiting the display:** the **ESC** key is **ignored by default** so the customer
> cannot accidentally close the view. For testing you can allow it with
> `LIVE_DISPLAY_ALLOW_ESC=1`. The emergency exit is always **Ctrl+Alt+Q**.

You can also launch the PyQt live view manually at any time from the **CodeGloFix
Live View** desktop icon.

### Required customer-PC setup (door PC)

For an unattended door PC that comes back on its own after any power event:

1. **Connect the 7" HDMI display before boot/login** (step 1 above).
2. **Enable GNOME auto-login** for the run user (step 2 above).
3. **Enable BIOS "Restore on AC Power Loss"** (a.k.a. *AC Back / After Power Loss*
   → set to *Power On* / *Last State*) so the PC powers itself up after a mains
   cut.
4. **Do a reboot test** — reboot and confirm the PyQt live view comes up on the 7"
   display on its own.
5. **Do a power-cut test** — pull mains, restore it, and confirm the PC powers on,
   auto-logs in, and the live view returns unattended.

> **7-inch physical test is still required** for final hardware proof — the live
> view must be confirmed running full-screen on the actual 7" panel, not just on a
> bench monitor.

### Verify the live display

```bash
cd /opt/codeglofix/app
./deploy/verify_pyqt_display.sh
./deploy/verify_pyqt_display_install.sh
systemctl --user status codeglofix-live-display.service --no-pager
```

Logs:
```bash
journalctl --user -u codeglofix-live-display.service -e
tail -f /var/log/codeglofix/live_display.log
```

---

## 6. Admin icon usage (inside)

- Double-click **CodeGloFix Admin** on the desktop → opens `https://localhost/admin`.
- Log in with the admin password. Manage members, payments, enrolment, relay
  test, emergency lock, logs and settings here.
- The admin panel **always requires login** and never auto-opens, so it is never
  shown on the outside display.

### Change or recover the admin password

The admin password is stored in `/etc/codeglofix-access.env` (root-only, `0600`).
To change it — or to recover access if it is forgotten — edit that file and
restart the service:

```bash
sudo nano /etc/codeglofix-access.env
# change the line to:   ADMIN_PASSWORD=your-new-password
sudo systemctl restart codeglofix-access@$(whoami).service
```

Save (`Ctrl+O`, Enter) and exit (`Ctrl+X`), then log in again from the
**CodeGloFix Admin** icon with the new password. Because the file is root-owned,
this reset always works even if the panel password is lost.

---

## 7. Backup / restore

- **Automatic:** daily 03:30 via `codeglofix-backup@$USER.timer` (catches up after
  boot if the PC was off). Backups land in `/var/backups/codeglofix`.
- **Manual backup now:**
  ```bash
  sudo systemctl start codeglofix-backup@$USER
  systemctl list-timers codeglofix-backup@$USER --no-pager   # next run
  ```
- **Migrate data from another PC:** see "Data migration" below.

---

## 8. Start / stop / restart

```bash
sudo systemctl start    codeglofix-access@$USER     # start app
sudo systemctl stop     codeglofix-access@$USER     # stop app
sudo systemctl restart  codeglofix-access@$USER     # restart app
sudo systemctl status   codeglofix-access@$USER --no-pager
sudo systemctl restart  nginx                # restart proxy
journalctl -u codeglofix-access@$USER -e            # live logs (or tail /var/log/codeglofix/backend.log)
```

---

## 9. Data migration (old PC → new PC)

On the **source** PC:
```bash
cd <project>/deploy
./export_codeglofix_data.sh                 # → codeglofix_data_<host>_<date>.tar.gz
```
Copy the file to the customer PC (USB or scp), then on the **customer** PC:
```bash
cd /opt/codeglofix/app/deploy
./import_codeglofix_data.sh /path/to/codeglofix_data_*.tar.gz
```
The import stops the service, backs up current `/var/lib/codeglofix` to
`/var/backups/codeglofix/predeploy_*`, restores the bundle, fixes permissions, and
restarts. No secrets are ever exported.

---

## 10. Troubleshooting

| Problem | Fix |
|---|---|
| **502 Bad Gateway** | App not up. `sudo systemctl status codeglofix-access@$USER`; `tail /var/log/codeglofix/backend.log`. Check `ADMIN_PASSWORD`/`SESSION_SECRET` are set in `/etc/codeglofix-access.env`. |
| **Camera missing / black feed** | `v4l2-ctl --list-devices`; set correct `VIDEO_SOURCE`; `sudo systemctl restart codeglofix-access@$USER`. Check USB autosuspend (start.sh disables it). |
| **Relay missing** | Plug in the USB controller; check `dmesg | grep ttyUSB`; relay auto-reconnects. Confirm `relay_auto_connect` true in `/var/lib/codeglofix/gate_config.json`. |
| **Certificate warning** | Expected only when you open `https://localhost` **manually** in a browser (self-signed cert). The PyQt live view does not use a browser and never shows it; the **Admin** launcher uses `--ignore-certificate-errors`. To silence it for manual browsing too, import `/etc/nginx/ssl/face-attendance.crt` into the browser/OS trust store. |
| **Live view didn't open on login** | Is auto-login on? Check the user service: `systemctl --user status codeglofix-live-display.service --no-pager`. Logs: `journalctl --user -u codeglofix-live-display.service -e` or `tail /var/log/codeglofix/live_display.log`. Confirm the 7" HDMI was connected before login. |
| **Live view opens on the wrong screen** | It auto-selects the 7" by priority (CLI index → 800x480 → 1024x600 → smallest → primary). Make sure the 7" panel is connected before login; re-run `./deploy/verify_pyqt_display.sh` to see the detected screens. |
| **Live view exits accidentally** | ESC is ignored by default. Only `LIVE_DISPLAY_ALLOW_ESC=1` re-enables it (testing only); the emergency exit is **Ctrl+Alt+Q**. |
| **Admin icon does nothing / shows cert page** | Almost always **no Chrome/Chromium installed** — the launcher then falls back to a browser that can't bypass the self-signed cert. Install Google Chrome: `curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && sudo apt install -y /tmp/chrome.deb`. Do **not** change the script to `http://` — nginx 301-redirects it back to `https://`. |
| **Admin icon won't launch (untrusted)** | Right-click → *Allow Launching*, or `gio set ~/Desktop/CodeGloFix-Admin.desktop metadata::trusted true`. |
| **`open_admin.sh` was edited to `http://`** | Revert to the audited HTTPS version: `sudo sed -i 's#ADMIN_URL:-http://localhost/admin#ADMIN_URL:-https://localhost/admin#' /opt/codeglofix/app/kiosk/open_admin.sh`. |
| **App reachable from LAN on :5000** | It must not be — confirm `ss -ltnp | grep 5000` shows `127.0.0.1:5000`. Only nginx :443 should be LAN-facing. |
| **"Could not build a working CodeGloFix runtime"** | No production Python (3.10–3.12) worked. Install Python 3.12 from official repos and force it: `sudo apt install python3.12 python3.12-venv python3.12-dev && ./install_customer_pc.sh --python python3.12`. Air-gapped? Use the offline wheelhouse (§1b). See §1a. |
| **InsightFace fails on Python 3.14** | Expected — 3.14 is experimental (no insightface cp314 wheel). Use the production runtime: `./install_customer_pc.sh --python python3.12`. Do not run production on 3.14. |

---

## 11. Tidying the desktop after install

If the installation package was left on the Desktop, move it out of sight so the
customer only sees the two CodeGloFix icons:
```bash
# Move the leftover package folder out of ~/Desktop (keep a copy elsewhere):
mv ~/Desktop/CodeGloFix ~/codeglofix_package_backup    # adjust the folder name to match

# Remove any stray launcher that points at the old Desktop copy:
rm -f ~/Desktop/CodeGloFix-LiveView.desktop ~/Desktop/CodeGloFix-Admin.desktop
```
Then re-run `install_customer_pc.sh` so the icons point at `/opt/codeglofix/app`.
The customer should then see just the two CodeGloFix icons and no package folder.

---

## 12. Remove a previous install

To clear an existing install before re-installing, use the **non-destructive**
helper — it stops the services, removes the units/nginx/launchers, and **preserves
the database and enrolments** (it backs them up, never deletes them):

```bash
./deploy/remove_previous_install.sh
```

This is the recommended path; there is no rollback-to-browser step to run. If you
need to stop things by hand, disable the backend service, the backup timer, and the
PyQt live-display **user** service:

```bash
systemctl --user disable --now codeglofix-live-display.service
sudo systemctl disable --now codeglofix-access@$USER codeglofix-backup@$USER.timer
```

The database, enrolled members, face data, gate config and logs under
`/var/lib/codeglofix` and `/var/backups/codeglofix` are left intact so a
re-install keeps every member.
