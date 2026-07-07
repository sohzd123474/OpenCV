"""SQLite storage — the local, single-site counterpart of db/schema.sql.

Embeddings are stored as float32 blobs with an explicit dim column so the
128-D SFace default and a 512-D ArcFace upgrade can coexist (matching only
ever compares same-dim vectors from the same model).
"""
import sqlite3
from datetime import datetime, timezone

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id         INTEGER PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS embeddings (
    id          INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    vec         BLOB NOT NULL,
    dim         INTEGER NOT NULL,
    quality     REAL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS match_attempts (
    id          INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    decision    TEXT NOT NULL,
    employee_id INTEGER,
    similarity  REAL,
    margin      REAL,
    quality     REAL
);
CREATE TABLE IF NOT EXISTS attendance (
    id          INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    event_type  TEXT NOT NULL CHECK (event_type IN ('check_in','check_out')),
    occurred_at TEXT NOT NULL,
    similarity  REAL,
    synced      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_att_emp_time ON attendance(employee_id, occurred_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def add_employee(conn, code: str, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO employees(code, name, created_at) VALUES (?,?,?)",
        (code, name, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def get_employee_by_code(conn, code: str):
    return conn.execute("SELECT * FROM employees WHERE code=?", (code,)).fetchone()


def list_employees(conn):
    return conn.execute(
        "SELECT e.*, COUNT(m.id) AS n_embeddings FROM employees e "
        "LEFT JOIN embeddings m ON m.employee_id = e.id "
        "GROUP BY e.id ORDER BY e.code"
    ).fetchall()


def add_embedding(conn, employee_id: int, vec: np.ndarray, quality: float) -> None:
    vec = np.asarray(vec, dtype=np.float32)
    conn.execute(
        "INSERT INTO embeddings(employee_id, vec, dim, quality, created_at) VALUES (?,?,?,?,?)",
        (employee_id, vec.tobytes(), vec.shape[0], float(quality), now_iso()),
    )
    conn.commit()


def gallery(conn, dim: int):
    """All embeddings of the given dimension as (employee_id, vector) pairs."""
    rows = conn.execute("SELECT employee_id, vec FROM embeddings WHERE dim=?", (dim,)).fetchall()
    return [(r["employee_id"], np.frombuffer(r["vec"], dtype=np.float32)) for r in rows]


def record_attempt(conn, decision: str, employee_id, similarity, margin, quality) -> None:
    conn.execute(
        "INSERT INTO match_attempts(occurred_at, decision, employee_id, similarity, margin, quality) "
        "VALUES (?,?,?,?,?,?)",
        (now_iso(), decision, employee_id, similarity, margin, quality),
    )
    conn.commit()


def last_attendance(conn, employee_id: int):
    return conn.execute(
        "SELECT * FROM attendance WHERE employee_id=? ORDER BY occurred_at DESC, id DESC LIMIT 1",
        (employee_id,),
    ).fetchone()


def record_attendance(conn, employee_id: int, event_type: str, similarity: float) -> int:
    cur = conn.execute(
        "INSERT INTO attendance(employee_id, event_type, occurred_at, similarity) VALUES (?,?,?,?)",
        (employee_id, event_type, now_iso(), float(similarity)),
    )
    conn.commit()
    return cur.lastrowid


def attendance_report(conn, date_from: str | None = None, date_to: str | None = None):
    query = (
        "SELECT a.occurred_at, e.code, e.name, a.event_type, a.similarity "
        "FROM attendance a JOIN employees e ON e.id = a.employee_id WHERE 1=1"
    )
    params: list = []
    if date_from:
        query += " AND a.occurred_at >= ?"
        params.append(date_from)
    if date_to:
        query += " AND a.occurred_at <= ?"
        params.append(date_to)
    query += " ORDER BY a.occurred_at"
    return conn.execute(query, params).fetchall()


def unsynced_events(conn):
    return conn.execute(
        "SELECT a.id, a.event_type, a.occurred_at, a.similarity, e.code AS employee_code "
        "FROM attendance a JOIN employees e ON e.id = a.employee_id "
        "WHERE a.synced = 0 ORDER BY a.occurred_at"
    ).fetchall()


def mark_synced(conn, event_id: int) -> None:
    conn.execute("UPDATE attendance SET synced=1 WHERE id=?", (event_id,))
    conn.commit()
