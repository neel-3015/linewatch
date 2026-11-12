"""Shop-floor simulator: pretends to be a fleet of networked machines.

Each machine streams temperature, vibration, spindle RPM and a parts counter.
Scripted scenarios give the dashboard something real to catch:

  CNC-03    bearing wear: vibration creeps up until the trend alert fires,
            then the warning and critical alerts, then maintenance resets it
  PRESS-02  periodic overheating past the temperature limits
  any       random short FAULTs, idle periods, and a counter reset

A small fraction of readings is deliberately malformed (--bad-rate) to show
the server rejecting bad data instead of storing it.

    python -m linewatch.simulator --url http://127.0.0.1:8000 --key $LINEWATCH_API_KEY
"""
import argparse
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request

FLEET = [
    # id,        name,               line,     kind,    base temp, base vib, rpm,  parts/min
    ("CNC-01",   "CNC Mill 1",       "Line A", "cnc",   55, 2.2, 8000, 6),
    ("CNC-02",   "CNC Mill 2",       "Line A", "cnc",   57, 2.4, 8200, 6),
    ("CNC-03",   "CNC Mill 3",       "Line A", "cnc",   56, 2.5, 8100, 6),
    ("CNC-04",   "CNC Lathe 4",      "Line A", "lathe", 52, 1.9, 3500, 9),
    ("PRESS-01", "Stamping Press 1", "Line B", "press", 62, 3.8, 120, 30),
    ("PRESS-02", "Stamping Press 2", "Line B", "press", 64, 3.9, 120, 30),
    ("WELD-01",  "Robotic Welder 1", "Line B", "weld",  70, 1.5, 0, 12),
    ("PAINT-01", "Paint Booth 1",    "Line B", "paint", 38, 0.8, 1800, 15),
]


class Machine:
    def __init__(self, spec, rng):
        (self.id, self.name, self.line, self.kind, self.base_t, self.base_v,
         self.base_rpm, self.rate) = spec
        self.rng = rng
        self.state = "RUNNING"
        self.state_left = 0.0       # seconds remaining in a non-RUNNING state
        self.temp = self.base_t
        self.wear = 0.0             # extra vibration from bearing wear (mm/s)
        self.parts = rng.randint(1000, 50000)
        self.parts_frac = 0.0
        self.t = 0.0                # simulated seconds since start

    def _enter(self, state, seconds):
        self.state, self.state_left = state, seconds

    def step(self, dt):
        self.t += dt
        rng = self.rng

        if self.state != "RUNNING":
            self.state_left -= dt
            if self.state_left <= 0:
                if self.state == "MAINTENANCE":
                    self.wear = 0.0
                self._enter("RUNNING", 0)
        elif rng.random() < 0.0015 * dt:
            self._enter("FAULT", rng.uniform(8, 25))
        elif rng.random() < 0.002 * dt:
            self._enter("IDLE", rng.uniform(10, 40))

        running = self.state == "RUNNING"

        # scenario: bearing wear on CNC-03 (~0.9 mm/s per minute)
        if self.id == "CNC-03" and running:
            self.wear += 0.015 * dt
            if self.base_v + self.wear > 12.5:
                self._enter("MAINTENANCE", 45)

        # temperature: first-order approach to a target, plus noise
        target = self.base_t if running else self.base_t - 15
        if self.id == "PRESS-02" and running:
            # overheat cycle: every 6 minutes, ~90 s excursion up to ~100 C
            phase = (self.t % 360) / 360
            if 0.55 < phase < 0.80:
                target = self.base_t + 40 * math.sin((phase - 0.55) / 0.25 * math.pi)
        self.temp += (target - self.temp) * min(1.0, 0.08 * dt) + rng.gauss(0, 0.25)

        vib = (self.base_v + self.wear) * (1 if running else 0.15) + abs(rng.gauss(0, 0.15))
        rpm = self.base_rpm * (1 + rng.gauss(0, 0.01)) if running else 0.0

        if running:
            self.parts_frac += self.rate / 60 * dt
            whole = int(self.parts_frac)
            self.parts += whole
            self.parts_frac -= whole
        if rng.random() < 0.0002 * dt:      # PLC reboot -> counter reset
            self.parts = 0

        return {
            "machine_id": self.id, "name": self.name, "line": self.line, "kind": self.kind,
            "ts": time.time(), "state": self.state,
            "temp_c": round(self.temp, 2), "vibration": round(vib, 3),
            "rpm": round(max(rpm, 0.0), 1), "parts_total": self.parts,
        }


def corrupt(reading, rng):
    """Produce the kinds of garbage real sensors send."""
    r = dict(reading)
    kind = rng.choice(["range", "type", "future", "missing", "state"])
    if kind == "range":
        r["temp_c"] = 999.0
    elif kind == "type":
        r["vibration"] = "n/a"
    elif kind == "future":
        r["ts"] = r["ts"] + 3600
    elif kind == "missing":
        r.pop("rpm")
    else:
        r["state"] = "ON_FIRE"
    return r


def post(url, key, batch, timeout=5):
    req = urllib.request.Request(
        url.rstrip("/") + "/api/ingest", data=json.dumps(batch).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": key}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.environ.get("LINEWATCH_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--key", default=os.environ.get("LINEWATCH_API_KEY"))
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between batches")
    ap.add_argument("--bad-rate", type=float, default=0.01, help="fraction of corrupted readings")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--count", type=int, default=0, help="stop after N batches (0 = forever)")
    args = ap.parse_args(argv)
    if not args.key:
        ap.error("--key or LINEWATCH_API_KEY is required")

    rng = random.Random(args.seed)
    fleet = [Machine(spec, rng) for spec in FLEET]
    pending, sent = [], 0
    print(f"simulating {len(fleet)} machines -> {args.url} every {args.interval}s", flush=True)

    while args.count == 0 or sent < args.count:
        batch = [m.step(args.interval) for m in fleet]
        batch = [corrupt(r, rng) if rng.random() < args.bad_rate else r for r in batch]
        pending = (pending + batch)[-400:]      # buffer through short outages
        try:
            res = post(args.url, args.key, pending)
            for ev in res.get("alerts", []):
                print(f"  ALERT {ev['event']:>9} {ev['machine_id']:<9} {ev['rule']:<10} "
                      f"{ev.get('message', '')}", flush=True)
            for rej in res.get("rejected", []):
                print(f"  rejected: {rej['reason']}", flush=True)
            pending = []
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit("server rejected the API key")
            if e.code == 422:       # whole batch invalid -> it's garbage, don't retry it
                pending = []
            print(f"  server error {e.code}; will retry", flush=True)
        except (urllib.error.URLError, OSError) as e:
            print(f"  cannot reach server ({e}); buffering {len(pending)} readings", flush=True)
        sent += 1
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
