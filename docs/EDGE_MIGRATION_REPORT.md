# CodeGloFix Access Control — Edge Device Migration Report

**Prepared:** 2026-07-17
**Subject:** Converting the current production PC deployment into a cost-effective edge device
**Scope:** Based on a direct read of the current source (`gym_face_access/`). No code was changed.

---

## 1. Executive summary (read this first)

Your instinct is "rewrite in C++ and compress it." After reading the code, the honest
engineering answer is:

> **Do NOT rewrite in C++. Instead, move the *same code* onto a cheap ARM single-board
> computer (SBC), drop the desktop-only pieces, and — only if you need more speed —
> convert the two small models to run on the board's NPU.**

Here is *why* a C++ rewrite is the wrong lever:

- **The expensive work is already C++.** Your face detection and recognition run inside
  **ONNX Runtime** (a C++ engine) and **OpenCV** (C++). Matching runs in **NumPy** (C/BLAS).
  Rewriting your Python in C++ would re-implement ~5,300 lines of already-verified logic
  and give you **almost no inference speedup**, because the neural network is *already native*.
- **Your models are already "edge class."** You use InsightFace `buffalo_sc` — their
  *smallest* pack:
  - `det_500m.onnx` — **2.5 MB** (face detector)
  - `w600k_mbf.onnx` — **13.6 MB** (MobileFaceNet ArcFace embedding, 512-dim)
  - Total model footprint: **~16 MB.** This already fits a Raspberry Pi.
- **The cost saving comes from the hardware, not the language.** Going from a
  $400–600 mini-PC to a $60–150 SBC is where "cost-effective edge device" actually happens.

**Bottom line:** the fastest, lowest-risk, cheapest path is **Option A** below. It reuses
100% of your working code.

---

## 2. What your system actually is today

| Layer | Technology | Notes for edge port |
|---|---|---|
| Web/API backend | FastAPI + Uvicorn (`main.py`, 2,121 lines) | Pure Python glue. Runs fine on ARM. |
| Face detection + embedding | InsightFace `buffalo_sc` on **ONNX Runtime (CPU)** (`identity_engine.py`) | **Already native C++.** The real compute. |
| Camera + drawing | **OpenCV** (`cv2`) | Already native C++. ARM wheels exist. |
| Matching | **NumPy** cosine similarity `matrix @ query` (`_match_face`) | Already native/BLAS. Vectorized, scales to 1,000+ members. |
| Business rules / gate logic | Python (`face_gate.py`, `gym_access.py`) | Pure glue. Small, already verified. Do not rewrite. |
| Database | **SQLite** (`gym.db`, ~650 KB) | *Perfect* for edge — no server, single file. Keep as-is. |
| Enrolments | ArcFace embeddings in `.npz` (~56 KB) | Tiny. Keep as-is. |
| Door hardware | Serial to relay controller firmware (`relay_controller.py`, pyserial) | All timing lives in firmware. Trivially portable. |
| Admin UI | Web (Jinja templates + FastAPI, session-cookie auth) | Already a browser app — key to solving the "same PC" problem. |
| Live display / kiosk | **PyQt6 + PyQt6-WebEngine (Chromium)** (`kiosk/`) | **Heavy: ~540 MB venv.** This is your biggest cut candidate. |
| Deployment | systemd units, nginx, Ubuntu 22.04 installers (`deploy/`) | Already production-grade. Reusable on ARM Ubuntu/Debian. |

**Key measured facts**
- App code footprint: **2.4 MB** (excluding models and the Qt venv).
- The `pyqt_display_test/.venv` alone is **542 MB** — almost entirely Qt + Chromium.
- The detection pipeline is already thread-separated (capture thread, detection thread,
  stale-frame watchdog, relay keepalive) — a clean, edge-friendly design.

---

## 3. The migration options, ranked

### ✅ Option A — Keep Python, move to a cheap ARM SBC *(RECOMMENDED)*

Reuse **all** existing code. `onnxruntime`, `opencv-python`, `numpy`, `fastapi` all have
ARM64 wheels. The only real work is packaging + testing on the new board.

**Recommended hardware (pick one):**

| Board | ~Price | Why | Recognition speed |
|---|---|---|---|
| **Raspberry Pi 5 (8 GB)** | ~$80 | Best support, easiest, huge community | CPU-only, ~2–5 FPS on buffalo_sc — enough for a door |
| **Radxa Rock 5 / Orange Pi 5 (RK3588)** | ~$100–150 | Has a **6 TOPS NPU** you can offload the models to | 15–30+ FPS with RKNN INT8 |
| **Hailo-8L on a Pi 5** | ~$80 + $70 hat | 13 TOPS AI accelerator, keeps Pi ecosystem | Very fast, low CPU |

- **Effort:** days-to-weeks (mostly re-testing, not coding).
- **Risk:** **low** — same code, same verified logic.
- **Cost win:** the biggest one. This is where "cost-effective" comes from.

> Start here. Buy a Pi 5 (8GB), flash Ubuntu 24.04 arm64, run your existing
> `deploy/install_customer_pc.sh` (adjust for arm64 wheels), and measure. In most gym
> door scenarios a Pi 5's CPU is already enough — you only move to Option B if the
> measured FPS is too low for your throughput.

### ⚙️ Option B — Native acceleration (only if Option A is too slow)

Do **not** rewrite the app. Instead accelerate *just the two models*:

- **Rockchip RK3588** → convert `.onnx` → **RKNN**, quantize **INT8**, run on the 6-TOPS NPU.
- **Hailo** → compile models to Hailo format via their SDK.
- **Google Coral** → convert to EdgeTPU TFLite (needs a retrain/convert step for ArcFace).

You keep FastAPI, SQLite, the relay code, and all business logic in Python. Only the
`analyse_face()` inference call swaps to the NPU runtime. This is where **5–10× speedups**
live — *not* in Python→C++.

- **Effort:** 1–3 weeks per accelerator (model conversion + accuracy re-validation).
- **Risk:** medium — INT8 quantization can shift recognition scores; you must re-tune the
  `identity_threshold` (currently `0.60`) and re-verify against your enrolled set.

### ❌ Option C — Full C++ rewrite *(NOT recommended)*

- Months of work re-implementing already-working, already-verified logic.
- You lose the FastAPI/Jinja admin ecosystem and rebuild the web UI + auth + DB layer by hand.
- **Near-zero inference gain** — the models already run in native C++ via ONNX Runtime.
- Only justified on a microcontroller-class device with *no* OS/Python (e.g. an ESP32-class
  MCU). Your workload (live camera + web admin + SQLite) does not fit that class anyway.

**Verdict:** skip C++. It is effort spent on the layer that is *already* native.

---

## 4. The real architectural change: "admin uses the same PC"

This is the part that genuinely needs design work, because an edge device usually has **no
monitor/keyboard**. Good news: **your admin UI is already a web app**, so this is mostly a
networking + display decision, not a rewrite.

**Decide the device's role:**

**Model 1 — Headless door unit (cheapest, recommended for scale)**
- The SBC sits at the gate, runs the backend, drives the relay. No screen.
- Admin opens the dashboard **from their phone or laptop** over the gym's LAN/Wi-Fi.
- You already have `nginx` config + cookie auth. Just:
  - bind the app to `0.0.0.0` (already served on `:5000`),
  - give the device a stable name (mDNS, e.g. `codeglofix.local`, or a static IP),
  - keep HTTPS + the existing auth (`auth.py`).
- **Cut the entire PyQt6/WebEngine stack** → saves ~540 MB and removes Chromium from the
  device. Members at the door see nothing, or see a cheap attached HDMI screen (Model 2).

**Model 2 — Door unit *with* a small attached screen**
- Cheap HDMI panel at the gate shows the live "granted/denied" view.
- On an SBC, do **not** ship PyQt6+Chromium. Instead show the existing **MJPEG stream**
  (`main.py` already serves it) full-screen via a lightweight viewer, or a minimal
  fullscreen browser page. Same visual result, a fraction of the footprint.

Either way, the admin no longer needs to physically sit at the device — they manage it
from any browser on the network. That directly solves your "without this idea I have to
think of another way" concern.

---

## 5. "Compress" / footprint-reduction ideas (concrete)

| Idea | Saving | Effort |
|---|---|---|
| **Drop PyQt6 + PyQt6-WebEngine** on headless units | **~540 MB**, removes Chromium | Low — it's already an optional wrapper (`requirements-display.txt`) |
| Serve the door screen via existing MJPEG stream instead of Qt | Removes the entire Qt/Chromium runtime | Low |
| **INT8-quantize** the two ONNX models | Smaller models + big speedup on NPU | Medium (re-tune thresholds) |
| Build **onnxruntime** without unused execution providers | Smaller runtime | Medium |
| Run Uvicorn with a **single worker** on the SBC | Lower RAM | Trivial (config) |
| Keep **SQLite** (do not "upgrade" to a DB server) | It's already ideal for edge | None — keep as-is |
| Use a **read-only root FS + overlay** for the app code | Reliability on power-loss (gyms lose power) | Medium |

Your models (~16 MB) and app code (~2.4 MB) are already tiny. **The single biggest byte
saving on the whole system is deleting the Qt/Chromium display stack** on headless units.

---

## 6. Recommended phased plan

1. **Phase 0 — Baseline (0.5 day).** On the current PC, measure end-to-end recognition FPS
   and CPU usage. This is your target number.
2. **Phase 1 — Prove it on a Pi 5 (1–2 weeks).** Flash Ubuntu arm64, install with arm64
   wheels, run headless, admin via phone/laptop. Measure FPS. **In many gym-door cases you
   stop here** — this is the whole "cost-effective edge device."
3. **Phase 2 — Only if too slow: add an NPU (1–3 weeks).** Move to RK3588 or a Hailo hat,
   convert + INT8-quantize the two models, **re-verify recognition accuracy** against your
   enrolled members, re-tune `identity_threshold`.
4. **Phase 3 — Harden (1 week).** Read-only FS/overlay, watchdog on power-loss, field-update
   mechanism, and a documented "flash-and-go" SD/eMMC image so every new gym is a clone.
5. **Phase 4 — Productize the image.** One golden image → duplicate per site. This is the
   actual "cost-effective" multiplier: hardware ~$100 + a 10-minute flash per gym.

---

## 7. Direct answers to your questions

- **"Convert to C++?"** — No. The compute is already C++ (ONNX Runtime, OpenCV). Rewriting
  buys near-zero speed for months of risk. Keep Python.
- **"Any compress idea?"** — Yes: delete the PyQt6/Chromium display stack (~540 MB) on
  headless units, INT8-quantize the two models, trim onnxruntime providers. Models/app are
  already tiny.
- **"Cost-effective edge device?"** — Move to a ~$80–150 ARM SBC (Raspberry Pi 5, or RK3588
  if you need the NPU). That, not the language, is where the cost drops.
- **"Admin uses the same PC — need another way?"** — Your admin UI is already web-based. Run
  the device headless and let admins manage it from a phone/laptop over the LAN. You already
  have nginx + auth for this.

---

## 8. Risks / things to verify before committing

- **CPU-only FPS on the Pi 5** — must be measured (Phase 1); it decides whether you need Phase 2.
- **INT8 accuracy drift** — quantization can change match scores; always re-validate against
  real enrolled faces and re-tune thresholds. Never ship quantized without this check.
- **Camera compatibility** — confirm your USB/CSI camera works with OpenCV on the target board
  (the capture loop already handles reconnect, which helps).
- **Power loss** — gyms lose power; plan a read-only/overlay FS so `gym.db` and the SD card
  survive hard cuts.
- **Serial/relay wiring** — `/dev/ttyUSB*` vs `/dev/ttyACM*` device names differ per board;
  your `relay_port: "auto"` discovery already handles this, just re-test.

---

*This report is intentionally opinionated: the highest-value move is a cheap ARM board running
your existing, already-working code — not a rewrite.*
