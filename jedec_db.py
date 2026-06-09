#!/usr/bin/env python3
"""
jedec_db.py — SQLite persistence layer for the JEDEC qualification tool.
Database file: jedec_projects.db (created alongside this file on first run).
"""
from __future__ import annotations
import sqlite3, json, os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jedec_projects.db")

# ── Connection ─────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con

# ── Schema ─────────────────────────────────────────────────────────────────────

def init_db():
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            description TEXT    DEFAULT '',
            part_type   TEXT    DEFAULT 'ttv',
            status      TEXT    DEFAULT 'active',
            created_at  TEXT    DEFAULT (datetime('now')),
            updated_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS project_sample_size (
            project_id  INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            ltpd        REAL    DEFAULT 5.0,
            failures    INTEGER DEFAULT 0,
            confidence  REAL    DEFAULT 0.90,
            updated_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS project_pass_fail (
            project_id  INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            n           TEXT    DEFAULT '',
            failures    INTEGER DEFAULT 0,
            confidence  REAL    DEFAULT 0.90,
            ltpd_pct    REAL    DEFAULT 5.0,
            updated_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS project_samples (
            project_id  INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            data        TEXT    DEFAULT '{}',
            updated_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS project_meta (
            project_id    INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            device_name   TEXT    DEFAULT '',
            device_pkg    TEXT    DEFAULT '',
            bond_type     TEXT    DEFAULT '',
            engineer      TEXT    DEFAULT '',
            lot_id        TEXT    DEFAULT '',
            notes         TEXT    DEFAULT '',
            updated_at    TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS project_gantt_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            action_type TEXT    NOT NULL,
            snapshot    TEXT    NOT NULL,
            created_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS project_gantt (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            task_name   TEXT    NOT NULL DEFAULT '',
            category    TEXT    DEFAULT '',
            test_key    TEXT    DEFAULT '',
            start_week  INTEGER DEFAULT 1,
            duration    INTEGER DEFAULT 1,
            status      TEXT    DEFAULT 'not_started',
            sort_order  INTEGER DEFAULT 0
        );
        """)
        # ── Incremental migrations (safe to re-run) ──────────────────────────
        for stmt in [
            "ALTER TABLE project_sample_size ADD COLUMN confidence REAL DEFAULT 0.90",
            "ALTER TABLE project_gantt ADD COLUMN test_key TEXT DEFAULT ''",
            "ALTER TABLE project_meta  ADD COLUMN gantt_start_date TEXT DEFAULT ''",
            """CREATE TABLE IF NOT EXISTS project_gantt_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                action_type TEXT NOT NULL, snapshot TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')))""",
            """CREATE TABLE IF NOT EXISTS project_csam (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                sample_id   TEXT    DEFAULT '',
                test_key    TEXT    DEFAULT '',
                stage       TEXT    DEFAULT '',
                filename    TEXT    DEFAULT '',
                image_data  TEXT    NOT NULL DEFAULT '',
                notes       TEXT    DEFAULT '',
                created_at  TEXT    DEFAULT (datetime('now')))""",
            """CREATE TABLE IF NOT EXISTS project_test_conditions (
                project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                test_key     TEXT    NOT NULL,
                condition_key TEXT   NOT NULL DEFAULT '',
                PRIMARY KEY (project_id, test_key))""",
            """CREATE TABLE IF NOT EXISTS project_test_tracker (
                project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                test_key       TEXT    NOT NULL,
                status         TEXT    NOT NULL DEFAULT 'pending',
                started_at     TEXT    DEFAULT NULL,
                completed_at   TEXT    DEFAULT NULL,
                duration_hours REAL    DEFAULT NULL,
                PRIMARY KEY (project_id, test_key))""",
            "ALTER TABLE project_gantt ADD COLUMN n_mode TEXT DEFAULT 'auto'",
            "ALTER TABLE project_gantt ADD COLUMN n_custom INTEGER DEFAULT NULL",
            "ALTER TABLE project_gantt ADD COLUMN parent_task_id INTEGER DEFAULT NULL",
        ]:
            try:
                con.execute(stmt)
            except Exception:
                pass  # already exists

# ── Projects CRUD ──────────────────────────────────────────────────────────────

def list_projects() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT id, name, description, part_type, status, created_at, updated_at "
            "FROM projects ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

def create_project(name: str, description: str = "", part_type: str = "ttv") -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO projects (name, description, part_type) VALUES (?,?,?)",
            (name.strip(), description.strip(), part_type)
        )
        pid = cur.lastrowid
        # Seed empty meta row
        con.execute("INSERT INTO project_meta (project_id) VALUES (?)", (pid,))
        return pid

def get_project(pid: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None

def update_project(pid: int, **kwargs):
    allowed = {"name", "description", "part_type", "status"}
    fields  = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    fields["updated_at"] = datetime.utcnow().isoformat(timespec="seconds")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _conn() as con:
        con.execute(f"UPDATE projects SET {set_clause} WHERE id=?",
                    (*fields.values(), pid))

def delete_project(pid: int):
    with _conn() as con:
        con.execute("DELETE FROM projects WHERE id=?", (pid,))

def _touch(con: sqlite3.Connection, pid: int):
    con.execute("UPDATE projects SET updated_at=datetime('now') WHERE id=?", (pid,))

# ── Project Meta ───────────────────────────────────────────────────────────────

def get_meta(pid: int) -> dict:
    with _conn() as con:
        row = con.execute("SELECT * FROM project_meta WHERE project_id=?", (pid,)).fetchone()
        return dict(row) if row else {}

def save_meta(pid: int, **kwargs):
    allowed = {"device_name", "device_pkg", "bond_type", "engineer", "lot_id", "notes",
               "gantt_start_date"}
    fields  = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    fields["updated_at"] = datetime.utcnow().isoformat(timespec="seconds")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _conn() as con:
        con.execute(f"UPDATE project_meta SET {set_clause} WHERE project_id=?",
                    (*fields.values(), pid))
        _touch(con, pid)

# ── Sample Size ────────────────────────────────────────────────────────────────

def get_sample_size(pid: int) -> dict:
    with _conn() as con:
        row = con.execute(
            "SELECT ltpd, failures, confidence FROM project_sample_size WHERE project_id=?", (pid,)
        ).fetchone()
        return dict(row) if row else {"ltpd": 5.0, "failures": 0, "confidence": 0.90}

def save_sample_size(pid: int, ltpd: float, failures: int, confidence: float = 0.90):
    with _conn() as con:
        con.execute("""
            INSERT INTO project_sample_size (project_id, ltpd, failures, confidence, updated_at)
            VALUES (?,?,?,?,datetime('now'))
            ON CONFLICT(project_id) DO UPDATE SET
                ltpd=excluded.ltpd, failures=excluded.failures,
                confidence=excluded.confidence,
                updated_at=excluded.updated_at
        """, (pid, ltpd, failures, confidence))
        _touch(con, pid)

# ── Pass / Fail ────────────────────────────────────────────────────────────────

def get_pass_fail(pid: int) -> dict:
    with _conn() as con:
        row = con.execute(
            "SELECT n, failures, confidence, ltpd_pct FROM project_pass_fail WHERE project_id=?",
            (pid,)
        ).fetchone()
        return dict(row) if row else {"n": "", "failures": 0, "confidence": 0.90, "ltpd_pct": 5.0}

def save_pass_fail(pid: int, n, failures: int, confidence: float, ltpd_pct: float):
    with _conn() as con:
        con.execute("""
            INSERT INTO project_pass_fail (project_id, n, failures, confidence, ltpd_pct, updated_at)
            VALUES (?,?,?,?,?,datetime('now'))
            ON CONFLICT(project_id) DO UPDATE SET
                n=excluded.n, failures=excluded.failures,
                confidence=excluded.confidence, ltpd_pct=excluded.ltpd_pct,
                updated_at=excluded.updated_at
        """, (pid, str(n), failures, confidence, ltpd_pct))
        _touch(con, pid)

# ── Sample Records ─────────────────────────────────────────────────────────────

def get_samples(pid: int) -> dict:
    with _conn() as con:
        row = con.execute(
            "SELECT data FROM project_samples WHERE project_id=?", (pid,)
        ).fetchone()
        return json.loads(row["data"]) if row else {}

def save_samples(pid: int, data: dict):
    with _conn() as con:
        con.execute("""
            INSERT INTO project_samples (project_id, data, updated_at)
            VALUES (?,?,datetime('now'))
            ON CONFLICT(project_id) DO UPDATE SET
                data=excluded.data, updated_at=excluded.updated_at
        """, (pid, json.dumps(data)))
        _touch(con, pid)

# ── GANTT Tasks ────────────────────────────────────────────────────────────────

def list_gantt_tasks(pid: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM project_gantt WHERE project_id=? ORDER BY sort_order, id",
            (pid,)
        ).fetchall()
        return [dict(r) for r in rows]

def add_gantt_task(pid: int, task_name: str, category: str = "",
                   test_key: str = "", start_week: int = 1, duration: int = 1,
                   status: str = "not_started",
                   n_mode: str = "auto", n_custom: int | None = None,
                   parent_task_id: int | None = None) -> int:
    with _conn() as con:
        max_order = con.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM project_gantt WHERE project_id=?",
            (pid,)
        ).fetchone()[0]
        cur = con.execute(
            """INSERT INTO project_gantt
               (project_id, task_name, category, test_key, start_week, duration,
                status, sort_order, n_mode, n_custom, parent_task_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, task_name.strip(), category.strip(), test_key.strip(),
             int(start_week), int(duration), status, max_order + 10,
             n_mode, n_custom, parent_task_id)
        )
        _touch(con, pid)
        return cur.lastrowid

def update_gantt_task(tid: int, pid: int, **kwargs):
    allowed = {"task_name", "category", "test_key", "start_week", "duration",
               "status", "sort_order", "n_mode", "n_custom", "parent_task_id"}
    fields  = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _conn() as con:
        con.execute(
            f"UPDATE project_gantt SET {set_clause} WHERE id=? AND project_id=?",
            (*fields.values(), tid, pid)
        )
        _touch(con, pid)

def delete_gantt_task(tid: int, pid: int):
    with _conn() as con:
        con.execute(
            "DELETE FROM project_gantt WHERE id=? AND project_id=?", (tid, pid)
        )
        _touch(con, pid)

def clear_gantt_tasks(pid: int):
    with _conn() as con:
        con.execute("DELETE FROM project_gantt WHERE project_id=?", (pid,))
        _touch(con, pid)

# ── GANTT Undo History ─────────────────────────────────────────────────────────

_HISTORY_LIMIT = 20   # max entries kept per project

def push_gantt_history(pid: int, action_type: str, snapshot):
    """Push a snapshot onto the undo stack. snapshot may be a dict or list."""
    with _conn() as con:
        con.execute(
            "INSERT INTO project_gantt_history (project_id, action_type, snapshot) VALUES (?,?,?)",
            (pid, action_type, json.dumps(snapshot))
        )
        # Trim to limit (keep newest)
        con.execute(
            """DELETE FROM project_gantt_history WHERE id IN (
               SELECT id FROM project_gantt_history WHERE project_id=?
               ORDER BY id DESC LIMIT -1 OFFSET ?)""",
            (pid, _HISTORY_LIMIT)
        )

def pop_gantt_history(pid: int) -> dict | None:
    """Pop the most recent history entry for this project. Returns None if empty."""
    with _conn() as con:
        row = con.execute(
            "SELECT id, action_type, snapshot FROM project_gantt_history "
            "WHERE project_id=? ORDER BY id DESC LIMIT 1",
            (pid,)
        ).fetchone()
        if not row:
            return None
        con.execute("DELETE FROM project_gantt_history WHERE id=?", (row["id"],))
        return {"action_type": row["action_type"], "snapshot": json.loads(row["snapshot"])}

def has_gantt_history(pid: int) -> bool:
    with _conn() as con:
        return bool(con.execute(
            "SELECT 1 FROM project_gantt_history WHERE project_id=? LIMIT 1", (pid,)
        ).fetchone())

def reorder_gantt_tasks(pid: int, ordered_ids: list[int]):
    """Set sort_order = 10,20,30,... for the given task IDs in order."""
    with _conn() as con:
        for i, tid in enumerate(ordered_ids):
            con.execute(
                "UPDATE project_gantt SET sort_order=? WHERE id=? AND project_id=?",
                ((i + 1) * 10, int(tid), pid)
            )
        _touch(con, pid)

def bulk_add_gantt_tasks(pid: int, tasks: list[dict]):
    """Insert a list of task dicts; skip if project already has tasks.

    Each dict may include '_parent_idx' (int index into the same list) to
    wire up parent_task_id after all rows are inserted.
    """
    with _conn() as con:
        existing = con.execute(
            "SELECT COUNT(*) FROM project_gantt WHERE project_id=?", (pid,)
        ).fetchone()[0]
        if existing:
            return  # don't overwrite user's tasks
        inserted_ids: list[int] = []
        for i, t in enumerate(tasks):
            cur = con.execute(
                """INSERT INTO project_gantt
                   (project_id, task_name, category, test_key,
                    start_week, duration, status, sort_order,
                    n_mode, n_custom)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (pid, t["task_name"], t.get("category", ""),
                 t.get("test_key", ""),
                 t.get("start_week", 1), t.get("duration", 1),
                 "not_started", (i + 1) * 10,
                 t.get("n_mode", "auto"),
                 t.get("n_custom"))
            )
            inserted_ids.append(cur.lastrowid)
        # Second pass: resolve _parent_idx → parent_task_id
        for i, t in enumerate(tasks):
            p_idx = t.get("_parent_idx")
            if p_idx is not None:
                con.execute(
                    "UPDATE project_gantt SET parent_task_id=? WHERE id=?",
                    (inserted_ids[p_idx], inserted_ids[i])
                )
        _touch(con, pid)

# ── Test Conditions (per-project selected condition per test) ─────────────────

def get_test_conditions(pid: int) -> dict:
    """Return {test_key: condition_key} for all saved conditions."""
    with _conn() as con:
        rows = con.execute(
            "SELECT test_key, condition_key FROM project_test_conditions WHERE project_id=?",
            (pid,)
        ).fetchall()
        return {r["test_key"]: r["condition_key"] for r in rows}

def save_test_conditions(pid: int, conditions: dict):
    """Upsert {test_key: condition_key} mapping."""
    with _conn() as con:
        for key, val in conditions.items():
            con.execute(
                """INSERT INTO project_test_conditions (project_id, test_key, condition_key)
                   VALUES (?,?,?)
                   ON CONFLICT(project_id, test_key) DO UPDATE SET condition_key=excluded.condition_key""",
                (pid, key.strip(), val.strip())
            )
        _touch(con, pid)

# ── Test Tracker (start / complete per test) ───────────────────────────────────

def get_test_tracker(pid: int) -> dict:
    """Return {test_key: {status, started_at, completed_at, duration_hours}}."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM project_test_tracker WHERE project_id=?", (pid,)
        ).fetchall()
        return {r["test_key"]: dict(r) for r in rows}

def upsert_test_tracker(pid: int, test_key: str, **kwargs):
    """Insert or update a tracker row. kwargs: status, started_at, completed_at, duration_hours."""
    allowed = {"status", "started_at", "completed_at", "duration_hours"}
    fields  = {k: v for k, v in kwargs.items() if k in allowed}
    with _conn() as con:
        existing = con.execute(
            "SELECT 1 FROM project_test_tracker WHERE project_id=? AND test_key=?",
            (pid, test_key)
        ).fetchone()
        if existing:
            if fields:
                set_clause = ", ".join(f"{k}=?" for k in fields)
                con.execute(
                    f"UPDATE project_test_tracker SET {set_clause} WHERE project_id=? AND test_key=?",
                    (*fields.values(), pid, test_key)
                )
        else:
            fields.setdefault("status", "pending")
            cols = ", ".join(["project_id", "test_key"] + list(fields.keys()))
            vals = ", ".join(["?"] * (2 + len(fields)))
            con.execute(
                f"INSERT INTO project_test_tracker ({cols}) VALUES ({vals})",
                (pid, test_key, *fields.values())
            )
        _touch(con, pid)

# ── CSAM Image Repository ──────────────────────────────────────────────────────

def upsert_csam_image(pid: int, sample_id: str, test_key: str, stage: str,
                      image_data: str, filename: str = "", notes: str = "") -> int:
    """Delete any existing image for (pid, test_key, sample_id, stage) then insert fresh."""
    with _conn() as con:
        con.execute(
            "DELETE FROM project_csam WHERE project_id=? AND test_key=? AND sample_id=? AND stage=?",
            (pid, test_key.strip(), sample_id.strip(), stage.strip())
        )
        cur = con.execute(
            """INSERT INTO project_csam
               (project_id, sample_id, test_key, stage, filename, image_data, notes)
               VALUES (?,?,?,?,?,?,?)""",
            (pid, sample_id.strip(), test_key.strip(), stage.strip(),
             filename.strip(), image_data, notes.strip())
        )
        _touch(con, pid)
        return cur.lastrowid

def save_csam_image(pid: int, sample_id: str, test_key: str, stage: str,
                    image_data: str, filename: str = "", notes: str = "") -> int:
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO project_csam
               (project_id, sample_id, test_key, stage, filename, image_data, notes)
               VALUES (?,?,?,?,?,?,?)""",
            (pid, sample_id.strip(), test_key.strip(), stage.strip(),
             filename.strip(), image_data, notes.strip())
        )
        _touch(con, pid)
        return cur.lastrowid

def list_csam_images(pid: int) -> list[dict]:
    """Return image metadata (no image_data blob) ordered by test, sample, stage."""
    with _conn() as con:
        rows = con.execute(
            "SELECT id, sample_id, test_key, stage, filename, notes, created_at "
            "FROM project_csam WHERE project_id=? "
            "ORDER BY test_key, sample_id, stage, id",
            (pid,)
        ).fetchall()
        return [dict(r) for r in rows]

def get_csam_image(iid: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM project_csam WHERE id=?", (iid,)).fetchone()
        return dict(row) if row else None

def delete_csam_image(iid: int):
    with _conn() as con:
        con.execute("DELETE FROM project_csam WHERE id=?", (iid,))
