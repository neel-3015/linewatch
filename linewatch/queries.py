"""Read-side queries that feed the dashboard."""

STALE_SECONDS = 30            # no reading for this long -> machine shown OFFLINE
KPI_WINDOW = 15 * 60          # availability / throughput window
SPARK_POINTS = 60


def _throughput_per_hour(rows):
    """Parts/hour from a cumulative counter, tolerating counter resets."""
    if len(rows) < 2:
        return 0.0
    made = 0
    for a, b in zip(rows, rows[1:]):
        d = b["parts_total"] - a["parts_total"]
        made += d if d >= 0 else b["parts_total"]   # reset -> count from zero
    span = rows[-1]["ts"] - rows[0]["ts"]
    return made / span * 3600 if span > 0 else 0.0


def machine_summary(conn, m, now):
    mid = m["id"]
    window = conn.execute(
        "SELECT ts, state, temp_c, vibration, rpm, parts_total FROM readings "
        "WHERE machine_id=? AND ts>=? ORDER BY ts", (mid, now - KPI_WINDOW)).fetchall()
    spark = window[-SPARK_POINTS:]
    latest = conn.execute(
        "SELECT * FROM readings WHERE machine_id=? ORDER BY ts DESC LIMIT 1", (mid,)).fetchone()
    alerts = conn.execute(
        "SELECT rule, severity, message FROM alerts WHERE machine_id=? AND closed_ts IS NULL "
        "ORDER BY severity='critical' DESC, opened_ts", (mid,)).fetchall()

    online = now - m["last_seen"] <= STALE_SECONDS
    running = sum(1 for r in window if r["state"] == "RUNNING")
    return {
        "id": mid, "name": m["name"], "line": m["line"], "kind": m["kind"],
        "status": (latest["state"] if online and latest else "OFFLINE"),
        "last_seen_s": round(now - m["last_seen"], 1),
        "temp_c": latest["temp_c"] if latest else None,
        "vibration": latest["vibration"] if latest else None,
        "rpm": latest["rpm"] if latest else None,
        "parts_total": latest["parts_total"] if latest else None,
        "availability": round(running / len(window), 3) if window else None,
        "parts_per_hour": round(_throughput_per_hour(window), 1),
        "alerts": [dict(a) for a in alerts],
        "spark": {
            "ts": [r["ts"] for r in spark],
            "temp_c": [round(r["temp_c"], 2) for r in spark],
            "vibration": [round(r["vibration"], 2) for r in spark],
        },
    }


def overview(conn, now):
    machines = [machine_summary(conn, m, now) for m in
                conn.execute("SELECT * FROM machines ORDER BY line, id").fetchall()]
    open_alerts = conn.execute(
        "SELECT severity, COUNT(*) n FROM alerts WHERE closed_ts IS NULL GROUP BY severity"
    ).fetchall()
    counts = {r["severity"]: r["n"] for r in open_alerts}
    avail = [m["availability"] for m in machines if m["availability"] is not None]
    rejected_hr = conn.execute(
        "SELECT COUNT(*) FROM rejected WHERE ts>=?", (now - 3600,)).fetchone()[0]
    return {
        "generated_at": now,
        "fleet": {
            "machines": len(machines),
            "online": sum(m["status"] != "OFFLINE" for m in machines),
            "running": sum(m["status"] == "RUNNING" for m in machines),
            "alerts_critical": counts.get("critical", 0),
            "alerts_warning": counts.get("warning", 0),
            "parts_per_hour": round(sum(m["parts_per_hour"] for m in machines), 1),
            "availability": round(sum(avail) / len(avail), 3) if avail else None,
            "rejected_last_hour": rejected_hr,
        },
        "machines": machines,
        "alerts": recent_alerts(conn, limit=25),
    }


def recent_alerts(conn, limit=50, open_only=False):
    sql = ("SELECT a.*, m.name FROM alerts a JOIN machines m ON m.id=a.machine_id "
           + ("WHERE a.closed_ts IS NULL " if open_only else "")
           + "ORDER BY a.closed_ts IS NOT NULL, a.opened_ts DESC LIMIT ?")
    return [dict(r) for r in conn.execute(sql, (limit,)).fetchall()]


def history(conn, machine_id, now, minutes):
    rows = conn.execute(
        "SELECT ts, state, temp_c, vibration, rpm, parts_total FROM readings "
        "WHERE machine_id=? AND ts>=? ORDER BY ts", (machine_id, now - minutes * 60)).fetchall()
    return [dict(r) for r in rows]
