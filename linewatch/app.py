"""Flask app: authenticated ingest API, read API, and the dashboard."""
import hmac
import json
import os
import time
from pathlib import Path

from flask import Flask, g, jsonify, request, send_from_directory

from . import db, queries, rules
from .validation import ValidationError, validate

STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_BATCH = 500
PRUNE_EVERY = 60  # seconds


def create_app(db_path=None, api_key=None, clock=time.time):
    db_path = db_path or os.environ.get("LINEWATCH_DB", "linewatch.db")
    api_key = api_key if api_key is not None else os.environ.get("LINEWATCH_API_KEY", "")
    if len(api_key) < 16:
        # fail closed: refuse to start with no key or a weak one
        raise RuntimeError("LINEWATCH_API_KEY must be set (16+ characters)")

    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    state = {"last_prune": 0.0}

    def conn():
        if "db" not in g:
            g.db = db.connect(db_path)
        return g.db

    @app.teardown_appcontext
    def _close(_exc):
        c = g.pop("db", None)
        if c is not None:
            c.close()

    @app.after_request
    def _headers(resp):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/ingest")
    def ingest():
        supplied = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(supplied.encode(), api_key.encode()):
            return jsonify(error="invalid or missing API key"), 401

        body = request.get_json(silent=True)
        if isinstance(body, dict):
            body = [body]
        if not isinstance(body, list) or not body:
            return jsonify(error="body must be a reading object or a non-empty list"), 400
        if len(body) > MAX_BATCH:
            return jsonify(error=f"batch larger than {MAX_BATCH}"), 413

        now = clock()
        c = conn()
        accepted, duplicates, rejected, events = 0, 0, [], []
        c.execute("BEGIN IMMEDIATE")
        try:
            for i, raw in enumerate(body):
                try:
                    r = validate(raw, now)
                except ValidationError as e:
                    rejected.append({"index": i, "reason": str(e)})
                    c.execute("INSERT INTO rejected(ts, reason, payload) VALUES (?,?,?)",
                              (now, str(e), json.dumps(raw, default=str)[:1000]))
                    continue
                c.execute(
                    "INSERT INTO machines(id, name, line, kind, first_seen, last_seen) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "name=excluded.name, line=excluded.line, kind=excluded.kind, "
                    "last_seen=MAX(last_seen, excluded.last_seen)",
                    (r["machine_id"], r["name"], r["line"], r["kind"], now, now))
                inserted = c.execute(
                    "INSERT INTO readings(machine_id, ts, state, temp_c, vibration, rpm, parts_total) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(machine_id, ts) DO NOTHING",
                    (r["machine_id"], r["ts"], r["state"], r["temp_c"], r["vibration"],
                     r["rpm"], r["parts_total"])).rowcount
                if not inserted:          # duplicate -> already processed, don't re-alert
                    duplicates += 1
                    continue
                events += rules.evaluate(c, r)
                accepted += 1
            if now - state["last_prune"] > PRUNE_EVERY:
                db.prune(c, now)
                state["last_prune"] = now
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

        status = 200 if accepted or duplicates else 422
        return jsonify(accepted=accepted, duplicates=duplicates, rejected=rejected,
                       alerts=events), status

    @app.get("/api/overview")
    def overview():
        return jsonify(queries.overview(conn(), clock()))

    @app.get("/api/alerts")
    def alerts():
        open_only = request.args.get("open") == "1"
        return jsonify(queries.recent_alerts(conn(), limit=200, open_only=open_only))

    @app.get("/api/machines/<machine_id>/history")
    def history(machine_id):
        minutes = request.args.get("minutes", default=15, type=int)
        minutes = max(1, min(minutes, 24 * 60))
        return jsonify(queries.history(conn(), machine_id, clock(), minutes))

    @app.get("/healthz")
    def healthz():
        conn().execute("SELECT 1")
        return jsonify(ok=True)

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    return app


def wsgi():
    """Entry point for gunicorn: gunicorn 'linewatch.app:wsgi()'"""
    return create_app()


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
