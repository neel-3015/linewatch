"""Alert rules, evaluated on every accepted reading.

Four rules:
  OVERTEMP   temperature thresholds (warning / critical)
  VIBRATION  vibration severity zones modeled on ISO 10816 (7.1 / 11.2 mm/s)
  VIB_TREND  vibration rising steadily -> early warning of bearing wear,
             usually fires minutes before VIBRATION does
  FAULT      the machine itself reports a FAULT state

Threshold alerts use hysteresis: an alert opens at the warning level but only
closes once the value falls below 90% of it, so a value hovering right at the
line doesn't open and close an alert every few seconds.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Threshold:
    rule: str
    field: str
    label: str
    unit: str
    warn: float
    crit: float


THRESHOLDS = (
    Threshold("OVERTEMP", "temp_c", "Temperature", "°C", 85.0, 95.0),
    Threshold("VIBRATION", "vibration", "Vibration", "mm/s", 7.1, 11.2),
)
HYSTERESIS = 0.90

TREND_WINDOW_S = 120          # slope is fit over the last 2 minutes of RUNNING data
TREND_MIN_SPAN_S = 30         # ...which must cover at least 30 s
TREND_MIN_POINTS = 10
TREND_OPEN = 0.6              # mm/s per minute
TREND_CLOSE = 0.3


def slope_per_minute(points):
    """Least-squares slope of (ts_seconds, value) pairs, in units per minute."""
    n = len(points)
    if n < 2:
        return 0.0
    mx = sum(t for t, _ in points) / n
    my = sum(v for _, v in points) / n
    sxx = sum((t - mx) ** 2 for t, _ in points)
    if sxx == 0:
        return 0.0
    sxy = sum((t - mx) * (v - my) for t, v in points)
    return sxy / sxx * 60.0


def _open_alert(conn, mid, rule):
    return conn.execute(
        "SELECT * FROM alerts WHERE machine_id=? AND rule=? AND closed_ts IS NULL",
        (mid, rule)).fetchone()


def _open(conn, mid, rule, severity, message, ts, value):
    conn.execute(
        "INSERT INTO alerts(machine_id, rule, severity, message, opened_ts, peak) "
        "VALUES (?,?,?,?,?,?)", (mid, rule, severity, message, ts, value))
    return {"event": "opened", "machine_id": mid, "rule": rule,
            "severity": severity, "message": message}


def _close(conn, alert, ts):
    conn.execute("UPDATE alerts SET closed_ts=? WHERE id=?", (ts, alert["id"]))
    return {"event": "closed", "machine_id": alert["machine_id"],
            "rule": alert["rule"], "severity": alert["severity"]}


def _threshold(conn, r, th):
    mid, ts, v = r["machine_id"], r["ts"], r[th.field]
    sev = "critical" if v >= th.crit else "warning" if v >= th.warn else None
    cur = _open_alert(conn, mid, th.rule)
    msg = f"{th.label} {v:.1f} {th.unit} (limit {th.crit if sev == 'critical' else th.warn} {th.unit})"

    if sev and not cur:
        return [_open(conn, mid, th.rule, sev, msg, ts, v)]
    if sev and cur:
        peak = max(cur["peak"] or v, v)
        if sev == "critical" and cur["severity"] == "warning":   # escalate in place
            conn.execute("UPDATE alerts SET severity='critical', message=?, peak=? WHERE id=?",
                         (msg, peak, cur["id"]))
            return [{"event": "escalated", "machine_id": mid, "rule": th.rule,
                     "severity": "critical", "message": msg}]
        conn.execute("UPDATE alerts SET peak=? WHERE id=?", (peak, cur["id"]))
        return []
    if cur and v < th.warn * HYSTERESIS:
        return [_close(conn, cur, ts)]
    return []


def _fault(conn, r):
    mid, ts = r["machine_id"], r["ts"]
    cur = _open_alert(conn, mid, "FAULT")
    if r["state"] == "FAULT" and not cur:
        return [_open(conn, mid, "FAULT", "critical", "Machine reported FAULT state", ts, None)]
    if r["state"] != "FAULT" and cur:
        return [_close(conn, cur, ts)]
    return []


def _trend(conn, r):
    mid, ts = r["machine_id"], r["ts"]
    rows = conn.execute(
        "SELECT ts, vibration FROM readings WHERE machine_id=? AND state='RUNNING' "
        "AND ts>=? ORDER BY ts", (mid, ts - TREND_WINDOW_S)).fetchall()
    cur = _open_alert(conn, mid, "VIB_TREND")
    if len(rows) < TREND_MIN_POINTS or rows[-1]["ts"] - rows[0]["ts"] < TREND_MIN_SPAN_S:
        return [_close(conn, cur, ts)] if cur else []
    slope = slope_per_minute([(row["ts"], row["vibration"]) for row in rows])
    if slope >= TREND_OPEN and not cur:
        return [_open(conn, mid, "VIB_TREND", "warning",
                      f"Vibration rising {slope:.2f} mm/s per min - possible bearing wear",
                      ts, slope)]
    if cur and slope < TREND_CLOSE:
        return [_close(conn, cur, ts)]
    return []


def evaluate(conn, reading):
    """Run every rule for one reading (already inserted). Returns alert events."""
    events = []
    for th in THRESHOLDS:
        events += _threshold(conn, reading, th)
    events += _fault(conn, reading)
    events += _trend(conn, reading)
    return events
