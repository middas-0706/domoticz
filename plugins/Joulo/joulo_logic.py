"""Pure helpers for the Joulo plugin: session normalisation and per-day aggregation.

No Domoticz imports, so everything here is unit testable.

Managed counters only store integers, so values are kept in these units:
  energy      Wh
  ERE credits milli-ERE (1/1000)
  euro        cents
"""
from datetime import datetime, timezone

KEEP = (
    "id", "charger_id", "charger_nickname", "started_at", "ended_at", "kwh", "meter_start_wh", "meter_stop_wh",
    "status", "counts_for_ere", "ere_credits", "estimated_euro", "id_tag", "id_tag_label",
)


def is_waiting(charger):
    """A session is open but the charger is paused, for example a scheduled charge that has not started."""
    return bool(charger.get("is_charging")) and str(charger.get("charging_state") or "").lower() == "paused"


def parse_iso(value):
    """Parse a Joulo ISO 8601 timestamp into an aware datetime, None for empty values."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def compact_session(session):
    """Keep only the fields the plugin and the page need."""
    return {k: session.get(k) for k in KEEP if k in session}


def local_day(started_at, tz=None):
    """Calendar day (YYYY-MM-DD) in the Domoticz server's local time zone (or tz) of a session start."""
    parsed = parse_iso(started_at)
    if parsed is None:
        return None
    return parsed.astimezone(tz).strftime("%Y-%m-%d")


def duration_seconds(session, now=None):
    start = parse_iso(session.get("started_at"))
    if start is None:
        return 0
    end = parse_iso(session.get("ended_at")) or (now or datetime.now(timezone.utc))
    return max(0, int((end - start).total_seconds()))


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def aggregate_days(sessions, charger_id=None, tz=None):
    """Aggregate sessions per local day.

    Returns {day: {"wh": int, "ere_milli": int, "cents": int, "count": int, "seconds": int}}.
    Sums are computed in floating point and rounded once per day, so re-aggregating the full session set always
    gives the same totals (this is what makes the history writes idempotent).
    """
    raw = {}
    for session in sessions:
        if charger_id and session.get("charger_id") != charger_id:
            continue
        day = local_day(session.get("started_at"), tz)
        if day is None:
            continue
        slot = raw.setdefault(day, {"kwh": 0.0, "ere": 0.0, "eur": 0.0, "count": 0, "seconds": 0})
        slot["kwh"] += _num(session.get("kwh"))
        slot["ere"] += _num(session.get("ere_credits"))
        slot["eur"] += _num(session.get("estimated_euro"))
        slot["count"] += 1
        slot["seconds"] += duration_seconds(session)
    return {
        day: {
            "wh": int(round(v["kwh"] * 1000)),
            "ere_milli": int(round(v["ere"] * 1000)),
            "cents": int(round(v["eur"] * 100)),
            "count": v["count"],
            "seconds": v["seconds"],
        }
        for day, v in raw.items()
    }


def cumulative(days, field):
    """[(day, running_total, usage)] ascending by day for one aggregate field."""
    total = 0
    rows = []
    for day in sorted(days):
        usage = days[day][field]
        total += usage
        rows.append((day, total, usage))
    return rows


def totals(days, field):
    return sum(v[field] for v in days.values())


def merge_sessions(cache, sessions):
    """Merge fetched sessions into the id keyed cache. Returns the set of local days that changed."""
    changed = set()
    for session in sessions:
        sid = session.get("id")
        if not sid:
            continue
        compact = compact_session(session)
        old = cache.get(sid)
        if old == compact:
            continue
        for item in (old, compact):
            if item:
                day = local_day(item.get("started_at"))
                if day:
                    changed.add(day)
        cache[sid] = compact
    return changed


def tag_label(session):
    return session.get("id_tag_label") or session.get("id_tag") or ""
