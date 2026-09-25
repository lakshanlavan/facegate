#!/usr/bin/env python3
"""
live_display.py — PyQt6/PyQt6-WebEngine live-display wrapper for CodeGloFix /
DigitFace access control.

Opens the live attendance page (http://127.0.0.1:5000/) full-screen on the
connected door display — preferring a small 7-inch HDMI panel — WITHOUT relying
on Google Chrome. This is the ONLY production live-display method: the Chrome/
Chromium kiosk live view has been removed and there is no Chrome fallback. The
Admin panel is a separate, manual launcher (kiosk/open_admin.sh) and is not
affected by this display.

By default it talks DIRECTLY to the backend on http://127.0.0.1:5000/ (the port
uvicorn binds in start.sh), NOT https://localhost/. This is deliberate: the
direct port needs no nginx and no self-signed TLS certificate, so the display
comes up even when nginx is absent/misconfigured or the cert is not yet trusted
— the most common reason the wrapper used to hang on "Waiting for backend…".
Set LIVE_DISPLAY_URL / LIVE_DISPLAY_HEALTH_URL to https://localhost/ to route
through nginx instead (the self-signed cert for localhost is still accepted).

What it does
------------
  * Waits for the backend health endpoint (http://127.0.0.1:5000/api/health) to
    report healthy BEFORE loading the UI.
  * Accepts the self-signed certificate for localhost hosts ONLY (only relevant
    when overridden to an https://localhost/ URL).
  * Selects the target screen by this priority:
        1. CLI screen index argument, if provided and valid.
        2. Exact 800x480 display.
        3. Exact 1024x600 display.
        4. Smallest connected display by pixel area.
        5. Primary display (fallback).
    (With a single display it simply opens there full-screen.)
  * Opens full-screen on the selected screen. Door-kiosk exit policy: a plain ESC
    is IGNORED by default (a stray key must not close the live display); set
    LIVE_DISPLAY_ALLOW_ESC=1 to re-enable ESC. An emergency exit — Ctrl+Alt+Q —
    is ALWAYS available for on-site operators.
  * Never permanently hides the mouse cursor.
  * Logs the screen list, selected screen, health status, load status,
    reloads and errors.
  * Runs a lightweight watchdog: periodically re-checks /api/health; on repeated
    health failures it reloads the page; after repeated reload failures it exits
    with a non-zero code so its supervisor — the codeglofix-live-display.service
    systemd --user unit (Restart=always) — restarts it. This script itself does
    NOT install or enable that unit; the installer does.

Read-only contract
------------------
  This script does not modify backend routes, templates, the database, admin
  logic, nginx, or any systemd unit. It only *renders* the existing page.

Usage
-----
    python live_display.py            # auto-select target screen
    python live_display.py 1          # force screen index 1

Environment overrides (all optional)
------------------------------------
    LIVE_DISPLAY_URL          page to load           (default http://127.0.0.1:5000/)
    LIVE_DISPLAY_HEALTH_URL   health endpoint        (default http://127.0.0.1:5000/api/health)
    LIVE_DISPLAY_HEALTH_EVERY watchdog period sec    (default 15)
    LIVE_DISPLAY_HEALTH_FAILS consecutive health fails before reload (default 3)
    LIVE_DISPLAY_MAX_RELOADS  reload attempts before exit(non-zero) (default 5)
    LIVE_DISPLAY_ALLOW_ESC    "1" to let plain ESC close the window (default "0";
                              Ctrl+Alt+Q always exits regardless)

Exit codes
----------
    0  clean exit (Ctrl+Alt+Q / allowed-ESC / window closed)
    2  gave up after repeated reload failures (supervisor should restart)
    3  no screens detected / fatal Qt init error
"""

import os
import ssl
import sys
import logging
import urllib.request

# ── Qt platform bootstrap (non-destructive, this process only) ───────────────
# On a Wayland session the "xcb" plugin can fail (needs libxcb-cursor0) and the
# environment may globally force QT_QPA_PLATFORM=xcb. When we are clearly on
# Wayland and the platform is unset or the broken "xcb", prefer "wayland" so the
# wrapper runs out of the box. Any OTHER explicit value (e.g. offscreen) is
# respected. This sets an env var for THIS process only — nothing global.
def _on_wayland() -> bool:
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    return bool(runtime) and os.path.exists(os.path.join(runtime, "wayland-0"))


_qt_platform = os.environ.get("QT_QPA_PLATFORM", "")
if _on_wayland() and _qt_platform in ("", "xcb"):
    os.environ["QT_QPA_PLATFORM"] = "wayland"
# ─────────────────────────────────────────────────────────────────────────────

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QGuiApplication, QKeyEvent
from PyQt6.QtWidgets import QApplication, QLabel, QStackedLayout, QWidget
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEnginePage


# ── Configuration (env-overridable) ──────────────────────────────────────────
# Default to the DIRECT backend port (plain HTTP, no nginx, no self-signed cert).
# Override to https://localhost/ to route through nginx (cert still accepted).
URL = os.environ.get("LIVE_DISPLAY_URL", "http://127.0.0.1:5000/")
HEALTH_URL = os.environ.get("LIVE_DISPLAY_HEALTH_URL", "http://127.0.0.1:5000/api/health")
HEALTH_EVERY_SEC = int(os.environ.get("LIVE_DISPLAY_HEALTH_EVERY", "15"))
HEALTH_FAILS_BEFORE_RELOAD = int(os.environ.get("LIVE_DISPLAY_HEALTH_FAILS", "3"))
MAX_RELOAD_ATTEMPTS = int(os.environ.get("LIVE_DISPLAY_MAX_RELOADS", "5"))
INITIAL_HEALTH_POLL_SEC = 3  # how often to poll while waiting for first-healthy
# The live page embeds an infinite MJPEG stream (<img src=/video_feed_kiosk>),
# so the browser 'load' event (loadFinished) may NEVER fire even though the page
# renders. Reveal the page on load-progress or after a short grace period rather
# than waiting for loadFinished, and drive the watchdog from backend health.
REVEAL_ON_PROGRESS = int(os.environ.get("LIVE_DISPLAY_REVEAL_PROGRESS", "30"))  # percent
REVEAL_GRACE_MS = int(os.environ.get("LIVE_DISPLAY_REVEAL_GRACE_MS", "4000"))

# Door-kiosk exit policy: a plain ESC is IGNORED by default so a stray key press
# cannot close the live display. Set LIVE_DISPLAY_ALLOW_ESC=1 for testing/bench
# use. Ctrl+Alt+Q is an ALWAYS-on emergency exit for on-site operators.
ALLOW_ESC = os.environ.get("LIVE_DISPLAY_ALLOW_ESC", "0").strip().lower() in ("1", "true", "yes", "on")
EXIT_HINT = "(Press ESC to exit)" if ALLOW_ESC else "(kiosk mode — Ctrl+Alt+Q to exit)"

ALLOWED_CERT_HOSTS = {"localhost", "127.0.0.1", "::1", ""}

log = logging.getLogger("live_display")


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
    )
    log.addHandler(handler)
    log.setLevel(logging.INFO)


# ── Health probe (uses an unverified TLS context for the self-signed cert) ────
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def check_health(timeout: float = 4.0) -> bool:
    """Return True when the backend health endpoint reports healthy."""
    try:
        req = urllib.request.Request(HEALTH_URL, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
            if resp.status != 200:
                return False
            body = resp.read(4096).decode("utf-8", "replace")
            # FastAPI emits compact {"ok":true,...}; accept a 200 even if the
            # body shape changes, but prefer the explicit ok:true marker.
            return ('"ok":true' in body.replace(" ", "")) or resp.status == 200
    except Exception as exc:  # noqa: BLE001 — any failure = unhealthy
        log.warning("health check failed: %s", exc)
        return False


# ── Screen selection ─────────────────────────────────────────────────────────
def select_screen(screens, forced_index=None):
    """Return (screen, reason) using the required priority order."""
    primary = QGuiApplication.primaryScreen()

    # 1. Explicit CLI index wins when valid.
    if forced_index is not None:
        if 0 <= forced_index < len(screens):
            return screens[forced_index], f"CLI screen index {forced_index}"
        log.warning("requested screen index %s out of range (0..%d); ignoring",
                    forced_index, len(screens) - 1)

    # 2. Exact 800x480.
    for s in screens:
        g = s.geometry()
        if (g.width(), g.height()) == (800, 480):
            return s, "exact 800x480 (7-inch target)"

    # 3. Exact 1024x600.
    for s in screens:
        g = s.geometry()
        if (g.width(), g.height()) == (1024, 600):
            return s, "exact 1024x600 (small HDMI target)"

    # 4. Smallest by area (only meaningful with >1 screen).
    if len(screens) > 1:
        smallest = min(screens, key=lambda s: s.geometry().width() * s.geometry().height())
        return smallest, "smallest connected display by area"

    # 5. Primary fallback (also the single-display case).
    return primary if primary is not None else screens[0], "primary display (fallback)"


def log_screens(screens) -> None:
    primary = QGuiApplication.primaryScreen()
    log.info("detected %d screen(s):", len(screens))
    for i, s in enumerate(screens):
        g = s.geometry()
        log.info("  [%d] name=%r geometry=%dx%d@(%d,%d) dpr=%s primary=%s",
                 i, s.name(), g.width(), g.height(), g.x(), g.y(),
                 s.devicePixelRatio(), s is primary)


# ── WebEngine page that accepts the localhost self-signed cert ───────────────
class LocalhostPage(QWebEnginePage):
    def certificateError(self, error) -> bool:
        host = ""
        try:
            host = error.url().host()
        except Exception:  # noqa: BLE001 — API shape varies across Qt versions
            pass
        allowed = host in ALLOWED_CERT_HOSTS
        log.warning("certificate error host=%r -> %s", host,
                    "accepted (localhost)" if allowed else "REJECTED")
        if allowed:
            try:
                error.acceptCertificate()  # Qt 6.5+
            except Exception:  # noqa: BLE001 — older Qt relies on the bool return
                pass
        return allowed


# ── Main window ──────────────────────────────────────────────────────────────
class LiveDisplay(QWidget):
    def __init__(self, screen, reason):
        super().__init__()
        self._target_geo = screen.geometry()
        self._health_fail_streak = 0
        self._reload_attempts = 0
        self._page_shown = False
        self._ui_loaded = False

        self.setWindowTitle("CodeGloFix Live Display")
        self.setStyleSheet("background-color: #101820;")

        # Status label shown until the backend is healthy and the page loads.
        self._status = QLabel(
            "CodeGloFix Live Display\n\nWaiting for backend…\n"
            f"{HEALTH_URL}\n\n{EXIT_HINT}"
        )
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet(
            "color:#4FC3F7; font-size:26px; font-weight:bold; background:#101820;")

        self._view = QWebEngineView()
        self._view.setPage(LocalhostPage(self._view))
        self._view.loadFinished.connect(self._on_load_finished)
        self._view.loadStarted.connect(lambda: log.info("web load started"))
        self._view.loadProgress.connect(self._on_load_progress)

        # Stack: status label on top until the page is ready, then the web view.
        self._stack = QStackedLayout()
        self._stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self._stack.addWidget(self._view)
        self._stack.addWidget(self._status)
        self.setLayout(self._stack)
        self._show_status(True)

        self.setGeometry(self._target_geo)

        # Poll for first-healthy, then load. Watchdog starts once loaded.
        self._initial_timer = QTimer(self)
        self._initial_timer.timeout.connect(self._await_healthy_then_load)
        self._initial_timer.start(INITIAL_HEALTH_POLL_SEC * 1000)
        QTimer.singleShot(0, self._await_healthy_then_load)  # try immediately

    # -- status overlay helpers ------------------------------------------------
    def _show_status(self, visible: bool):
        self._status.setVisible(visible)
        if visible:
            self._stack.setCurrentWidget(self._status)
        else:
            self._stack.setCurrentWidget(self._view)

    def _set_status_text(self, text: str):
        self._status.setText(text)

    # -- initial load ----------------------------------------------------------
    def _await_healthy_then_load(self):
        if self._ui_loaded:
            return
        if check_health():
            log.info("backend healthy — loading %s", URL)
            self._initial_timer.stop()
            self._ui_loaded = True
            self._view.setUrl(QUrl(URL))
            # loadFinished may never fire for the streaming live page, so reveal
            # the page after a short grace period regardless.
            QTimer.singleShot(REVEAL_GRACE_MS, self._reveal_page)
            # Start the periodic watchdog now that we have attempted a load.
            self._watchdog = QTimer(self)
            self._watchdog.timeout.connect(self._watchdog_tick)
            self._watchdog.start(HEALTH_EVERY_SEC * 1000)
        else:
            log.info("waiting for backend health at %s …", HEALTH_URL)
            self._set_status_text(
                "CodeGloFix Live Display\n\nWaiting for backend…\n"
                f"{HEALTH_URL}\n\n{EXIT_HINT}")

    # -- reveal / progress -----------------------------------------------------
    def _reveal_page(self):
        """Show the web view (hide the waiting overlay). Idempotent."""
        if not self._page_shown:
            self._page_shown = True
            log.info("page revealed (streaming live page keeps 'load' pending — "
                     "this is expected)")
        self._show_status(False)

    def _on_load_progress(self, pct: int):
        if pct >= REVEAL_ON_PROGRESS and not self._page_shown:
            log.info("load progress %d%% — revealing page", pct)
            self._reveal_page()

    # -- page load result ------------------------------------------------------
    def _on_load_finished(self, ok: bool):
        # NOTE: for the live page this may never fire (perpetual MJPEG stream).
        # We therefore do NOT gate the display on it — see _reveal_page().
        if ok:
            log.info("page load finished OK: %s", URL)
            self._reveal_page()
        else:
            log.error("page load FAILED: %s (watchdog will retry via health)", URL)
            self._page_shown = False
            self._set_status_text(
                "CodeGloFix Live Display\n\nPage failed to load — retrying…\n"
                f"{URL}\n\n{EXIT_HINT}")
            self._show_status(True)

    # -- watchdog (driven by BACKEND health, not the browser load event) -------
    def _watchdog_tick(self):
        if check_health():
            if self._health_fail_streak:
                log.info("backend healthy again (streak reset)")
            self._health_fail_streak = 0
            if not self._page_shown:
                # Healthy but overlay still up (load event never came) → reveal.
                self._reveal_page()
            return

        self._health_fail_streak += 1
        log.warning("watchdog: backend unhealthy (streak=%d, reloads=%d)",
                    self._health_fail_streak, self._reload_attempts)

        if self._health_fail_streak >= HEALTH_FAILS_BEFORE_RELOAD:
            self._health_fail_streak = 0
            self._reload_attempts += 1
            if self._reload_attempts > MAX_RELOAD_ATTEMPTS:
                log.error("watchdog: exceeded %d reload attempts — exiting non-zero "
                          "so a supervisor can restart", MAX_RELOAD_ATTEMPTS)
                QApplication.instance().exit(2)
                return
            log.warning("watchdog: reloading page (attempt %d/%d)",
                        self._reload_attempts, MAX_RELOAD_ATTEMPTS)
            self._page_shown = False
            self._set_status_text(
                "CodeGloFix Live Display\n\nReconnecting…\n"
                f"{URL}\n\n{EXIT_HINT}")
            self._show_status(True)
            self._view.setUrl(QUrl(URL))

    # -- input -----------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent):
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        # Emergency exit is ALWAYS available for on-site operators.
        if event.key() == Qt.Key.Key_Q and ctrl and alt:
            log.info("Ctrl+Alt+Q pressed — emergency exit")
            self.close()
            return
        # Plain ESC closes ONLY when explicitly allowed. Default door-kiosk policy
        # ignores it so a stray key press cannot take the live display down.
        if event.key() == Qt.Key.Key_Escape:
            if ALLOW_ESC:
                log.info("ESC pressed — exiting (LIVE_DISPLAY_ALLOW_ESC=1)")
                self.close()
            else:
                log.info("ESC ignored (kiosk mode; set LIVE_DISPLAY_ALLOW_ESC=1 to "
                         "allow, or use Ctrl+Alt+Q)")
            return
        super().keyPressEvent(event)


def main() -> int:
    _setup_logging()

    forced_index = None
    if len(sys.argv) > 1:
        try:
            forced_index = int(sys.argv[1])
        except ValueError:
            log.warning("ignoring non-integer screen index argument: %r", sys.argv[1])

    log.info("starting live_display (url=%s health=%s platform=%s)",
             URL, HEALTH_URL, os.environ.get("QT_QPA_PLATFORM", "<default>"))

    app = QApplication(sys.argv)

    screens = QGuiApplication.screens()
    if not screens:
        log.error("no screens detected by Qt — cannot open display")
        return 3

    log_screens(screens)
    screen, reason = select_screen(screens, forced_index)
    geo = screen.geometry()
    log.info("selected screen: name=%r %dx%d — %s",
             screen.name(), geo.width(), geo.height(), reason)

    window = LiveDisplay(screen, reason)
    window.show()
    handle = window.windowHandle()
    if handle is not None:
        handle.setScreen(screen)
    window.setGeometry(geo)
    window.showFullScreen()

    rc = app.exec()
    log.info("event loop finished (rc=%d)", rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
