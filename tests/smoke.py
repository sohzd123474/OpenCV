"""Smoke test for the non-camera parts: matcher decisions and DB round-trips.

Run:  python tests/smoke.py
"""
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db as dbm
from app.matcher import Matcher


def unit(*values):
    v = np.asarray(values, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_matcher():
    alice = unit(1.0, 0.0, 0.0)
    bob = unit(0.0, 1.0, 0.0)
    gallery = [(1, alice), (2, bob)]
    m = Matcher(gallery, accept=0.40, reject=0.28, min_margin=0.05)

    d = m.match(unit(0.95, 0.05, 0.0))          # clearly alice
    assert d.outcome == "match" and d.employee_id == 1, d

    d = m.match(unit(1.0, 1.0, 0.2))            # between alice and bob -> ambiguous
    assert d.outcome == "buffer", d

    d = m.match(unit(0.3, 0.1, 1.0))            # weak similarity -> buffer zone
    assert d.outcome in ("buffer", "reject"), d

    d = m.match(unit(0.0, 0.0, 1.0))            # orthogonal -> reject
    assert d.outcome == "reject" and d.employee_id is None, d

    d = Matcher([], 0.40, 0.28, 0.05).match(alice)   # empty gallery
    assert d.outcome == "reject", d

    try:
        Matcher(gallery, accept=0.3, reject=0.4, min_margin=0.05)
        raise AssertionError("inverted thresholds should raise")
    except ValueError:
        pass
    print("matcher: OK")


def test_db():
    with tempfile.TemporaryDirectory() as tmp:
        conn = dbm.connect(os.path.join(tmp, "test.sqlite3"))
        emp_id = dbm.add_employee(conn, "E001", "Ada Lovelace")

        vec = unit(0.2, 0.5, 0.8)
        dbm.add_embedding(conn, emp_id, vec, quality=120.0)
        gallery = dbm.gallery(conn, 3)
        assert len(gallery) == 1 and gallery[0][0] == emp_id
        assert np.allclose(gallery[0][1], vec), "embedding blob round-trip failed"
        assert dbm.gallery(conn, 128) == [], "dim filter failed"

        assert dbm.last_attendance(conn, emp_id) is None
        dbm.record_attendance(conn, emp_id, "check_in", 0.71)
        assert dbm.last_attendance(conn, emp_id)["event_type"] == "check_in"
        dbm.record_attendance(conn, emp_id, "check_out", 0.69)
        assert dbm.last_attendance(conn, emp_id)["event_type"] == "check_out"

        dbm.record_attempt(conn, "reject", None, 0.11, 0.0, 80.0)
        rows = dbm.attendance_report(conn)
        assert len(rows) == 2 and rows[0]["code"] == "E001"

        unsynced = dbm.unsynced_events(conn)
        assert len(unsynced) == 2 and unsynced[0]["employee_code"] == "E001"
        dbm.mark_synced(conn, unsynced[0]["id"])
        assert len(dbm.unsynced_events(conn)) == 1
        conn.close()  # Windows: file must be closed before the temp dir is removed
    print("db: OK")


if __name__ == "__main__":
    test_matcher()
    test_db()
    print("all smoke tests passed")
