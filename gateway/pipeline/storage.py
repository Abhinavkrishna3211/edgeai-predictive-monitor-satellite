#!/usr/bin/env python3
"""
storage.py — SQLite-backed persistence for EPM alert events, maintenance log,
and serialisable model state (adaptive baselines, RUL estimator).

Uses WAL journaling so the HTTP reader thread never blocks the satellite
writer thread, and a crash mid-write leaves the DB in a consistent state.

Thread-safety: sqlite3 connections opened with check_same_thread=False and
isolation_level=None (autocommit).  The GIL plus SQLite's own WAL locking
makes single-row inserts and SELECT queries safe without a Python-level lock.
Only DDL (CREATE TABLE) is run inside a BEGIN/COMMIT via executescript().
"""

import gzip
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS satellites (
    name       TEXT PRIMARY KEY,
    mac        TEXT,
    first_seen INTEGER,
    last_seen  INTEGER
);
CREATE TABLE IF NOT EXISTS alert_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    satellite  TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    from_state TEXT,
    to_state   TEXT    NOT NULL,
    p_fault    REAL,
    reason     TEXT,
    FOREIGN KEY(satellite) REFERENCES satellites(name)
);
CREATE INDEX IF NOT EXISTS idx_alerts_sat_ts ON alert_events(satellite, ts);

CREATE TABLE IF NOT EXISTS maintenance (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    satellite  TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    technician TEXT,
    work_type  TEXT,
    notes      TEXT,
    FOREIGN KEY(satellite) REFERENCES satellites(name)
);
CREATE INDEX IF NOT EXISTS idx_maint_sat_ts ON maintenance(satellite, ts);

CREATE TABLE IF NOT EXISTS model_state (
    satellite  TEXT    NOT NULL,
    component  TEXT    NOT NULL,   -- 'baselines', 'rul', etc.
    state_json TEXT    NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (satellite, component)
);
"""


class Storage:
    """Crash-safe SQLite store for EPM alert events, maintenance, and model state."""

    def __init__(self, db_path: str = "logs/epm.db"):
        self.path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._connect()
        # DDL must run outside autocommit (executescript wraps in BEGIN/COMMIT)
        self.conn.executescript(SCHEMA)
        # WAL mode: readers never block writer; writer never blocks readers.
        # Must be set AFTER the schema so the journal mode applies to all tables.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")   # durable but fast
        self.conn.execute("PRAGMA cache_size = -8000")     # 8 MB page cache

    def _connect(self):
        # check_same_thread=False: we rely on the GIL + WAL for safety.
        # isolation_level=None: autocommit — every INSERT commits immediately.
        self.conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None)

    # ── Alert events ──────────────────────────────────────────────────────────

    def log_alert(self, sat: str, from_state: str, to_state: str,
                  p_fault: float, reason: str = "") -> None:
        """Persist one state-change or INFO event to the alert_events table."""
        self.conn.execute(
            "INSERT INTO alert_events"
            "(satellite, ts, from_state, to_state, p_fault, reason)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (sat, int(time.time()), from_state, to_state,
             float(p_fault), reason or ""),
        )

    def recent_alerts(self, sat: str = None, limit: int = 100):
        """Return up to `limit` most-recent alert rows, optionally filtered by satellite."""
        if sat:
            return self.conn.execute(
                "SELECT * FROM alert_events"
                " WHERE satellite=? ORDER BY ts DESC LIMIT ?",
                (sat, limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM alert_events ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()

    # ── Maintenance log ───────────────────────────────────────────────────────

    def log_maintenance(self, sat: str, technician: str,
                        work_type: str, notes: str) -> None:
        """Append a maintenance event.  `notes` may be a raw string or JSON dict."""
        self.conn.execute(
            "INSERT INTO maintenance"
            "(satellite, ts, technician, work_type, notes)"
            " VALUES (?, ?, ?, ?, ?)",
            (sat, int(time.time()), technician or "", work_type or "", notes or ""),
        )

    def get_latest_maintenance(self, sat: str) -> "dict | None":
        """Return the most recent maintenance record for `sat` as a dict, or None."""
        row = self.conn.execute(
            "SELECT technician, work_type, notes, ts"
            " FROM maintenance WHERE satellite=?"
            " ORDER BY ts DESC LIMIT 1",
            (sat,),
        ).fetchone()
        if row is None:
            return None
        technician, work_type, notes_field, ts = row
        # notes may be a JSON-serialised full record dict (written by recv_verify)
        try:
            record = json.loads(notes_field) if notes_field else {}
        except (json.JSONDecodeError, TypeError):
            record = {
                "technician": technician,
                "maint_type": work_type,
                "notes":      notes_field or "",
                "updated_at": ts,
            }
        return record

    def get_all_maintenance(self) -> dict:
        """Return {satellite: latest_record_dict} for every satellite with maintenance history."""
        # Subquery isolates max(ts) per satellite before joining for notes
        rows = self.conn.execute(
            "SELECT m.satellite, m.technician, m.work_type, m.notes, m.ts"
            " FROM maintenance m"
            " INNER JOIN ("
            "   SELECT satellite, MAX(ts) AS max_ts FROM maintenance GROUP BY satellite"
            " ) latest ON m.satellite = latest.satellite AND m.ts = latest.max_ts"
        ).fetchall()
        result: dict = {}
        for sat, tech, wtype, notes_field, ts in rows:
            try:
                record = json.loads(notes_field) if notes_field else {}
            except (json.JSONDecodeError, TypeError):
                record = {"technician": tech, "maint_type": wtype,
                          "notes": notes_field or "", "updated_at": ts}
            result[sat] = record
        return result

    # ── Model state ───────────────────────────────────────────────────────────

    def save_model_state(self, sat: str, component: str, state: dict) -> None:
        """Upsert a JSON-serialisable model state (replaces any existing row)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO model_state"
            "(satellite, component, state_json, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (sat, component, json.dumps(state), int(time.time())),
        )

    def load_model_state(self, sat: str, component: str) -> "dict | None":
        """Return the stored state dict, or None if no entry exists."""
        row = self.conn.execute(
            "SELECT state_json FROM model_state"
            " WHERE satellite=? AND component=?",
            (sat, component),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    # ── Connection lifecycle ──────────────────────────────────────────────────

    def close(self) -> None:
        """Close the SQLite connection (releases WAL file lock on Windows)."""
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ── Satellite registry ────────────────────────────────────────────────────

    def upsert_satellite(self, name: str, mac: str) -> None:
        """Register (or update last_seen for) a satellite."""
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO satellites(name, mac, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET mac=excluded.mac, last_seen=excluded.last_seen",
            (name, mac, now, now),
        )


# ── CSV log rotation (called from recv_verify background thread) ───────────────

def rotate_old_csvs(csv_root: str, max_age_days: int = 90) -> int:
    """Gzip CSV files older than `max_age_days` under `csv_root`.

    Returns the number of files compressed this run.
    """
    cutoff = time.time() - max_age_days * 86400
    count  = 0
    for dirpath, _dirs, files in os.walk(csv_root):
        for fname in files:
            if not fname.endswith(".csv"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                if os.path.getmtime(fpath) < cutoff:
                    gz_path = fpath + ".gz"
                    with open(fpath, "rb") as f_in:
                        with gzip.open(gz_path, "wb") as f_out:
                            f_out.write(f_in.read())
                    os.remove(fpath)
                    count += 1
            except OSError:
                pass
    return count
