"""
auth.py
-------
Server-side session authentication for the CodeGloFix Admin panel.
"""

import os
import hashlib
import hmac
import base64
import json
import time
from typing import Optional

from fastapi import APIRouter, Cookie, Form, HTTPException, status
from fastapi.responses import JSONResponse

# ── Config ────────────────────────────────────────────────────────────────────

ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "")
SESSION_SECRET: str = os.environ.get("SESSION_SECRET", "")
SESSION_TTL: int = int(os.environ.get("SESSION_TTL", str(8 * 3600)))

# Optional: set to "true" when serving behind HTTPS
COOKIE_SECURE: bool = os.environ.get("COOKIE_SECURE", "false").lower() in (
    "1", "true", "yes", "on"
)

COOKIE_NAME = "admin_session"


# ── Startup validation ────────────────────────────────────────────────────────

def validate_auth_config() -> None:
    if not ADMIN_PASSWORD:
        raise RuntimeError(
            "[AUTH] ADMIN_PASSWORD environment variable is not set. "
            "Set it before starting the server."
        )
    if not SESSION_SECRET or len(SESSION_SECRET) < 32:
        raise RuntimeError(
            "[AUTH] SESSION_SECRET environment variable is not set or too short. "
            "Generate one with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )
    print("[AUTH] Auth config OK — admin password and session secret loaded")


# ── Token helpers ─────────────────────────────────────────────────────────────

def _make_token(expires_at: int) -> str:
    payload = json.dumps({"role": "admin", "exp": expires_at}, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(
        SESSION_SECRET.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_b64}.{sig}"


def _verify_token(token: str) -> bool:
    try:
        payload_b64, sig = token.rsplit(".", 1)
    except ValueError:
        return False

    expected_sig = hmac.new(
        SESSION_SECRET.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, sig):
        return False

    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
        return payload.get("role") == "admin" and payload.get("exp", 0) > time.time()
    except Exception:
        return False


# ── Synchronous helper (for page routes / conditional payloads) ───────────────

def is_authenticated(admin_session: Optional[str]) -> bool:
    """Return True if the given cookie value is a valid, unexpired admin token.

    Use this in page routes and in public APIs that should return a fuller
    payload to a logged-in admin but only minimal/safe data to the kiosk.
    """
    return bool(admin_session and _verify_token(admin_session))


# ── FastAPI dependency ────────────────────────────────────────────────────────

async def require_admin(admin_session: Optional[str] = Cookie(default=None)) -> None:
    if not admin_session or not _verify_token(admin_session):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required",
        )


# ── Auth routes ───────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
async def login(password: str = Form(...)):
    # Read live from os.environ so password changes via /api/settings/password
    # take effect immediately — without needing a service restart.
    live_password = os.environ.get("ADMIN_PASSWORD", "")
    if not live_password or not hmac.compare_digest(
        password.encode(), live_password.encode()
    ):
        time.sleep(0.5)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password",
        )

    expires_at = int(time.time()) + SESSION_TTL
    token = _make_token(expires_at)

    response = JSONResponse({"ok": True, "expires_at": expires_at})
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        secure=COOKIE_SECURE,
        max_age=SESSION_TTL,
        path="/",
    )
    print("[AUTH] Admin logged in")
    return response


@router.post("/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(key=COOKIE_NAME, path="/")
    print("[AUTH] Admin logged out")
    return response


@router.get("/check")
async def auth_check(admin_session: Optional[str] = Cookie(default=None)):
    ok = bool(admin_session and _verify_token(admin_session))
    return JSONResponse({"authenticated": ok})