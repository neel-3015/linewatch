"""SQLite storage layer: schema, connections, retention."""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    line        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id           INTEGER PRIMARY KEY,
    machine_id   TEXT NOT NULL REFERENCES machines(id),
    ts           REAL NOT NULL,
    state        TEXT NOT NULL,
    temp_c       REAL NOT NULL,
    vibration    REAL NOT NULL,
    rpm          REAL NOT NULL,
    parts_total  INTEGER NOT NULL
);
-- unique (machine, ts): a re-sent reading (device retry, replayed batch) is stored once
CREATE UNIQUE INDEX IF NOT EXISTS ux_readings_machine_ts ON readings(machine_id, ts);
CREATE INDEX IF NOT EXISTS ix_readings_ts ON readings(ts);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY,
    machine_id  TEXT NOT NULL REFERENCES machines(id),
    rule        TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('warning', 'critical')),
    message     TEXT NOT NULL,
    opened_ts   REAL NOT NULL,
    closed_ts   REAL,
    peak        REAL
);
-- at most one open alert per (machine, rule)
CREATE UNIQUE INDEX IF NOT EXISTS ux_alerts_open
    ON alerts(machine_id, rule) WHERE closed_ts IS NULL;

-- dead-letter table: every payload that failed validation, with the reason
CREATE TABLE IF NOT EXISTS rejected (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,
    reason   TEXT NOT NULL,
    payload  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rejected_ts ON rejected(ts);
"""

RETENTION_SECONDS = 24 * 3600


def connect(path):
    # isolation_level=None -> we manage transactions explicitly (BEGIN IMMEDIATE)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # readers don't block the writer
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


def prune(conn, now, keep_seconds=RETENTION_SECONDS):
    """Drop raw readings and dead-letter rows older than the retention window."""
    cutoff = now - keep_seconds
    n = conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,)).rowcount
    conn.execute("DELETE FROM rejected WHERE ts < ?", (cutoff,))
    return n
