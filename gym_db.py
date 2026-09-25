"""
gym_db.py
---------
SQLite database layer for CodeGloFix Access Control System.

Tables:
    members          — registered gym members  (now has `role` column)
    membership_plans — plan definitions
    payments         — payment records per member
    access_logs      — every access attempt
    system_settings  — key/value config

Changes from v1:
    + members.role column: 'member' | 'owner' | 'staff'
    + owner/staff bypass payment check → door always opens for them
    + rename_member() also cascades to access_logs.member_name (was already there)
    + member_name_check() returns 'active', 'inactive', 'not_found'
      (no change — duplicate guard uses this in main.py enrol/confirm)
    + get_member_by_name() includes role field
    + list_members() includes role field
    + set_member_role() / get_member_role() added
    + check_member_access() — role bypass logic added
"""

import os
import sqlite3
import threading
import queue
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Module-level constants ─────────────────────────────────────────────────────

# Production deployments set CODEGLOFIX_DB_PATH (e.g. /var/lib/codeglofix/gym.db)
# so the database lives in a persistent system location, separate from the
# read-only app code in /opt/codeglofix/app. With the env var unset (dev PC) the
# DB stays next to the source as before — no behaviour change. The legacy
# DIGITGYM_DB_PATH name is still honoured as an internal fallback so older env
# files keep working during migration.
DB_PATH = Path(
    os.environ.get("CODEGLOFIX_DB_PATH")
    or os.environ.get("DIGITGYM_DB_PATH")
    or str(Path(__file__).parent / "gym.db")
)


# Valid roles
ROLE_MEMBER = "member"
ROLE_OWNER  = "owner"
ROLE_STAFF  = "staff"
PRIVILEGED_ROLES = {ROLE_OWNER, ROLE_STAFF}  # bypass payment check

# ── Module-level state ─────────────────────────────────────────────────────────

_lock = threading.Lock()
_log_queue: queue.Queue = queue.Queue(maxsize=10000)
_log_writer_started = False
_log_writer_lock = threading.Lock()



# ── Connection helper ──────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-4000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn




# ── Async access-log writer ───────────────────────────────────────────────────

def _ensure_log_writer_started() -> None:
    """Start a background writer so detection never waits for log INSERT commits."""
    global _log_writer_started
    with _log_writer_lock:
        if _log_writer_started:
            return
        t = threading.Thread(target=_log_writer_loop, name="gym-db-log-writer", daemon=True)
        t.start()
        _log_writer_started = True
        print("[GYM_DB] Async access-log writer started")


def _log_writer_loop() -> None:
    while True:
        payload = _log_queue.get()
        try:
            _insert_access_log_sync(**payload)
        except Exception as e:
            print(f"[GYM_DB] Async log write failed: {e}")
        finally:
            _log_queue.task_done()


def _insert_access_log_sync(
    name: str,
    decision: str,
    reason: str,
    confidence: float,
    door_opened: bool,
    member_id: Optional[int] = None,
    ts: Optional[str] = None,
) -> Optional[str]:
    """Actual SQLite INSERT executed by background writer."""
    now_str = ts or datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn = _get_conn()
        try:
            if member_id is None:
                row = conn.execute(
                    "SELECT id FROM members WHERE name=? AND is_active=1", (name,)
                ).fetchone()
                if row:
                    member_id = row["id"]

            conn.execute(
                "INSERT INTO access_logs "
                "(member_id, member_name, ts, decision, reason, door_opened) "
                "VALUES (?,?,?,?,?,?)",
                (member_id, name, now_str, decision, reason,
                 1 if door_opened else 0),
            )
            conn.commit()
            print(
                f"[GYM_DB] ACCESS {decision.upper()}: {name} "
                f"reason={reason} door={door_opened}"
            )
            return "logged"
        finally:
            conn.close()

# ── DB initialisation ──────────────────────────────────────────────────────────

def init_db() -> None:
    """
    Create all tables. Safe to call on every startup — all CREATE IF NOT EXISTS.
    Also runs _migrate() to add the `role` column to existing databases.
    Seeds default membership plans and settings on first run.
    """
    # Guarantee the data directory exists before SQLite opens the file. In
    # production DB_PATH is /var/lib/codeglofix/gym.db (the installer creates the
    # dir); this mkdir makes a fresh-PC first start self-sufficient even if the
    # dir is somehow absent, so gym.db (+ -wal/-shm) auto-create on first run.
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[GYM_DB] WARNING: could not create data dir {DB_PATH.parent}: {e}")
    with _lock:
        conn = _get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS members (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_code  TEXT    NOT NULL UNIQUE,
                    name         TEXT    NOT NULL UNIQUE,
                    phone        TEXT,
                    email        TEXT,
                    joined_at    TEXT    NOT NULL,
                    is_active    INTEGER NOT NULL DEFAULT 1,
                    photo_note   TEXT,
                    role         TEXT    NOT NULL DEFAULT 'member'
                );

                CREATE TABLE IF NOT EXISTS membership_plans (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_name     TEXT    NOT NULL UNIQUE,
                    duration_days INTEGER NOT NULL,
                    price         REAL    NOT NULL,
                    description   TEXT,
                    is_active     INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS payments (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_id      INTEGER NOT NULL,
                    plan_id        INTEGER,
                    amount         REAL    NOT NULL,
                    payment_date   TEXT    NOT NULL,
                    valid_from     TEXT    NOT NULL,
                    valid_until    TEXT    NOT NULL,
                    payment_method TEXT    NOT NULL DEFAULT 'cash',
                    received_by    TEXT,
                    note           TEXT,
                    FOREIGN KEY(member_id) REFERENCES members(id),
                    FOREIGN KEY(plan_id)   REFERENCES membership_plans(id)
                );

                CREATE TABLE IF NOT EXISTS access_logs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_id   INTEGER,
                    member_name TEXT    NOT NULL,
                    ts          TEXT    NOT NULL,
                    decision    TEXT    NOT NULL,
                    reason      TEXT    NOT NULL,
                    door_opened INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS system_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_payments_member
                    ON payments(member_id);
                CREATE INDEX IF NOT EXISTS idx_payments_valid_until
                    ON payments(valid_until);
                CREATE INDEX IF NOT EXISTS idx_access_logs_ts
                    ON access_logs(ts);
                CREATE INDEX IF NOT EXISTS idx_access_logs_member
                    ON access_logs(member_name);
                CREATE INDEX IF NOT EXISTS idx_members_active_name
                    ON members(is_active, name);
                CREATE INDEX IF NOT EXISTS idx_payments_member_valid_until
                    ON payments(member_id, valid_until DESC);
                CREATE INDEX IF NOT EXISTS idx_payments_payment_date
                    ON payments(payment_date);
                CREATE INDEX IF NOT EXISTS idx_access_logs_member_ts
                    ON access_logs(member_id, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_access_logs_name_ts
                    ON access_logs(member_name, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_access_logs_ts_decision
                    ON access_logs(ts, decision);
            """)
            conn.commit()
            _migrate(conn)
            _ensure_access_log_indexes(conn)
            _seed_plans(conn)
            _seed_settings(conn)
            conn.commit()
        finally:
            conn.close()

    _ensure_log_writer_started()
    print(f"[GYM_DB] Database ready: {DB_PATH}")


def _migrate(conn: sqlite3.Connection) -> None:
    """
    Add `role` column to existing members table if it does not exist yet.
    Safe to call every startup — checks column existence first.
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(members)").fetchall()]
    if "role" not in cols:
        conn.execute("ALTER TABLE members ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
        conn.commit()
        print("[GYM_DB] Migration: added members.role column")


def _seed_plans(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM membership_plans").fetchone()[0]
    if count == 0:
        default_plans = [
            ("Monthly",   30,  5000.0,  "30-day membership"),
            ("Quarterly", 90,  13500.0, "90-day membership"),
            ("Annual",    365, 50000.0, "365-day membership"),
        ]
        conn.executemany(
            "INSERT INTO membership_plans (plan_name, duration_days, price, description) "
            "VALUES (?,?,?,?)",
            default_plans,
        )
        print("[GYM_DB] Default membership plans seeded")


def _seed_settings(conn: sqlite3.Connection) -> None:
    defaults = {
        "door_open_seconds":   "3",
        "expiry_warning_days": "7",
        "allow_expiring":      "true",
        "emergency_lock":      "false",
    }
    for key, val in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?,?)",
            (key, val),
        )


# ── Members ────────────────────────────────────────────────────────────────────

def add_member(
    name: str,
    phone: str = "",
    email: str = "",
    role: str = ROLE_MEMBER,
    member_code: Optional[str] = None,
) -> int:
    """
    Add a new member. Returns new member ID.
    Raises ValueError if name already exists (active or inactive).
    Duplicate-name guard: checks both active AND inactive records.

    member_code (the user-facing "Member ID"):
        - If provided non-empty (after trim), it is used as-is.
        - If blank/None, an auto GYM<timestamp> code is generated (legacy behaviour).
        - Uniqueness is enforced by the members.member_code UNIQUE constraint;
          a duplicate raises ValueError with a clear message.

    Reactivation rule:
        If the name exists but is inactive, the record is reactivated and its
        EXISTING member_code is preserved — a member_code passed here is ignored
        for reactivation (admin can change it later via update_member_details).
    """
    if role not in (ROLE_MEMBER, ROLE_OWNER, ROLE_STAFF):
        raise ValueError(f"Invalid role '{role}'. Must be member / owner / staff")

    now_str = datetime.now().isoformat(timespec="seconds")

    # Blank / whitespace-only → fall back to legacy auto-generated code.
    code = (member_code or "").strip()
    if not code:
        code = f"GYM{datetime.now().strftime('%Y%m%d%H%M%S')}"

    with _lock:
        conn = _get_conn()
        try:
            # Check both active AND inactive — prevent re-use of removed names
            existing = conn.execute(
                "SELECT id, is_active FROM members WHERE name=?", (name,)
            ).fetchone()

            if existing:
                if existing["is_active"]:
                    raise ValueError(f"'{name}' is already an active member")
                else:
                    # Reactivate soft-deleted record rather than create duplicate.
                    # Keep the existing member_code — do NOT overwrite it here.
                    conn.execute(
                        "UPDATE members SET is_active=1, role=?, joined_at=? WHERE id=?",
                        (role, now_str, existing["id"]),
                    )
                    conn.commit()
                    print(f"[GYM_DB] Member reactivated: {name} (id={existing['id']}) role={role}")
                    return existing["id"]

            try:
                cur = conn.execute(
                    "INSERT INTO members (member_code, name, phone, email, joined_at, role) "
                    "VALUES (?,?,?,?,?,?)",
                    (code, name, phone or "", email or "", now_str, role),
                )
                conn.commit()
            except sqlite3.IntegrityError as e:
                # Name is pre-checked above, so a UNIQUE violation here is the
                # member_code (the user-facing Member ID) already being in use.
                raise ValueError(f"Member ID '{code}' is already in use") from e

            member_id = cur.lastrowid
            print(f"[GYM_DB] Member added: {name} (id={member_id}) role={role} code={code}")
            return member_id
        finally:
            conn.close()


def remove_member(name: str) -> bool:
    """Soft-delete — sets is_active=0. Returns True if found."""
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM members WHERE name=? AND is_active=1", (name,)
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE members SET is_active=0 WHERE id=?", (row["id"],)
            )
            conn.commit()
            print(f"[GYM_DB] Member deactivated: {name}")
            return True
        finally:
            conn.close()


def rename_member(old_name: str, new_name: str) -> bool:
    """
    Rename a member in members table AND cascade to access_logs.member_name.
    Also renames the in-memory cooldown entry so the gate doesn't lose state.

    Returns True if found and renamed.
    Raises ValueError if new_name is already taken by an active member.
    """
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM members WHERE name=? AND is_active=1", (old_name,)
            ).fetchone()
            if not row:
                return False

            # Duplicate check — active members only
            conflict = conn.execute(
                "SELECT id FROM members WHERE name=? AND is_active=1", (new_name,)
            ).fetchone()
            if conflict:
                raise ValueError(f"'{new_name}' is already an active member")

            member_id = row["id"]

            # Rename in members table
            conn.execute(
                "UPDATE members SET name=? WHERE id=?", (new_name, member_id)
            )
            # Cascade to access_logs
            conn.execute(
                "UPDATE access_logs SET member_name=? WHERE member_id=?",
                (new_name, member_id),
            )
            conn.commit()
            print(f"[GYM_DB] Member renamed: '{old_name}' → '{new_name}'")
        finally:
            conn.close()

    return True


def set_member_role(name: str, role: str) -> bool:
    """
    Set the role for an active member.
    role must be 'member', 'owner', or 'staff'.
    Returns True if found and updated, False if member not found.
    """
    if role not in (ROLE_MEMBER, ROLE_OWNER, ROLE_STAFF):
        raise ValueError(f"Invalid role '{role}'. Must be: member / owner / staff")

    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM members WHERE name=? AND is_active=1", (name,)
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE members SET role=? WHERE id=?", (role, row["id"])
            )
            conn.commit()
            print(f"[GYM_DB] Role set: '{name}' → {role}")
            return True
        finally:
            conn.close()


def get_member_role(name: str) -> Optional[str]:
    """Return the role string for a member, or None if not found."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT role FROM members WHERE name=? AND is_active=1", (name,)
        ).fetchone()
        return row["role"] if row else None
    finally:
        conn.close()


def list_members() -> List[Dict]:
    """
    Return all active members with current payment status and role.

    Production optimized: one SQL query using latest-payment/latest-access CTEs.
    This avoids the old N+1 pattern that ran two extra SELECTs per member.
    """
    today_obj = date.today()
    warning_days = _get_warning_days()
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            WITH latest_payment AS (
                SELECT * FROM (
                    SELECT p.*, ROW_NUMBER() OVER (
                        PARTITION BY p.member_id
                        ORDER BY p.valid_until DESC, p.id DESC
                    ) AS rn
                    FROM payments p
                ) WHERE rn = 1
            ), latest_access AS (
                SELECT * FROM (
                    SELECT al.member_id, al.ts, al.decision,
                           ROW_NUMBER() OVER (
                               PARTITION BY al.member_id
                               ORDER BY al.ts DESC, al.id DESC
                           ) AS rn
                    FROM access_logs al
                    WHERE al.member_id IS NOT NULL
                ) WHERE rn = 1
            )
            SELECT m.id, m.member_code, m.name, m.phone, m.email, m.joined_at, m.role,
                   lp.valid_until, lp.amount, lp.plan_id,
                   la.ts AS last_ts, la.decision AS last_decision
            FROM members m
            LEFT JOIN latest_payment lp ON lp.member_id = m.id
            LEFT JOIN latest_access la ON la.member_id = m.id
            WHERE m.is_active = 1
            ORDER BY m.name
            """
        ).fetchall()

        result = []
        for r in rows:
            role = r["role"] or ROLE_MEMBER
            valid_until = r["valid_until"]
            days_left = None
            payment_status = "no_payment"

            if role in PRIVILEGED_ROLES:
                payment_status = role
                valid_until = None
            elif valid_until:
                delta = (date.fromisoformat(valid_until) - today_obj).days
                days_left = delta
                if delta < 0:
                    payment_status = "expired"
                elif delta == 0:
                    payment_status = "expires_today"
                elif delta <= warning_days:
                    payment_status = "expiring_soon"
                else:
                    payment_status = "active"

            result.append({
                "id": r["id"],
                "member_code": r["member_code"],
                "name": r["name"],
                "phone": r["phone"],
                "email": r["email"],
                "joined_at": r["joined_at"],
                "role": role,
                "payment_status": payment_status,
                "valid_until": valid_until,
                "days_left": days_left,
                "last_access": (
                    {"ts": r["last_ts"], "decision": r["last_decision"]}
                    if r["last_ts"] else None
                ),
            })

        return result
    finally:
        conn.close()


def count_active_members() -> int:
    """Fast active-member count without loading the full member list."""
    conn = _get_conn()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM members WHERE is_active=1").fetchone()[0])
    finally:
        conn.close()


def get_member_detail(member_id: int) -> Optional[Dict]:
    """Return one active member detail row without running the full list_members() query."""
    conn = _get_conn()
    try:
        row = conn.execute(
            """
            WITH latest_payment AS (
                SELECT p.*
                FROM payments p
                WHERE p.member_id = ?
                ORDER BY p.valid_until DESC
                LIMIT 1
            ), latest_access AS (
                SELECT ts, decision
                FROM access_logs
                WHERE member_id = ?
                ORDER BY ts DESC
                LIMIT 1
            )
            SELECT m.id, m.member_code, m.name, m.phone, m.email, m.joined_at, m.role,
                   lp.valid_until, lp.amount, lp.plan_id,
                   la.ts AS last_ts, la.decision AS last_decision
            FROM members m
            LEFT JOIN latest_payment lp ON 1=1
            LEFT JOIN latest_access la ON 1=1
            WHERE m.id=? AND m.is_active=1
            """,
            (member_id, member_id, member_id),
        ).fetchone()
        if not row:
            return None

        d = dict(row)
        role = d.get("role") or ROLE_MEMBER
        valid_until = d.get("valid_until")
        days_left = None
        payment_status = "no_payment"

        if role in PRIVILEGED_ROLES:
            payment_status = role
            valid_until = None
        elif valid_until:
            delta = (date.fromisoformat(valid_until) - date.today()).days
            days_left = delta
            if delta < 0:
                payment_status = "expired"
            elif delta == 0:
                payment_status = "expires_today"
            elif delta <= _get_warning_days():
                payment_status = "expiring_soon"
            else:
                payment_status = "active"

        return {
            "id": d["id"],
            "member_code": d["member_code"],
            "name": d["name"],
            "phone": d["phone"],
            "email": d["email"],
            "joined_at": d["joined_at"],
            "role": role,
            "payment_status": payment_status,
            "valid_until": valid_until,
            "days_left": days_left,
            "last_access": (
                {"ts": d["last_ts"], "decision": d["last_decision"]}
                if d.get("last_ts") else None
            ),
        }
    finally:
        conn.close()


def get_member_by_name(name: str) -> Optional[Dict]:
    """Return active member dict (includes role) or None."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, member_code, name, phone, email, joined_at, is_active, role "
            "FROM members WHERE name=? AND is_active=1",
            (name,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def member_name_check(name: str) -> str:
    """Returns 'active', 'inactive', or 'not_found'."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT is_active FROM members WHERE name=?", (name,)
        ).fetchone()
        if not row:
            return "not_found"
        return "active" if row["is_active"] == 1 else "inactive"
    finally:
        conn.close()




def update_member_contact(name: str, phone: str = "", email: str = "") -> bool:
    """Update phone/email for an active member using the standard DB connection settings."""
    with _lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                "UPDATE members SET phone=?, email=? WHERE name=? AND is_active=1",
                (phone or "", email or "", name),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def update_member_details(
    name: str,
    member_code: str,
    phone: str = "",
    email: Optional[str] = None,
) -> bool:
    """
    Update the user-facing Member ID (member_code) and phone for an active member,
    identified by name. Returns True if a row was updated, False if not found.

    Rules:
        - The internal numeric members.id is never touched.
        - The member's name is never changed here (use rename_member for that).
        - member_code is trimmed; blank raises ValueError.
        - Duplicate member_code raises ValueError (member_code is UNIQUE).
        - phone may be blank (cleared).
        - email is preserved when `email` is None; otherwise it is set.
    """
    code = (member_code or "").strip()
    if not code:
        raise ValueError("Member ID cannot be blank")

    with _lock:
        conn = _get_conn()
        try:
            try:
                if email is None:
                    cur = conn.execute(
                        "UPDATE members SET member_code=?, phone=? "
                        "WHERE name=? AND is_active=1",
                        (code, phone or "", name),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE members SET member_code=?, phone=?, email=? "
                        "WHERE name=? AND is_active=1",
                        (code, phone or "", email or "", name),
                    )
                conn.commit()
            except sqlite3.IntegrityError as e:
                # member_code is the only UNIQUE column this UPDATE can violate.
                raise ValueError(f"Member ID '{code}' is already in use") from e

            return cur.rowcount > 0
        finally:
            conn.close()

# ── Membership plans ───────────────────────────────────────────────────────────

def list_plans(active_only: bool = False) -> List[Dict]:
    conn = _get_conn()
    try:
        sql = (
            "SELECT id, plan_name, duration_days, price, description, is_active "
            "FROM membership_plans "
        )
        if active_only:
            sql += "WHERE is_active = 1 "
        sql += "ORDER BY duration_days"
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def plan_name_exists(plan_name: str, exclude_id: Optional[int] = None) -> bool:
    """True if another plan already uses this (case-insensitive) name."""
    conn = _get_conn()
    try:
        if exclude_id is None:
            row = conn.execute(
                "SELECT 1 FROM membership_plans WHERE plan_name = ? COLLATE NOCASE LIMIT 1",
                (plan_name,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM membership_plans "
                "WHERE plan_name = ? COLLATE NOCASE AND id != ? LIMIT 1",
                (plan_name, exclude_id),
            ).fetchone()
        return row is not None
    finally:
        conn.close()


def add_plan(plan_name: str, duration_days: int, price: float, description: str = "") -> int:
    with _lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO membership_plans (plan_name, duration_days, price, description) "
                "VALUES (?,?,?,?)",
                (plan_name, duration_days, price, description),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def update_plan(
    plan_id: int,
    plan_name: str,
    duration_days: int,
    price: float,
    description: str = "",
) -> bool:
    """
    Update an existing plan's editable fields. Does NOT touch payments —
    historical payments.amount is a snapshot and stays unchanged.
    Returns True if a row was updated, False if the id did not exist.
    """
    with _lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                "UPDATE membership_plans "
                "SET plan_name = ?, duration_days = ?, price = ?, description = ? "
                "WHERE id = ?",
                (plan_name, duration_days, price, description, plan_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def set_plan_active(plan_id: int, is_active: int) -> bool:
    """
    Activate (1) or deactivate (0) a plan. Plans are never hard-deleted, so
    existing payments that reference this plan_id remain valid.
    Returns True if a row was updated, False if the id did not exist.
    """
    with _lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                "UPDATE membership_plans SET is_active = ? WHERE id = ?",
                (1 if is_active else 0, plan_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


# ── Payments ───────────────────────────────────────────────────────────────────

def add_payment(
    member_id: int,
    amount: float,
    valid_from: str,
    valid_until: str,
    plan_id: Optional[int] = None,
    payment_method: str = "cash",
    received_by: str = "",
    note: str = "",
) -> int:
    now_str = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO payments "
                "(member_id, plan_id, amount, payment_date, valid_from, valid_until, "
                " payment_method, received_by, note) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (member_id, plan_id, amount, now_str, valid_from, valid_until,
                 payment_method, received_by, note),
            )
            conn.commit()
            print(
                f"[GYM_DB] Payment recorded: member_id={member_id} "
                f"amount={amount} valid={valid_from}→{valid_until}"
            )
            return cur.lastrowid
        finally:
            conn.close()


def get_latest_valid_payment(member_id: int) -> Optional[Dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT p.*, pl.plan_name FROM payments p "
            "LEFT JOIN membership_plans pl ON p.plan_id = pl.id "
            "WHERE p.member_id=? ORDER BY p.valid_until DESC LIMIT 1",
            (member_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["days_left"] = (date.fromisoformat(d["valid_until"]) - date.today()).days
        return d
    finally:
        conn.close()


def get_member_payments(member_id: int) -> List[Dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT p.*, pl.plan_name FROM payments p "
            "LEFT JOIN membership_plans pl ON p.plan_id = pl.id "
            "WHERE p.member_id=? ORDER BY p.payment_date DESC",
            (member_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_payments(limit: int = 200) -> List[Dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT p.*, m.name as member_name, pl.plan_name "
            "FROM payments p "
            "LEFT JOIN members m ON p.member_id = m.id "
            "LEFT JOIN membership_plans pl ON p.plan_id = pl.id "
            "ORDER BY p.payment_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["days_left"] = (date.fromisoformat(d["valid_until"]) - date.today()).days
            result.append(d)
        return result
    finally:
        conn.close()


# ── Access decision ────────────────────────────────────────────────────────────

def check_member_access(name: str) -> Dict:
    """
    Fast access decision logic for the real-time door path.

    Uses one SQLite connection/query to retrieve member + latest payment. The
    relay decision remains separate, so this function only evaluates access.
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            """
            WITH latest_payment AS (
                SELECT p.*
                FROM payments p
                JOIN members m2 ON m2.id = p.member_id
                WHERE m2.name = ? AND m2.is_active = 1
                ORDER BY p.valid_until DESC, p.id DESC
                LIMIT 1
            )
            SELECT m.id, m.name, m.is_active, m.role,
                   lp.valid_until, lp.amount, lp.plan_id
            FROM members m
            LEFT JOIN latest_payment lp ON 1=1
            WHERE m.name = ? AND m.is_active = 1
            """,
            (name, name),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return {
            "allowed": False,
            "reason": "denied_unknown",
            "message": "Unknown person",
            "member_id": None,
            "days_left": None,
            "valid_until": None,
            "role": "unknown",
        }

    role = row["role"] or ROLE_MEMBER

    if role in PRIVILEGED_ROLES:
        role_label = "Owner" if role == ROLE_OWNER else "Staff"
        return {
            "allowed": True,
            "reason": f"granted_{role}",
            "message": f"{name} — {role_label} access · Door opened",
            "member_id": row["id"],
            "days_left": None,
            "valid_until": None,
            "role": role,
        }

    valid_until = row["valid_until"]
    if not valid_until:
        return {
            "allowed": False,
            "reason": "denied_no_payment",
            "message": f"{name} — No payment record",
            "member_id": row["id"],
            "days_left": None,
            "valid_until": None,
            "role": role,
        }

    days_left = (date.fromisoformat(valid_until) - date.today()).days

    if days_left < 0:
        return {
            "allowed": False,
            "reason": "denied_expired",
            "message": f"{name} — Payment expired on {valid_until}",
            "member_id": row["id"],
            "days_left": days_left,
            "valid_until": valid_until,
            "role": role,
        }

    warning_days = _get_warning_days()
    if days_left <= warning_days:
        return {
            "allowed": True,
            "reason": "granted_expiring_soon",
            "message": f"{name} — Door opened · Expires in {days_left}d",
            "member_id": row["id"],
            "days_left": days_left,
            "valid_until": valid_until,
            "role": role,
        }

    return {
        "allowed": True,
        "reason": "granted_valid",
        "message": f"{name} — Door opened",
        "member_id": row["id"],
        "days_left": days_left,
        "valid_until": valid_until,
        "role": role,
    }

# ── Access logging ─────────────────────────────────────────────────────────────

def log_access(
    name: str,
    decision: str,
    reason: str,
    confidence: float,
    door_opened: bool,
    member_id: Optional[int] = None,
) -> Optional[str]:
    """
    Queue an access attempt for asynchronous SQLite writing.

    Production correction:
      FaceGate already controls per-person cooldown before access decision.
      Do not apply a second DB cooldown here, because it can silently drop
      valid access logs when gate cooldown is changed at runtime.

    Note:
      confidence is accepted for compatibility with gym_access.py, but it is
      intentionally not stored in access_logs for the clean gym-admin view.
    """
    _ensure_log_writer_started()
    now = datetime.now()

    payload = {
        "name": name,
        "decision": decision,
        "reason": reason,
        "confidence": confidence,
        "door_opened": door_opened,
        "member_id": member_id,
        "ts": now.isoformat(timespec="seconds"),
    }

    try:
        _log_queue.put_nowait(payload)
        return "queued"
    except queue.Full:
        # Do not block the detection loop. Losing one log is safer than delaying door access.
        print(f"[GYM_DB] WARNING: access log queue full; dropped log for {name}")
        return None

def log_manual_action(action: str, note: str = "") -> None:
    """Log manual door actions (admin open, emergency lock, test, etc.)."""
    now_str = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO access_logs "
                "(member_id, member_name, ts, decision, reason, door_opened) "
                "VALUES (?,?,?,?,?,?)",
                (None, "ADMIN", now_str, action, note,
                 1 if "open" in action else 0),
            )
            conn.commit()
        finally:
            conn.close()


def get_access_logs(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    name: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
) -> List[Dict]:
    conn = _get_conn()
    try:
        where = []
        params: list = []

        if date_from:
            where.append("ts >= ?")
            params.append(date_from)
        if date_to:
            where.append("ts <= ?")
            params.append(date_to + "T23:59:59")
        if name:
            # Exact search keeps long-term access-log queries index-friendly.
            where.append("member_name = ?")
            params.append(name)

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        params.extend([limit, offset])

        rows = conn.execute(
            f"SELECT * FROM access_logs {clause} ORDER BY ts DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_today_access(limit: int = 2000) -> List[Dict]:
    today = date.today().isoformat()
    return get_access_logs(date_from=today, date_to=today, limit=limit)


def get_last_access() -> Optional[Dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM access_logs ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── Expiry helpers ─────────────────────────────────────────────────────────────

def get_expiring_members(days: Optional[int] = None) -> List[Dict]:
    """Members whose payment expires within `days` days (default: warning_days setting).
    Owner/staff are excluded — they don't need payment."""
    if days is None:
        days = _get_warning_days()

    today = date.today()
    cutoff = (today + timedelta(days=days)).isoformat()
    today_str = today.isoformat()

    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT m.id, m.name, m.phone, m.role,
                   p.valid_until, p.amount, pl.plan_name
            FROM members m
            JOIN (
                SELECT member_id, MAX(valid_until) as valid_until
                FROM payments GROUP BY member_id
            ) latest ON m.id = latest.member_id
            JOIN payments p ON p.member_id = m.id AND p.valid_until = latest.valid_until
            LEFT JOIN membership_plans pl ON p.plan_id = pl.id
            WHERE m.is_active = 1
              AND m.role = 'member'
              AND p.valid_until >= ?
              AND p.valid_until <= ?
            ORDER BY p.valid_until ASC
            """,
            (today_str, cutoff),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["days_left"] = (date.fromisoformat(d["valid_until"]) - today).days
            result.append(d)
        return result
    finally:
        conn.close()


def get_expired_members() -> List[Dict]:
    """Members whose most recent payment has expired. Owner/staff excluded."""
    today = date.today().isoformat()
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT m.id, m.name, m.phone, m.role,
                   p.valid_until, p.amount, pl.plan_name
            FROM members m
            JOIN (
                SELECT member_id, MAX(valid_until) as valid_until
                FROM payments GROUP BY member_id
            ) latest ON m.id = latest.member_id
            JOIN payments p ON p.member_id = m.id AND p.valid_until = latest.valid_until
            LEFT JOIN membership_plans pl ON p.plan_id = pl.id
            WHERE m.is_active = 1
              AND m.role = 'member'
              AND p.valid_until < ?
            ORDER BY p.valid_until DESC
            """,
            (today,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["days_left"] = (date.fromisoformat(d["valid_until"]) - date.today()).days
            result.append(d)
        return result
    finally:
        conn.close()


# ── Revenue ────────────────────────────────────────────────────────────────────

def get_revenue_summary() -> Dict:
    today = date.today()
    today_str = today.isoformat()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    month_start = today.replace(day=1).isoformat()

    conn = _get_conn()
    try:
        def _sum(from_date: str) -> float:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM payments WHERE payment_date >= ?",
                (from_date,),
            ).fetchone()
            return round(float(row[0]), 2)

        today_rev  = _sum(today_str)
        week_rev   = _sum(week_start)
        month_rev  = _sum(month_start)

        plan_rows = conn.execute(
            """
            SELECT pl.plan_name, COUNT(*) as count, SUM(p.amount) as total
            FROM payments p
            LEFT JOIN membership_plans pl ON p.plan_id = pl.id
            WHERE p.payment_date >= ?
            GROUP BY pl.plan_name
            ORDER BY total DESC
            """,
            (month_start,),
        ).fetchall()

        total_members = conn.execute(
            "SELECT COUNT(*) FROM members WHERE is_active=1"
        ).fetchone()[0]

        active_count = conn.execute(
            """
            SELECT COUNT(DISTINCT m.id) FROM members m
            JOIN (
                SELECT member_id, MAX(valid_until) as vu FROM payments GROUP BY member_id
            ) lp ON m.id = lp.member_id
            WHERE m.is_active=1 AND m.role='member' AND lp.vu >= ?
            """,
            (today_str,),
        ).fetchone()[0]

        # Also count privileged members as "active"
        privileged_count = conn.execute(
            "SELECT COUNT(*) FROM members WHERE is_active=1 AND role IN ('owner','staff')"
        ).fetchone()[0]

        regular_members = conn.execute(
            "SELECT COUNT(*) FROM members WHERE is_active=1 AND role='member'"
        ).fetchone()[0]

        expired_count = regular_members - active_count

        warning_days = _get_warning_days()
        expiring_count = len(get_expiring_members(warning_days))

        today_entries = conn.execute(
            "SELECT COUNT(*) FROM access_logs WHERE ts >= ? AND decision LIKE 'access_granted%'",
            (today_str,),
        ).fetchone()[0]

        return {
            "today_revenue":     today_rev,
            "week_revenue":      week_rev,
            "month_revenue":     month_rev,
            "total_members":     total_members,
            "active_members":    active_count + privileged_count,
            "expired_members":   expired_count,
            "expiring_soon":     expiring_count,
            "privileged_members": privileged_count,
            "today_entries":     today_entries,
            "plan_breakdown":    [dict(r) for r in plan_rows],
        }
    finally:
        conn.close()


def get_revenue_range(date_from: str, date_to: str) -> Dict:
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT p.payment_date, p.amount, p.payment_method,
                   m.name as member_name, pl.plan_name
            FROM payments p
            LEFT JOIN members m ON p.member_id = m.id
            LEFT JOIN membership_plans pl ON p.plan_id = pl.id
            WHERE p.payment_date >= ? AND p.payment_date <= ?
            ORDER BY p.payment_date DESC
            """,
            (date_from, date_to + "T23:59:59"),
        ).fetchall()

        records = [dict(r) for r in rows]
        total = sum(r["amount"] for r in records)

        return {
            "records":   records,
            "total":     round(total, 2),
            "count":     len(records),
            "date_from": date_from,
            "date_to":   date_to,
        }
    finally:
        conn.close()


def get_daily_revenue_chart(days: int = 30) -> List[Dict]:
    conn = _get_conn()
    try:
        start = (date.today() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            """
            SELECT DATE(payment_date) as day, SUM(amount) as total
            FROM payments
            WHERE payment_date >= ?
            GROUP BY DATE(payment_date)
            ORDER BY day ASC
            """,
            (start,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── System settings ────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?,?)",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()


def get_all_settings() -> Dict:
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT key, value FROM system_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def _get_warning_days() -> int:
    try:
        return int(get_setting("expiry_warning_days", "7"))
    except Exception:
        return 7


# ── Long-term maintenance ─────────────────────────────────────────────────────

def cleanup_old_access_logs(retention_months: int = 12) -> int:
    """
    Delete only access_logs older than retention_months.

    Production policy:
      - keep the latest 12 months of door/access logs in SQLite
      - remove only logs older than 12 months
      - never remove members, payments, plans, settings, or embeddings here
    """
    retention_months = max(1, int(retention_months))
    cutoff_modifier = f"-{retention_months} months"

    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM access_logs WHERE ts < datetime('now', ?)",
                (cutoff_modifier,),
            ).fetchone()
            old_count = int(row[0] or 0)

            if old_count <= 0:
                print(f"[GYM_DB] Cleanup checked — no access logs older than {retention_months} months")
                return 0

            conn.execute(
                "DELETE FROM access_logs WHERE ts < datetime('now', ?)",
                (cutoff_modifier,),
            )
            conn.commit()
            print(f"[GYM_DB] Cleanup deleted {old_count} access logs older than {retention_months} months")
            return old_count
        finally:
            conn.close()


def optimize_db_after_cleanup() -> None:
    """Run lightweight SQLite optimization after access-log cleanup."""
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("ANALYZE")
            conn.commit()
            print("[GYM_DB] ANALYZE completed after cleanup")
        finally:
            conn.close()


# ── DB stats ───────────────────────────────────────────────────────────────────

def get_db_stats() -> Dict:
    conn = _get_conn()
    try:
        members     = conn.execute("SELECT COUNT(*) FROM members WHERE is_active=1").fetchone()[0]
        payments    = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        access_logs = conn.execute("SELECT COUNT(*) FROM access_logs").fetchone()[0]
        return {
            "members":     members,
            "payments":    payments,
            "access_logs": access_logs,
            "db_path":     str(DB_PATH),
        }
    finally:
        conn.close()

def _ensure_access_log_indexes(conn: sqlite3.Connection) -> None:
    """
    Recreate access_logs indexes after migration/table rebuild.

    Needed because removing the old confidence column rebuilds access_logs,
    and indexes can be lost after DROP/RENAME.
    """
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_access_logs_ts
            ON access_logs(ts);

        CREATE INDEX IF NOT EXISTS idx_access_logs_member
            ON access_logs(member_name);

        CREATE INDEX IF NOT EXISTS idx_access_logs_member_ts
            ON access_logs(member_id, ts DESC);

        CREATE INDEX IF NOT EXISTS idx_access_logs_name_ts
            ON access_logs(member_name, ts DESC);

        CREATE INDEX IF NOT EXISTS idx_access_logs_ts_decision
            ON access_logs(ts, decision);
    """)
