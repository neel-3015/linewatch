import pytest

from linewatch import db, rules
from linewatch.app import create_app
from linewatch.queries import _throughput_per_hour
from linewatch.validation import ValidationError, validate

KEY = "test-key-0123456789abcdef"
NOW = 1_800_000_000.0


def reading(**kw):
    r = {"machine_id": "CNC-01", "ts": NOW, "state": "RUNNING", "temp_c": 60.0,
         "vibration": 2.0, "rpm": 8000.0, "parts_total": 100}
    r.update(kw)
    return r


@pytest.fixture
def clock():
    class Clock:
        t = NOW
        def __call__(self):
            return self.t
    return Clock()


@pytest.fixture
def client(tmp_path, clock):
    app = create_app(db_path=str(tmp_path / "t.db"), api_key=KEY, clock=clock)
    return app.test_client()


def send(client, body, key=KEY):
    return client.post("/api/ingest", json=body, headers={"X-API-Key": key})


# ---------- validation ----------

def test_valid_reading_normalized():
    out = validate(reading(), NOW)
    assert out["machine_id"] == "CNC-01" and out["name"] == "CNC-01" and out["line"] == "unknown"


@pytest.mark.parametrize("bad, why", [
    (reading(temp_c=999), "outside plausible range"),
    (reading(vibration="n/a"), "wrong type"),
    (reading(rpm=True), "wrong type"),                 # bool is not a number
    (reading(parts_total=1.5), "wrong type"),
    (reading(temp_c=float("nan")), "NaN"),
    (reading(ts=NOW + 3600), "future"),
    (reading(ts=NOW - 2 * 86400), "older than 24h"),
    (reading(state="ON_FIRE"), "state must be"),
    (reading(machine_id="x; DROP TABLE"), "machine_id"),
    ({k: v for k, v in reading().items() if k != "rpm"}, "rpm missing"),
    ("not an object", "JSON object"),
])
def test_invalid_readings_rejected(bad, why):
    with pytest.raises(ValidationError, match=why):
        validate(bad, NOW)


# ---------- auth / API ----------

def test_refuses_to_start_without_strong_key(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(db_path=str(tmp_path / "x.db"), api_key="short")


def test_ingest_requires_key(client):
    assert send(client, reading(), key="wrong-key-wrong-key").status_code == 401
    assert client.post("/api/ingest", json=reading()).status_code == 401


def test_ingest_batch_partial_reject_goes_to_dead_letter(client):
    r = send(client, [reading(), reading(machine_id="CNC-02", temp_c=999)])
    body = r.get_json()
    assert r.status_code == 200 and body["accepted"] == 1
    assert body["rejected"][0]["index"] == 1
    fleet = client.get("/api/overview").get_json()["fleet"]
    assert fleet["machines"] == 1 and fleet["rejected_last_hour"] == 1


def test_all_invalid_is_422(client):
    assert send(client, [reading(temp_c=-100)]).status_code == 422


def test_bad_body_shapes(client):
    assert send(client, []).status_code == 400
    assert send(client, "hello").status_code == 400
    assert send(client, [reading()] * 501).status_code == 413


def test_security_headers(client):
    h = client.get("/").headers
    assert "default-src 'self'" in h["Content-Security-Policy"]
    assert h["X-Content-Type-Options"] == "nosniff"


# ---------- alert rules ----------

def test_overtemp_open_escalate_hysteresis_close(client, clock):
    def at(dt, temp):
        clock.t = NOW + dt
        return send(client, reading(ts=clock.t, temp_c=temp)).get_json()["alerts"]

    assert at(0, 80) == []
    assert at(2, 88)[0]["event"] == "opened"                    # warning
    assert at(4, 97)[0]["event"] == "escalated"                 # critical
    assert at(6, 80) == []                                      # inside hysteresis band (76.5-85)
    assert at(8, 75)[0]["event"] == "closed"                    # below 85 * 0.9
    alerts = client.get("/api/alerts").get_json()
    assert len(alerts) == 1 and alerts[0]["severity"] == "critical" and alerts[0]["peak"] == 97


def test_fault_state_alert(client, clock):
    ev = send(client, reading(state="FAULT")).get_json()["alerts"]
    assert [e["rule"] for e in ev] == ["FAULT"]
    assert client.get("/api/overview").get_json()["fleet"]["alerts_critical"] == 1
    clock.t += 2
    ev = send(client, reading(ts=clock.t)).get_json()["alerts"]
    assert {"event": "closed", "machine_id": "CNC-01", "rule": "FAULT", "severity": "critical"} in ev


def test_vibration_trend_fires_before_threshold(client, clock):
    fired = []
    for i in range(30):                         # +0.03 mm/s every 2 s = 0.9 mm/s/min
        clock.t = NOW + 2 * i
        vib = 2.0 + 0.03 * i
        for ev in send(client, reading(ts=clock.t, vibration=vib)).get_json()["alerts"]:
            fired.append((ev["rule"], vib))
    assert fired and fired[0][0] == "VIB_TREND"
    assert fired[0][1] < rules.THRESHOLDS[1].warn   # early warning, still below 7.1


def test_slope_math():
    pts = [(t, 1.0 + 0.01 * t) for t in range(0, 100, 5)]
    assert rules.slope_per_minute(pts) == pytest.approx(0.6)
    assert rules.slope_per_minute([(0, 1), (0, 2)]) == 0.0


# ---------- KPIs / storage ----------

def test_throughput_handles_counter_reset():
    rows = [{"ts": 0, "parts_total": 100}, {"ts": 1800, "parts_total": 130},
            {"ts": 2700, "parts_total": 10}, {"ts": 3600, "parts_total": 40}]
    assert _throughput_per_hour(rows) == pytest.approx(30 + 10 + 30)


def test_machine_goes_offline_when_stale(client, clock):
    send(client, reading())
    assert client.get("/api/overview").get_json()["machines"][0]["status"] == "RUNNING"
    clock.t += 31
    assert client.get("/api/overview").get_json()["machines"][0]["status"] == "OFFLINE"


def test_prune_retention(tmp_path):
    c = db.connect(str(tmp_path / "p.db"))
    c.execute("INSERT INTO machines VALUES ('M','M','L','k',0,0)")
    c.execute("INSERT INTO readings(machine_id,ts,state,temp_c,vibration,rpm,parts_total) "
              "VALUES ('M',?,'RUNNING',1,1,1,1),('M',?,'RUNNING',1,1,1,1)", (NOW - 90000, NOW))
    assert db.prune(c, NOW) == 1
    assert c.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1


def test_huge_integer_timestamp_is_rejected_not_500(client):
    r = send(client, [reading(ts=10**400)])
    assert r.status_code == 422 and "out of range" in r.get_json()["rejected"][0]["reason"]


def test_resent_reading_is_stored_once_and_not_realerted(client):
    first = send(client, reading(temp_c=90)).get_json()
    again = send(client, reading(temp_c=90))          # same machine + ts: device retry
    assert first["accepted"] == 1 and len(first["alerts"]) == 1
    assert again.status_code == 200
    assert again.get_json() == {"accepted": 0, "duplicates": 1, "rejected": [], "alerts": []}


def test_trend_ignores_fast_noise(client, clock):
    # 40 noisy readings in 10 s: too short a span to call a trend
    import random
    rng = random.Random(1)
    for i in range(40):
        clock.t = NOW + 0.25 * i
        ev = send(client, reading(ts=clock.t, vibration=2 + rng.uniform(0, 0.4))).get_json()["alerts"]
        assert not [e for e in ev if e["rule"] == "VIB_TREND"]
