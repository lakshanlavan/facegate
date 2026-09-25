"""
relay_controller.py
-------------------
Serial relay controller for CodeGloFix Access Control System.

Behaviour:
    Face recognised / admin clicks "Open Door"
        → sends "OPEN\n" over serial to the relay controller
        → controller firmware fires the relay (its own timing — Python doesn't care)

    Admin clicks "Lock" or enables emergency lock
        → sends "LOCK\n" to controller (optional — use if your firmware supports it)

Serial protocol (controller firmware must implement):
    OPEN   → controller pulses the relay for however long configured in firmware
    LOCK   → controller turns relay off immediately (optional but recommended)
    PING   → controller replies "PONG\n"    (used at startup to verify connection)
    TEST   → controller gives a 1-second relay pulse (used by admin relay test button)

All timing (how long the door stays open) lives in the controller firmware.
Python just sends a one-shot "OPEN" signal — no threads, no timers, no timed-close.

Config keys used from gate_config.json:
    relay_port       : "/dev/ttyUSB0", "/dev/ttyACM0", or "auto"
    relay_baudrate   : must match the baud rate in your controller firmware (default 115200)
    relay_auto_connect : true/false — connect at startup or lazily on first use
    emergency_lock   : persisted bool — restored at startup

Removed (no longer needed):
    relay_mode, relay_channel, relay_active_mode,
    door_open_seconds, manual_open_seconds
"""

from __future__ import annotations

import glob
import threading
import time
from datetime import datetime
from typing import Optional


class RelayController:
    """
    Thread-safe serial relay controller.

    send_open()   → sends OPEN\\n to the controller (face detected or admin button)
    send_lock()   → sends LOCK\\n to the controller (emergency lock / force lock)
    test()        → sends TEST\\n to the controller (admin relay test)
    status()      → returns connection/state dict for the dashboard
    """

    def __init__(
        self,
        port: str = "auto",
        baudrate: int = 115200,
        auto_connect: bool = True,
    ) -> None:
        self.port = (port or "auto").strip()
        self.baudrate = int(baudrate)
        self.auto_connect = bool(auto_connect)

        self._lock = threading.Lock()
        self._io_lock = threading.Lock()   # serialises serial write/read
        self._serial = None

        self._emergency_lock: bool = False
        self._last_action: str = "system_start"
        self._last_open_at_iso: Optional[str] = None
        self._last_error: str = ""
        self._connected: bool = False

        print(
            f"[RELAY] Serial relay mode — port={self.port} baud={self.baudrate}"
        )

        if self.auto_connect:
            try:
                self._get_serial()
                print("[RELAY] Relay controller connected and ready")
            except Exception as e:
                with self._lock:
                    self._last_error = str(e)
                print(f"[RELAY] Controller startup connect failed: {e}")

        # Production recovery: keep checking the relay controller in the background.
        # If USB is unplugged, this marks the relay disconnected and closes
        # the stale handle. When USB is plugged back, _get_serial() will
        # auto-discover and reconnect it.
        self._start_keepalive()

    # ── Class method constructor ───────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: dict) -> "RelayController":
        """
        Construct from gate_config.json with optional env overrides.

        Supported env overrides:
            RELAY_PORT=auto
            RELAY_BAUDRATE=115200
        """
        import os
        port     = os.environ.get("RELAY_PORT",     config.get("relay_port", "auto"))
        baudrate = os.environ.get("RELAY_BAUDRATE", config.get("relay_baudrate", 115200))
        return cls(
            port=port,
            baudrate=int(baudrate),
            auto_connect=bool(config.get("relay_auto_connect", True)),
        )

    # ── Emergency lock state ───────────────────────────────────────────────────

    def set_emergency_lock(self, enabled: bool) -> None:
        with self._lock:
            self._emergency_lock = bool(enabled)
            self._last_action = "emergency_lock_on" if enabled else "emergency_lock_off"
        if enabled:
            ok = self._send_command("LOCK")
            if ok:
                print("[RELAY] Emergency lock ON — LOCK sent to controller")
            else:
                print("[RELAY] Emergency lock ON — controller not connected; relay managed in software only")
        else:
            print("[RELAY] Emergency lock OFF")

    def is_emergency_locked(self) -> bool:
        with self._lock:
            return self._emergency_lock

    # ── Main signal methods ────────────────────────────────────────────────────

    def open_door(self, reason: str = "access_granted") -> bool:
        """
        Called when a face is recognised and access is granted.
        Sends OPEN to relay controller. Controller handles relay timing internally.
        Blocked if emergency lock is active.
        """
        with self._lock:
            if self._emergency_lock:
                self._last_action = "blocked_emergency_lock"
                print(f"[RELAY] BLOCKED — emergency lock active. reason={reason}")
                return False

        ok = self._send_command("OPEN")
        with self._lock:
            self._last_action = reason if ok else "open_failed"
            if ok:
                self._last_open_at_iso = datetime.now().isoformat(timespec="seconds")
                self._last_error = ""
        if ok:
            print(f"[RELAY] ✔ OPEN sent to controller — reason={reason}")
        return ok

    def manual_open(self, seconds: Optional[float] = None) -> bool:
        """
        Admin dashboard "Open Door" button.
        Sends OPEN to relay controller. The 'seconds' parameter is ignored —
        controller firmware controls the relay hold time.
        Emergency lock does NOT block this (admin override).
        """
        ok = self._send_command("OPEN")
        with self._lock:
            self._last_action = "manual_open" if ok else "manual_open_failed"
            if ok:
                self._last_open_at_iso = datetime.now().isoformat(timespec="seconds")
                self._last_error = ""
        print(f"[RELAY] Manual open — OPEN {'sent' if ok else 'FAILED'}")
        return ok

    def force_lock(self) -> None:
        """Admin 'Lock' button — sends LOCK to relay controller."""
        ok = self._send_command("LOCK")
        with self._lock:
            self._last_action = "force_lock" if ok else "force_lock_failed"
        print(f"[RELAY] Force lock — LOCK {'sent' if ok else 'FAILED'}")

    def test(self) -> bool:
        """Admin relay test — sends TEST to relay controller."""
        ok = self._send_command("TEST")
        with self._lock:
            self._last_action = "relay_test" if ok else "relay_test_failed"
        print(f"[RELAY] Relay test — TEST {'sent' if ok else 'FAILED'}")
        return ok

    def status(self) -> dict:
        with self._lock:
            return {
                "connected":      self._connected,
                "port":           self.port,
                "baudrate":       self.baudrate,
                "last_action":    self._last_action,
                "last_open_at":   self._last_open_at_iso,
                "last_error":     self._last_error,
                "emergency_lock": self._emergency_lock,
            }

    def _start_keepalive(self) -> None:
        """
        Background serial recovery loop.

        Required controller protocol:
            PING -> PONG

        Behaviour:
            - If the controller is unplugged, mark disconnected and close serial.
            - If the controller is plugged back, auto-discovery reconnects it.
            - No door-control logic is changed.
        """
        def _loop() -> None:
            while True:
                time.sleep(30)
                try:
                    with self._io_lock:
                        ser = self._get_serial()
                        ser.write(b"PING\n")
                        try:
                            ser.flush()
                        except Exception:
                            pass

                        reply = ser.readline().decode("utf-8", errors="ignore").strip()
                        if reply != "PONG":
                            raise RuntimeError(f"Relay controller keepalive failed: {reply!r}")

                    with self._lock:
                        self._connected = True
                        self._last_error = ""

                except Exception as e:
                    msg = f"{type(e).__name__}: {e}"
                    with self._lock:
                        self._connected = False
                        self._last_error = msg
                    self._close_serial()
                    print(f"[RELAY] Keepalive failed — {msg}")

        t = threading.Thread(
            target=_loop,
            name="relay-keepalive",
            daemon=True,
        )
        t.start()
        print("[RELAY] Relay keepalive started")

    # ── Serial helpers ─────────────────────────────────────────────────────────

    def _send_command(self, command: str) -> bool:
        """
        Write one command line to the relay controller.
        Does NOT wait for a response (fire-and-forget).
        Protected by _io_lock so concurrent callers cannot interleave.
        """
        with self._io_lock:
            try:
                ser = self._get_serial()
                line = (command.strip() + "\n").encode("utf-8")
                ser.write(line)
                try:
                    ser.flush()
                except Exception:
                    pass
                print(f"[RELAY] → {command}")
                return True
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                print(f"[RELAY] ERROR sending '{command}': {msg}")
                with self._lock:
                    self._last_error = msg
                    self._connected = False
                self._close_serial()
                return False

    def _get_serial(self):
        """
        Lazy serial connection with auto-port discovery.
        Raises RuntimeError if no relay controller is found.
        """
        if self._serial is not None:
            try:
                if getattr(self._serial, "is_open", True):
                    return self._serial
            except Exception:
                pass
            self._serial = None

        try:
            import serial
        except ImportError:
            raise RuntimeError(
                "pyserial not installed. Run: pip install pyserial"
            )

        candidates = (
            self._candidate_ports()
            if self.port.lower() in ("auto", "", "none")
            else [self.port]
        )

        last_error = ""
        for candidate in candidates:
            ser = None
            try:
                ser = serial.Serial(
                    port=candidate,
                    baudrate=self.baudrate,
                    timeout=1.5,
                    write_timeout=1.0,
                )
                # Controller resets on serial connect — give it time to boot
                time.sleep(2.0)
                try:
                    ser.reset_input_buffer()
                except Exception:
                    pass

                # Verify this is our relay controller
                if self._handshake(ser):
                    self._serial = ser
                    with self._lock:
                        self.port = candidate
                        self._connected = True
                        self._last_error = ""
                    print(f"[RELAY] Controller connected: {candidate} baud={self.baudrate}")
                    return self._serial
                else:
                    try:
                        ser.close()
                    except Exception:
                        pass
                    last_error = f"{candidate}: PING/PONG handshake failed"

            except Exception as e:
                last_error = f"{candidate}: {e}"
                try:
                    if ser is not None:
                        ser.close()
                except Exception:
                    pass

        raise RuntimeError(
            f"Cannot connect to relay controller. Tried: {candidates}. Last error: {last_error}"
        )

    def _handshake(self, ser) -> bool:
        """
        Send PING and expect PONG within 2 seconds.
        Your controller firmware must reply with "PONG" when it receives "PING".
        """
        try:
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
                    print("[RELAY] Controller handshake OK (PONG received)")
                    return True
                # Ignore controller boot messages
                if text.startswith(("READY", "BOOT", "ARDUINO")):
                    continue
            return False
        except Exception as e:
            print(f"[RELAY] Handshake error: {e}")
            return False

    def _close_serial(self) -> None:
        try:
            if self._serial is not None:
                self._serial.close()
        except Exception:
            pass
        finally:
            self._serial = None

    @staticmethod
    def _candidate_ports() -> list:
        """Auto-discover USB serial ports (ttyUSB* and ttyACM* cover most relay controllers)."""
        ports = []
        for pattern in (
            "/dev/serial/by-id/*",
            "/dev/ttyACM*",   # USB-CDC serial (common on many relay controllers)
            "/dev/ttyUSB*",   # CH340/CP2102 USB-serial adapters
        ):
            ports.extend(glob.glob(pattern))
        seen, unique = set(), []
        for p in ports:
            if p not in seen:
                unique.append(p)
                seen.add(p)
        return unique
