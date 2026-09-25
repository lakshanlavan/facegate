#!/usr/bin/env python3
"""
hardware_check.py — installation-time hardware validation for CodeGloFix Access Control.

Validates the two pieces of field hardware before go-live:
  • Camera      — opens the configured VIDEO_SOURCE, grabs one frame, saves a JPEG.
  • ESP32 relay — auto-discovers the USB serial port and does a PING/PONG handshake.
                  Optionally fires a one-second TEST pulse, but ONLY after explicit
                  confirmation (never opens the door by accident).

Run standalone:
    python tools/hardware_check.py --camera
    python tools/hardware_check.py --relay
    python tools/hardware_check.py --all
    python tools/hardware_check.py --relay-test-confirm      # prompts before pulsing
    python tools/hardware_check.py --all --yes-relay-test    # pulse without prompt (scripted)

Environment (same names the app uses):
    VIDEO_SOURCE     camera index/device/file       (default: 0)
    RELAY_PORT       'auto' or /dev/tty…             (default: auto)
    RELAY_BAUDRATE   serial baud                     (default: 115200)
    CODEGLOFIX_DB_PATH  used to locate the data dir for the saved test frame

Exit code: 0 only if every REQUESTED check passed; non-zero otherwise. The TEST
pulse is required only when it was actually requested/run.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time

RELAY_BAUDRATE_DEFAULT = 115200


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _data_dir(explicit: str | None) -> str:
    if explicit:
        return explicit
    db = os.environ.get("CODEGLOFIX_DB_PATH") or os.environ.get("DIGITGYM_DB_PATH")
    if db:
        return os.path.dirname(os.path.abspath(db))
    return "/var/lib/codeglofix"


def _parse_video_source(raw: str):
    """Mirror main._parse_video_source: (source, is_file, label)."""
    value = (raw or "0").strip()
    lower = value.lower()
    if value.isdigit():
        return int(value), False, "WEBCAM"
    if lower.startswith("/dev/video"):
        return value, False, "WEBCAM"
    if lower.startswith(("rtsp://", "rtmp://", "http://", "https://")):
        return value, False, "STREAM"
    return value, True, "VIDEO FILE"


def _candidate_serial_ports():
    """Auto-discover USB serial ports — same order the relay controller uses."""
    ports = []
    for pattern in ("/dev/serial/by-id/*", "/dev/ttyACM*", "/dev/ttyUSB*"):
        ports.extend(sorted(glob.glob(pattern)))
    seen, unique = set(), []
    for p in ports:
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp)
            unique.append(p)
    return unique


# ──────────────────────────────────────────────────────────────────────────────
# Camera
# ──────────────────────────────────────────────────────────────────────────────
def check_camera(out_path: str | None, data_dir: str) -> tuple[bool, str]:
    video_source = os.environ.get("VIDEO_SOURCE", "0")
    source, is_file, label = _parse_video_source(video_source)

    devices = sorted(glob.glob("/dev/video*"))
    print(f"  VIDEO_SOURCE      : {video_source}  (mode: {label})")
    print(f"  /dev/video* found : {', '.join(devices) if devices else '(none)'}")

    # In a hardware check we expect a real camera, not a file/stream.
    if is_file:
        reason = (f"wrong VIDEO_SOURCE — '{video_source}' is a video file/placeholder, "
                  f"not a camera device (set VIDEO_SOURCE=0 or /dev/video0)")
        print(f"CAMERA: FAIL — {reason}")
        return False, reason

    if not devices:
        reason = "camera not found (no /dev/video* device present — check USB cable/power)"
        print(f"CAMERA: FAIL — {reason}")
        return False, reason

    # Permission check for an explicit device path.
    if isinstance(source, str) and source.startswith("/dev/video") and os.path.exists(source):
        if not os.access(source, os.R_OK | os.W_OK):
            reason = f"permission denied opening {source} (add the user to the 'video' group)"
            print(f"CAMERA: FAIL — {reason}")
            return False, reason

    try:
        import cv2  # noqa
    except Exception as e:  # pragma: no cover
        reason = f"OpenCV (cv2) not importable: {e}"
        print(f"CAMERA: FAIL — {reason}")
        return False, reason

    cap = cv2.VideoCapture(source)
    if not cap or not cap.isOpened():
        # Distinguish permission vs generic open failure where we can.
        dev = source if isinstance(source, str) else f"/dev/video{source}"
        if os.path.exists(dev) and not os.access(dev, os.R_OK):
            reason = f"permission denied opening {dev} (add user to 'video' group)"
        else:
            reason = f"cannot open camera source '{source}' (in use, missing, or unsupported)"
        try:
            cap.release()
        except Exception:
            pass
        print(f"CAMERA: FAIL — {reason}")
        return False, reason

    # Warm-up: some webcams return a few empty frames before the sensor settles.
    frame = None
    ok = False
    for _ in range(10):
        ok, frame = cap.read()
        if ok and frame is not None and getattr(frame, "size", 0) > 0:
            break
        time.sleep(0.1)

    if not ok or frame is None or getattr(frame, "size", 0) == 0:
        cap.release()
        reason = "cannot capture frame (camera opened but returned an empty frame)"
        print(f"CAMERA: FAIL — {reason}")
        return False, reason

    h, w = frame.shape[:2]
    dest = out_path or os.path.join(data_dir, "hardware_test", "camera_test.jpg")
    saved_to = dest
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if not cv2.imwrite(dest, frame):
            raise IOError("cv2.imwrite returned False")
    except Exception as e:
        # Fall back to /tmp if the data dir is not writable by this user.
        fallback = os.path.join("/tmp", "codeglofix_hardware_test", "camera_test.jpg")
        try:
            os.makedirs(os.path.dirname(fallback), exist_ok=True)
            cv2.imwrite(fallback, frame)
            saved_to = fallback
            print(f"  (note: could not write {dest}: {e} — saved to {fallback})")
        except Exception as e2:
            cap.release()
            reason = f"captured a frame but could not save it ({e2})"
            print(f"CAMERA: FAIL — {reason}")
            return False, reason

    cap.release()
    print(f"  captured frame    : {w}x{h}")
    print(f"  saved test frame  : {saved_to}")
    print("CAMERA: PASS")
    return True, f"ok ({w}x{h}, saved {saved_to})"


# ──────────────────────────────────────────────────────────────────────────────
# Relay (ESP32) — serial handshake + optional TEST pulse
# ──────────────────────────────────────────────────────────────────────────────
def _open_serial(port: str, baud: int):
    import serial  # pyserial
    return serial.Serial(port=port, baudrate=baud, timeout=1, write_timeout=2)


def _handshake(ser) -> bool:
    """Send PING, expect PONG within 2s (identical to relay_controller)."""
    ser.write(b"PING\n")
    try:
        ser.flush()
    except Exception:
        pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        text = raw.decode("utf-8", errors="ignore").strip()
        if text == "PONG":
            return True
        if text.startswith(("READY", "BOOT", "ARDUINO")):
            continue
    return False


def check_relay_serial(baud: int):
    """Returns (passed: bool, reason: str, working_port: str|None)."""
    configured = os.environ.get("RELAY_PORT", "auto")
    try:
        import serial  # noqa: F401
    except Exception as e:
        reason = f"pyserial not installed ({e}) — install in the venv"
        print(f"RELAY SERIAL: FAIL — {reason}")
        return False, reason, None

    if configured and configured != "auto":
        candidates = [configured]
    else:
        candidates = _candidate_serial_ports()

    print(f"  RELAY_PORT        : {configured}")
    print(f"  RELAY_BAUDRATE    : {baud}")
    print(f"  serial ports found: {', '.join(candidates) if candidates else '(none)'}")

    if not candidates:
        reason = ("no serial ports found (/dev/serial/by-id, /dev/ttyACM*, /dev/ttyUSB*) — "
                  "is the ESP32 relay controller plugged in?")
        print(f"RELAY SERIAL: FAIL — {reason}")
        return False, reason, None

    last_error = "no PONG"
    for port in candidates:
        try:
            if os.path.exists(port) and not os.access(port, os.R_OK | os.W_OK):
                last_error = f"{port}: permission denied (add user to 'dialout' group)"
                continue
            ser = _open_serial(port, baud)
            try:
                time.sleep(0.3)  # let the board settle after port open
                if _handshake(ser):
                    print(f"  handshake         : PONG from {port}")
                    print("RELAY SERIAL: PASS")
                    ser.close()
                    return True, f"ok (PONG on {port} @ {baud})", port
                last_error = f"{port}: no PONG (wrong baud, wrong firmware, or not the relay)"
            finally:
                try:
                    ser.close()
                except Exception:
                    pass
        except Exception as e:
            last_error = f"{port}: {e}"

    print(f"RELAY SERIAL: FAIL — {last_error}")
    return False, last_error, None


def relay_test_pulse(port: str, baud: int, assume_yes: bool):
    """Fire one TEST pulse, but ONLY after explicit confirmation."""
    if not assume_yes:
        try:
            ans = input("This will PULSE the relay (the door may release). Continue? [y/N] ")
        except EOFError:
            ans = ""
        if ans.strip().lower() != "y":
            print("RELAY TEST: SKIPPED (not confirmed)")
            return None, "skipped (not confirmed)"
    try:
        ser = _open_serial(port, baud)
        try:
            time.sleep(0.3)
            ser.write(b"TEST\n")
            ser.flush()
            # Best-effort: many firmwares ack with OK/PONG, but the pulse itself is
            # the real signal. Read briefly so we surface any reply.
            time.sleep(0.5)
            reply = ""
            try:
                raw = ser.readline()
                reply = raw.decode("utf-8", errors="ignore").strip()
            except Exception:
                pass
            print(f"  TEST sent to {port}" + (f" (reply: {reply})" if reply else ""))
            print("RELAY TEST: PASS")
            return True, f"ok (TEST pulsed on {port})"
        finally:
            ser.close()
    except Exception as e:
        print(f"RELAY TEST: FAIL — {e}")
        return False, f"fail ({e})"


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CodeGloFix hardware validation")
    ap.add_argument("--camera", action="store_true", help="check the camera")
    ap.add_argument("--relay", action="store_true", help="check the relay serial handshake")
    ap.add_argument("--all", action="store_true", help="check camera + relay serial")
    ap.add_argument("--relay-test-confirm", action="store_true",
                    help="after the serial check, fire a TEST relay pulse (prompts first)")
    ap.add_argument("--yes-relay-test", action="store_true",
                    help="fire the TEST relay pulse without prompting (implies the pulse)")
    ap.add_argument("--baudrate", type=int,
                    default=int(os.environ.get("RELAY_BAUDRATE", RELAY_BAUDRATE_DEFAULT)),
                    help="serial baud rate (default 115200 / $RELAY_BAUDRATE)")
    ap.add_argument("--data-dir", default=None, help="data dir for the saved test frame")
    ap.add_argument("--out", default=None, help="explicit path for the camera test JPEG")
    ap.add_argument("--report-file", default=None,
                    help="write machine-readable key=value results here (for the installer)")
    args = ap.parse_args(argv)

    do_camera = args.camera or args.all
    do_relay = args.relay or args.all or args.relay_test_confirm or args.yes_relay_test
    do_pulse = args.relay_test_confirm or args.yes_relay_test
    if not (do_camera or do_relay):
        ap.error("nothing to do — pass --camera, --relay, --all, or --relay-test-confirm")

    data_dir = _data_dir(args.data_dir)
    results = {}
    overall_ok = True

    print("=" * 60)
    print("CodeGloFix hardware check")
    print("=" * 60)

    if do_camera:
        print("\n[CAMERA]")
        ok, reason = check_camera(args.out, data_dir)
        results["camera"] = (ok, reason)
        overall_ok = overall_ok and ok

    working_port = None
    if do_relay:
        print("\n[RELAY SERIAL]")
        ok, reason, working_port = check_relay_serial(args.baudrate)
        results["relay_serial"] = (ok, reason)
        overall_ok = overall_ok and ok

    if do_pulse:
        print("\n[RELAY TEST PULSE]")
        if working_port is None:
            print("RELAY TEST: SKIPPED (serial handshake did not pass — not pulsing)")
            results["relay_test"] = (False, "skipped (no serial)")
            overall_ok = False
        else:
            res, reason = relay_test_pulse(working_port, args.baudrate, args.yes_relay_test)
            if res is True:
                results["relay_test"] = (True, reason)
            elif res is None:
                results["relay_test"] = (None, reason)  # skipped, not a failure
            else:
                results["relay_test"] = (False, reason)
                overall_ok = False

    def _tag(ok):
        return "PASS" if ok is True else ("SKIP" if ok is None else "FAIL")

    print("\n" + "-" * 60)
    print("SUMMARY")
    for k, (ok, reason) in results.items():
        print(f"  {k:<13}: {_tag(ok)}  ({reason})")
    print(f"HARDWARE CHECK: {'PASS' if overall_ok else 'FAIL'}")
    print("-" * 60)

    if args.report_file:
        try:
            with open(args.report_file, "w") as f:
                for key in ("camera", "relay_serial", "relay_test"):
                    if key in results:
                        ok, reason = results[key]
                        f.write(f"{key}={_tag(ok)}|{reason}\n")
                    else:
                        f.write(f"{key}=NOTRUN|not run\n")
                f.write(f"overall={'PASS' if overall_ok else 'FAIL'}\n")
        except Exception as e:
            print(f"  (could not write report file {args.report_file}: {e})")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
