# CodeGloFix Gym Access System
## Final Full Installation, Previous Install Removal, Backup, Restore & Fresh System Guide

**Company:** CodeGloFix
**Developed by:** AI Engineer Lakshan L.
**Date:** 2026-07-17
**Architecture:** PyQt-only live display · local backend · browser admin panel

---

## 1. Purpose of this guide

This document is the single, complete reference for deploying and maintaining the
CodeGloFix Gym Access System on a customer PC. It covers:

- New / fresh installation
- Previous install removal (complete)
- Backing up a previous install before removal
- Reinstalling while keeping and reusing the old database
- A completely fresh install with the old database removed
- Restoring previously exported data after a reinstall
- Verification and troubleshooting

Read Section 10 ("Decision guide") first if you are unsure which path applies to a
particular customer situation.

---

## 2. Final architecture summary

- **PyQt-only live attendance display.** The outside 7-inch screen is driven by the
  PyQt6 live-display wrapper. It shows the composited camera feed and the access
  result popup.
- **Chrome kiosk live view has been removed.** There is no Chromium kiosk for the
  live view anymore — only the PyQt display.
- **Admin panel opens manually in a browser.** It is login-gated and is never shown
  on the public outside display.
- **Backend runs locally** as a single-worker service on the PC.
- **Main backend URL:** `http://127.0.0.1:5000/`
- **Admin URL:** `https://localhost/admin` (production, behind nginx TLS) or
  `http://127.0.0.1:5000/admin` in a plain local test session.
- **All persistent data is stored under** `/var/lib/codeglofix`.

---

## 3. Important paths

| Item | Path |
|---|---|
| Source path (dev) | `/home/auto/Desktop/display/gym_face_access` |
| Installed app | `/opt/codeglofix/app` |
| Installed venv | `/opt/codeglofix/venv` |
| Data directory | `/var/lib/codeglofix` |
| Database | `/var/lib/codeglofix/gym.db` |
| Face enrolments | `/var/lib/codeglofix/enrolments/shared` |
| Gate config | `/var/lib/codeglofix/gate_config.json` |
| Backup folder | `/var/backups/codeglofix` |
| Logs | `/var/log/codeglofix` |
| Environment file | `/etc/codeglofix-access.env` |

Service names:

- `codeglofix-access@<user>.service` — the backend
- `codeglofix-live-display.service` — the PyQt live display (user service)
- `codeglofix-backup@<user>.{service,timer}` — scheduled DB backups
- `codeglofix-health.{service,timer}` — health watchdog

---

## 4. Before removing a previous install — BACK UP FIRST

**This is the recommended first step for any change on an existing PC.**

Local backup (database + face embeddings + gate config + secrets):

```bash
cd /opt/codeglofix/app
./backup_db.sh
```

Portable export bundle (for moving data to another PC):

```bash
cd /opt/codeglofix/app
./deploy/export_codeglofix_data.sh
```

The export produces `codeglofix_data_<host>_<date>.tar.gz` containing:

- `gym.db`
- face embeddings / enrolments (`enrolments/shared/faces/arcface_embeddings.npz` + any face images)
- `gate_config.json`
- an env **SAMPLE** (no secrets) for reference

> ⚠️ **WARNING — do not skip this.** Never remove or wipe the old database before a
> backup unless the customer explicitly confirms they do **not** need the old
> members, face data, plans, payments, access logs, or gate configuration. Deletion
> of `/var/lib/codeglofix` is permanent.

---

## 5. Remove previous install but KEEP old data

This removes the app, services, desktop icons, and nginx setup, but **preserves**
`/var/lib/codeglofix` — including the old `gym.db`.

```bash
cd /opt/codeglofix/app
./deploy/remove_previous_install.sh --dry-run   # preview only — changes nothing
./deploy/remove_previous_install.sh             # perform removal
```

- `--dry-run` prints every action without changing anything.
- The default removal **does not delete** `/var/lib/codeglofix`.
- The old database remains available for the next install.
- Previous data (members, enrolments, logs, plans, payments) can be reused
  automatically by the next install.
- The uninstaller also **offers to back up** the data dir before removal; pass
  `--no-db-backup` only if you already have a backup. Add `--yes` to auto-confirm
  prompts.

---

## 6. Reinstall using the old database

This is the important "keep old data" path. If `/var/lib/codeglofix/gym.db`
already exists, the installer **preserves it** and the refreshed app reuses it (the
app file sync explicitly excludes `gym.db`, `gym.db-wal`, and `gym.db-shm`).

```bash
cd /home/auto/Desktop/display/gym_face_access
./deploy/install_customer_pc.sh
```

Then verify:

```bash
cd /opt/codeglofix/app
./deploy/verify_pyqt_display.sh
./deploy/verify_pyqt_display_install.sh
curl -s http://127.0.0.1:5000/api/health
```

What is preserved:

- Old members remain.
- Old face enrolments remain.
- Old access logs remain.
- Old payment / plan data remains if present.
- App files are refreshed to the new version.
- The database is **not** deleted.

> ℹ️ **Note (old-schema databases).** If, after a reinstall on a very old database,
> the Admin **Plans** tab shows an error, the preserved `membership_plans` table may
> predate a newer column. See Troubleshooting (Section 17) for the read-only schema
> check. The customer database must be preserved — never delete it to "fix" this.

---

## 7. Restore old backed-up data after reinstall

Use the import script to restore a previously exported bundle:

```bash
cd /opt/codeglofix/app
./deploy/import_codeglofix_data.sh /path/to/codeglofix_data_<host>_<date>.tar.gz
```

The import safely: stops the service → backs up the current `/var/lib/codeglofix`
to `/var/backups/codeglofix` → restores `gym.db`, `enrolments/shared`, and
`gate_config.json` from the bundle → fixes ownership/permissions → restarts the
service.

Use this when:

- The database was moved to another PC.
- Old data was exported before the reinstall.
- The customer wants previous members / enrolments restored.

---

## 8. Completely remove previous install AND old database (fresh-system path)

> 🛑 **STRONG WARNING.** The commands below **permanently delete** old member data,
> face enrolments, access logs, plans, payments, and gate configuration — unless you
> have already taken a backup (Section 4). There is no undo.

Step 1 — remove the install (preview, then run):

```bash
cd /opt/codeglofix/app
./deploy/remove_previous_install.sh --dry-run
./deploy/remove_previous_install.sh
```

Step 2 — delete the old data and logs:

```bash
sudo rm -rf /var/lib/codeglofix
sudo rm -rf /var/log/codeglofix
```

Optional — only if the customer wants to delete backups too:

```bash
sudo rm -rf /var/backups/codeglofix
```

> ⚠️ Do **not** delete `/var/backups/codeglofix` unless the customer is certain no
> backup is needed. That folder is your last safety net.

---

## 9. Fresh system installation after a full wipe

```bash
cd /home/auto/Desktop/display/gym_face_access
./deploy/install_customer_pc.sh
```

This creates:

- a new `/opt/codeglofix/app`
- a new `/opt/codeglofix/venv`
- a new `/var/lib/codeglofix`
- a new `gym.db` (with default membership plans seeded)
- a fresh service setup
- a clean system with no old member data

---

## 10. Decision guide — which path to choose

| Scenario | Use this path |
|---|---|
| Customer wants old members/data kept | Backup (§4) → remove previous install (§5) → reinstall (§6). Do **not** delete `/var/lib/codeglofix`. |
| Customer wants old data transferred | Export (§4) → fresh install (§9/§11) → import bundle (§7). |
| Customer wants a completely clean system | Backup if needed (§4) → remove previous install (§5) → delete `/var/lib/codeglofix` (§8) → install fresh (§9). |
| Previous install broken but data needed | Backup / export first (§4), then reinstall using old DB (§6). |
| Previous database corrupted / unwanted | Backup first (§4), then full wipe (§8) and fresh install (§9). |

---

## 11. Normal install command

```bash
cd /home/auto/Desktop/display/gym_face_access
./deploy/install_customer_pc.sh
```

Run as the normal desktop user (the script calls `sudo` itself where needed).

---

## 12. Verification commands

```bash
cd /opt/codeglofix/app
./deploy/verify_pyqt_display.sh
./deploy/verify_pyqt_display_install.sh
curl -s http://127.0.0.1:5000/api/health
systemctl status codeglofix-access@$(whoami).service --no-pager
systemctl --user status codeglofix-live-display.service --no-pager
```

A healthy backend returns JSON from `/api/health` with `ok: true` and a small
`camera_fresh_seconds_ago` value.

---

## 13. Admin open command

```bash
cd /opt/codeglofix/app
./kiosk/open_admin.sh
```

- **Admin URL (production):** `https://localhost/admin`
- **Admin URL (local test):** `http://127.0.0.1:5000/admin`

The admin panel is login-gated. It is never shown on the outside display.

---

## 14. PyQt live display — manual command

```bash
LIVE_DISPLAY_URL=http://127.0.0.1:5000/ \
LIVE_DISPLAY_HEALTH_URL=http://127.0.0.1:5000/api/health \
/opt/codeglofix/venv/bin/python /opt/codeglofix/app/kiosk/live_display.py 0
```

- The trailing `0` selects monitor index 0.
- **Exit:** `Ctrl + Alt + Q`
- Normally the display starts automatically via `codeglofix-live-display.service`
  or the "CodeGloFix Live View" desktop icon; the command above is for manual tests.

---

## 15. Service commands

Backend service:

```bash
systemctl status codeglofix-access@$(whoami).service --no-pager
sudo systemctl restart codeglofix-access@$(whoami).service
journalctl -u codeglofix-access@$(whoami).service -e --no-pager
```

PyQt live-display (user) service:

```bash
systemctl --user status codeglofix-live-display.service --no-pager
systemctl --user restart codeglofix-live-display.service
journalctl --user -u codeglofix-live-display.service -e --no-pager
```

---

## 16. Performance settings

Current production defaults (set in `start.sh`, all env-overridable):

| Setting | Default | Meaning |
|---|---|---|
| `STREAM_INTERVAL_SECONDS` | `0.0556` | MJPEG display pacing (~18 fps) |
| `CAMERA_WIDTH` | `896` | Requested capture width |
| `CAMERA_HEIGHT` | `504` | Requested capture height |
| `KIOSK_FG_SCALE` | `0.88` | Foreground scale; leaves a visible blurred border |

These affect the **display only** — recognition (model, threshold, detector input
320×320) is unchanged. The camera negotiates the nearest supported mode and falls
back to 640×480 only if it rejects the request.

**15 fps fallback** — if CPU is high on the target PC, revert with env overrides (no
code change):

```bash
STREAM_INTERVAL_SECONDS=0.0667 CAMERA_WIDTH=960 CAMERA_HEIGHT=540
```

> Do **not** attempt 20 fps unless a CPU measurement on the target PC confirms
> safe headroom.

---

## 17. Troubleshooting

**Plans tab shows Loading / error after a reinstall.**
The preserved database may have an older `membership_plans` schema. Check it
(read-only — this does not modify data):

```bash
sqlite3 /var/lib/codeglofix/gym.db "PRAGMA table_info(membership_plans);"
```

The current schema has columns: `id, plan_name, duration_days, price, description,
is_active`. If a column is missing, that is the cause. Do **not** delete the
database — preserve it and apply the backend fix. The Admin UI will now show a clear
message ("Could not load plans (HTTP …)" / "Session expired") instead of hanging on
"Loading".

**Backend health failed.**
`curl -s http://127.0.0.1:5000/api/health` returns nothing or `ok: false`. Check the
service log: `journalctl -u codeglofix-access@$(whoami).service -e --no-pager`.

**PyQt display does not open.**
Ensure the backend is up first, then run the manual command in Section 14. Confirm
`/opt/codeglofix/venv` has PyQt6 installed. Check
`systemctl --user status codeglofix-live-display.service`.

**Admin login issue.**
Confirm `ADMIN_PASSWORD` and `SESSION_SECRET` are set in
`/etc/codeglofix-access.env`. In local test, cookies require `COOKIE_SECURE=false`
over plain HTTP.

**Camera not found.**
Confirm `/dev/video0` exists and `VIDEO_SOURCE` is correct. Check the backend log for
the `[CAM] Capture resolution:` line.

**Relay not connected.**
Check `RELAY_PORT` in the environment. For a bench test with no relay hardware, use
`RELAY_PORT=/dev/ttyRELAY_NONE` — the app logs a safe "cannot connect" and never
opens a door.

**No blur visible on the 7-inch display.**
Ensure `KIOSK_FG_SCALE=0.88` (or lower) is in effect. At `1.0` and a widescreen
capture, the blurred border is nearly invisible.

**High CPU / low FPS.**
Use the 15 fps fallback env in Section 16. Measure delivered FPS by reading the
`/video_feed_kiosk` stream, not by opening the camera directly.

**Wrong venv / numpy ABI error (`numpy.dtype size changed`).**
InsightFace must run in the installed venv (`/opt/codeglofix/venv`) with the pinned
`numpy==1.26.4`, not system Python. Confirm the backend log shows
`Using python: /opt/codeglofix/venv/bin/python`.

---

## 18. Final handover checklist

- [ ] Previous data backed up (`backup_db.sh` / `export_codeglofix_data.sh`)
- [ ] Previous install removed if required
- [ ] Old DB preserved OR wiped according to the customer's decision
- [ ] Install completed
- [ ] Backend health OK (`/api/health`)
- [ ] PyQt display opens fullscreen
- [ ] Admin panel opens
- [ ] Camera works
- [ ] Recognition works
- [ ] Relay tested if available
- [ ] 7-inch display tested
- [ ] Reboot test passed
- [ ] Power-cut recovery tested
- [ ] PDF guide delivered

---

*CodeGloFix · CodeGloFix Gym Access System · Developed by AI Engineer Lakshan L. ·
2026-07-17*
