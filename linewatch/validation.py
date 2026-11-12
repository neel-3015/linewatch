"""Validation for telemetry coming off the shop floor.

Devices are untrusted input: every field is type-checked, range-checked against
physically plausible limits, and timestamp-checked before it touches the
database. Anything that fails is written to the dead-letter table, not dropped
silently, so bad sensors show up instead of quietly corrupting the dashboard.
"""
import math
import re

STATES = {"RUNNING", "IDLE", "FAULT", "MAINTENANCE"}

# field -> (allowed types, min, max)
NUMERIC = {
    "temp_c":      ((int, float), -40.0, 200.0),
    "vibration":   ((int, float), 0.0, 100.0),     # mm/s RMS
    "rpm":         ((int, float), 0.0, 30000.0),
    "parts_total": ((int,),       0, 10**9),
}
OPTIONAL_TEXT = {"name": 64, "line": 32, "kind": 32}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

MAX_FUTURE_SKEW = 120        # seconds a device clock may run ahead of ours
MAX_AGE = 24 * 3600          # older readings are rejected (replay / stale buffer)


class ValidationError(ValueError):
    pass


def _is_number(v, types):
    # bool is a subclass of int in Python -- never accept True/False as a number
    return isinstance(v, types) and not isinstance(v, bool)


def validate(raw, now):
    """Return a normalized reading dict, or raise ValidationError."""
    if not isinstance(raw, dict):
        raise ValidationError("reading must be a JSON object")

    mid = raw.get("machine_id")
    if not isinstance(mid, str) or not ID_RE.match(mid):
        raise ValidationError("machine_id must be 1-32 chars of [A-Za-z0-9_-]")

    ts = raw.get("ts")
    if not _is_number(ts, (int, float)):
        raise ValidationError("ts must be a unix timestamp")
    try:
        ts = float(ts)                       # huge ints overflow here
    except OverflowError:
        raise ValidationError("ts out of range") from None
    if not math.isfinite(ts):
        raise ValidationError("ts must be a unix timestamp")
    if ts > now + MAX_FUTURE_SKEW:
        raise ValidationError("ts is in the future")
    if ts < now - MAX_AGE:
        raise ValidationError("ts is older than 24h")

    state = raw.get("state")
    if state not in STATES:
        raise ValidationError(f"state must be one of {sorted(STATES)}")

    out = {"machine_id": mid, "ts": ts, "state": state}
    for field, (types, lo, hi) in NUMERIC.items():
        v = raw.get(field)
        if not _is_number(v, types):
            raise ValidationError(f"{field} missing or wrong type")
        if isinstance(v, float) and not math.isfinite(v):
            raise ValidationError(f"{field} is NaN/inf")
        if not lo <= v <= hi:
            raise ValidationError(f"{field}={v} outside plausible range [{lo}, {hi}]")
        out[field] = v

    for field, maxlen in OPTIONAL_TEXT.items():
        v = raw.get(field, mid if field == "name" else "unknown")
        if not isinstance(v, str) or not 0 < len(v) <= maxlen:
            raise ValidationError(f"{field} must be a string of 1-{maxlen} chars")
        out[field] = v

    return out
