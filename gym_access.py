"""
gym_access.py
-------------
Access decision engine for CodeGloFix Access Control System.

Sits between face recognition and the relay controller.
Called once per recognised face from the detection loop.

Flow:
    recognised name + confidence
        ↓
    emergency_lock check (in-memory flag, fastest path)
        ↓
    gym_db.check_member_access()  (DB lookup)
        ↓
    relay.open_door() if allowed  (non-blocking — runs in background thread)
        ↓
    gym_db.log_access()           (write result)
        ↓
    return AccessResult

Thread safety:
    _emergency_lock is an in-memory bool guarded by _state_lock.
    It is also persisted to gate_config.json and read at startup.
    relay.open_door() is non-blocking — does not stall the detection loop.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import gym_db
from relay_controller import RelayController


# ── Result type ────────────────────────────────────────────────────────────────

@dataclass
class AccessResult:
    name:         str
    decision:     str   # gate state string consumed by face_gate.py / main.py
    reason:       str   # DB reason code
    message:      str   # human-readable status bar message
    allowed:      bool
    door_opened:  bool
    confidence:   float
    days_left:    Optional[int]
    valid_until:  Optional[str]
    member_id:    Optional[int]


# ── GymAccessEngine ────────────────────────────────────────────────────────────

class GymAccessEngine:
    """
    Stateful access engine. One instance lives for the process lifetime.
    Holds a reference to the RelayController and emergency_lock state.
    """

    def __init__(self, relay: RelayController) -> None:
        self.relay = relay
        self._state_lock = threading.Lock()
        # Emergency lock is mirrored here for zero-latency check per frame
        self._emergency_lock: bool = False

    # ── Emergency lock ─────────────────────────────────────────────────────────

    def set_emergency_lock(self, enabled: bool) -> None:
        with self._state_lock:
            self._emergency_lock = enabled
        self.relay.set_emergency_lock(enabled)
        gym_db.set_setting("emergency_lock", "true" if enabled else "false")
        gym_db.log_manual_action(
            "emergency_lock_on" if enabled else "emergency_lock_off",
            note="admin action",
        )
        print(f"[ACCESS] Emergency lock {'ON' if enabled else 'OFF'}")

    def is_emergency_locked(self) -> bool:
        with self._state_lock:
            return self._emergency_lock

    def load_emergency_lock_from_db(self) -> None:
        """Called at startup to restore emergency_lock state from DB."""
        val = gym_db.get_setting("emergency_lock", "false").lower()
        enabled = val in ("true", "1", "yes")
        with self._state_lock:
            self._emergency_lock = enabled
        self.relay.set_emergency_lock(enabled)
        if enabled:
            print("[ACCESS] Emergency lock restored from DB — door will not open automatically")

    # ── Main decision entry point ──────────────────────────────────────────────

    def decide(self, name: str, confidence: float) -> AccessResult:
        """
        Called from detection loop for every face that passes the gate validation.
        Non-blocking — relay.open_door() runs timed-close in a daemon thread.
        """

        # ── 1. Emergency lock (in-memory, fastest) ────────────────────────────
        with self._state_lock:
            emergency = self._emergency_lock

        if emergency:
            result = AccessResult(
                name=name,
                decision="access_denied_emergency_lock",
                reason="denied_emergency_lock",
                message=f"{name} — Emergency lock active",
                allowed=False,
                door_opened=False,
                confidence=confidence,
                days_left=None,
                valid_until=None,
                member_id=None,
            )
            gym_db.log_access(
                name=name,
                decision=result.decision,
                reason=result.reason,
                confidence=confidence,
                door_opened=False,
            )
            return result

        # ── 2. DB access check ────────────────────────────────────────────────
        check = gym_db.check_member_access(name)

        if not check["allowed"]:
            # Map DB reason → gate state
            decision = _reason_to_gate_state(check["reason"])
            result = AccessResult(
                name=name,
                decision=decision,
                reason=check["reason"],
                message=check["message"],
                allowed=False,
                door_opened=False,
                confidence=confidence,
                days_left=check["days_left"],
                valid_until=check["valid_until"],
                member_id=check["member_id"],
            )
            gym_db.log_access(
                name=name,
                decision=decision,
                reason=check["reason"],
                confidence=confidence,
                door_opened=False,
                member_id=check["member_id"],
            )
            return result

        # ── 3. Access granted → open door ─────────────────────────────────────
        door_opened = self.relay.open_door(reason=check["reason"])

        decision = _reason_to_gate_state(check["reason"])
        # The DB message assumes the relay fired ("... Door opened"). If the relay
        # controller is offline the door did NOT physically open, so never tell the
        # member it did — surface a hardware fault instead.
        message = check["message"]
        if not door_opened:
            message = f"{name} - Access granted, DOOR FAULT (relay offline)"
        result = AccessResult(
            name=name,
            decision=decision,
            reason=check["reason"],
            message=message,
            allowed=True,
            door_opened=door_opened,
            confidence=confidence,
            days_left=check["days_left"],
            valid_until=check["valid_until"],
            member_id=check["member_id"],
        )

        gym_db.log_access(
            name=name,
            decision=decision,
            reason=check["reason"],
            confidence=confidence,
            door_opened=door_opened,
            member_id=check["member_id"],
        )

        print(
            f"[ACCESS] ✔ GRANTED: {name} "
            f"reason={check['reason']} "
            f"days_left={check['days_left']} "
            f"door={door_opened}"
        )

        return result

    # ── Manual door control ────────────────────────────────────────────────────

    def manual_open(self, seconds: Optional[float] = None) -> bool:
        # seconds is ignored — Arduino sketch controls relay hold time
        opened = self.relay.manual_open()
        gym_db.log_manual_action(
            "manual_open",
            note="admin opened door via dashboard",
        )
        return opened

    def force_lock(self) -> None:
        self.relay.force_lock()
        gym_db.log_manual_action("manual_lock", note="admin force-locked door")

    def relay_test(self) -> bool:
        result = self.relay.test()
        gym_db.log_manual_action("relay_test", note="admin relay test pulse")
        return result

    def door_status(self) -> dict:
        status = self.relay.status()
        status["emergency_lock"] = self.is_emergency_locked()
        return status


# ── Helper ─────────────────────────────────────────────────────────────────────

def _reason_to_gate_state(reason: str) -> str:
    """Map gym_db reason codes → face_gate.py state names."""
    mapping = {
        "granted_valid":           "access_granted",
        "granted_expiring_soon":   "access_granted_expiring",
        "granted_owner":           "access_granted",   # owner — green, door opens
        "granted_staff":           "access_granted",   # staff — green, door opens
        "denied_expired":          "access_denied_expired",
        "denied_no_payment":       "access_denied_expired",
        "denied_inactive":         "access_denied_inactive",
        "denied_unknown":          "access_denied_unknown",
        "denied_emergency_lock":   "access_denied_emergency_lock",
    }
    return mapping.get(reason, "access_denied_unknown")
