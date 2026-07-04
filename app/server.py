"""Flask-app: dashboard + JSON API voor het busvervoer-monitoringssysteem
van U-OV (Keolis en Transdev, gezamenlijke concessiehouder busvervoer
provincie Utrecht)."""
import hmac
import os
import time
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from . import db
from .gtfs_rt import UtrechtIndex

PROJECT_ROOT = Path(__file__).resolve().parent.parent
app = Flask(
    __name__,
    template_folder=str(PROJECT_ROOT / "templates"),
    static_folder=str(PROJECT_ROOT / "static"),
)
_index = UtrechtIndex()

ON_TIME_MAX_DELAY = 180  # seconden; conform gangbare NL OV-definitie van "op tijd"
VEHICLE_FRESHNESS_SECONDS = 90

# HTTP Basic Auth: staat standaard UIT (handig voor lokaal gebruik). Zet de
# omgevingsvariabele BUS_MONITOR_PASSWORD op de server om dit verplicht te
# maken -- doe dit altijd voordat de app vanaf het internet bereikbaar is.
AUTH_USER = os.environ.get("BUS_MONITOR_USER", "admin")
AUTH_PASSWORD = os.environ.get("BUS_MONITOR_PASSWORD")


@app.before_request
def require_auth():
    if not AUTH_PASSWORD:
        return None
    auth = request.authorization
    valid = (
        auth is not None
        and hmac.compare_digest(auth.username, AUTH_USER)
        and hmac.compare_digest(auth.password, AUTH_PASSWORD)
    )
    if not valid:
        return Response(
            "Authenticatie vereist", 401, {"WWW-Authenticate": 'Basic realm="Bus Monitor"'}
        )
    return None


def route_meta(route_id):
    r = _index.routes.get(route_id, {})
    return {
        "route_id": route_id,
        "short_name": r.get("short_name", "?"),
        "long_name": r.get("long_name", ""),
        "agency_name": r.get("agency_name", "?"),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/meta")
def api_meta():
    agencies = sorted({r["agency_name"] for r in _index.routes.values()})
    routes = [
        {"route_id": rid, **route_meta(rid)}
        for rid in _index.routes
    ]
    return jsonify({"agencies": agencies, "routes": routes, "route_count": len(routes)})


@app.route("/api/vehicles")
def api_vehicles():
    cutoff = int(time.time()) - VEHICLE_FRESHNESS_SECONDS
    conn = db.get_conn()
    try:
        # OVapi laat het vehicle_id-veld vaak leeg; val in dat geval terug op
        # trip_id als unieke identiteit per voertuig/rit zodat we niet alle
        # "vehicle_id-loze" bussen op elkaar dedupliceren.
        rows = conn.execute(
            """
            SELECT vp.*, COALESCE(NULLIF(vp.vehicle_id, ''), vp.trip_id) AS ident
            FROM vehicle_positions vp
            JOIN (
                SELECT COALESCE(NULLIF(vehicle_id, ''), trip_id) AS ident, MAX(fetched_at) AS max_fetched
                FROM vehicle_positions
                WHERE fetched_at >= ? AND trip_id IS NOT NULL
                GROUP BY ident
            ) latest ON COALESCE(NULLIF(vp.vehicle_id, ''), vp.trip_id) = latest.ident
                    AND vp.fetched_at = latest.max_fetched
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    # Laatst bekende vertraging per trip erbij zoeken voor kleurcodering op de kaart.
    conn = db.get_conn()
    try:
        delay_rows = conn.execute(
            """
            SELECT trip_id, arrival_delay, departure_delay
            FROM trip_delays
            WHERE fetched_at >= ?
            GROUP BY trip_id
            HAVING fetched_at = MAX(fetched_at)
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    delay_by_trip = {
        r["trip_id"]: r["arrival_delay"] if r["arrival_delay"] is not None else r["departure_delay"]
        for r in delay_rows
    }

    vehicles = []
    for r in rows:
        if not _index.is_relevant_route(r["route_id"]):
            # Kan voorkomen vlak na het herbouwen van de statische index
            # (build_static_index.py), als de database nog verse rijen van
            # een inmiddels uitgesloten lijn bevat. Verdwijnt vanzelf zodra
            # die rijen buiten het freshness-venster vallen.
            continue
        meta = route_meta(r["route_id"])
        vehicles.append({
            "ident": r["ident"],
            "vehicle_id": r["vehicle_id"],
            "trip_id": r["trip_id"],
            "route_id": r["route_id"],
            "route_short_name": meta["short_name"],
            "agency_name": meta["agency_name"],
            "lat": r["lat"],
            "lon": r["lon"],
            "bearing": r["bearing"],
            "speed": r["speed"],
            "delay_seconds": delay_by_trip.get(r["trip_id"]),
            "fetched_at": r["fetched_at"],
        })
    return jsonify({"vehicles": vehicles, "count": len(vehicles), "as_of": int(time.time())})


@app.route("/api/alerts")
def api_alerts():
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE active=1 ORDER BY last_seen DESC"
        ).fetchall()
    finally:
        conn.close()
    alerts = []
    for r in rows:
        route_ids = [rid for rid in (r["route_ids"] or "").split(",") if rid]
        alerts.append({
            "alert_id": r["alert_id"],
            "header": r["header"],
            "description": r["description"],
            "effect": r["effect"],
            "routes": [route_meta(rid) for rid in route_ids],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
        })
    return jsonify({"alerts": alerts, "count": len(alerts)})


@app.route("/api/stats")
def api_stats():
    """Punctualiteit per operator en per route, over ruwe data (laatste 14 dagen)
    aangevuld met opgerolde dagstatistieken voor oudere periodes."""
    conn = db.get_conn()
    try:
        raw = conn.execute(
            """
            SELECT route_id,
                   COUNT(*) AS sample_count,
                   SUM(CASE WHEN COALESCE(arrival_delay, departure_delay, 0) <= ? THEN 1 ELSE 0 END) AS on_time_count,
                   AVG(COALESCE(arrival_delay, departure_delay, 0)) AS avg_delay_seconds,
                   MAX(COALESCE(arrival_delay, departure_delay, 0)) AS max_delay_seconds
            FROM trip_delays
            GROUP BY route_id
            """,
            (ON_TIME_MAX_DELAY,),
        ).fetchall()
        rolled = conn.execute(
            """
            SELECT route_id,
                   SUM(sample_count) AS sample_count,
                   SUM(on_time_count) AS on_time_count,
                   AVG(avg_delay_seconds) AS avg_delay_seconds,
                   MAX(max_delay_seconds) AS max_delay_seconds
            FROM route_stats_daily
            GROUP BY route_id
            """
        ).fetchall()
    finally:
        conn.close()

    by_route = {}
    for r in list(raw) + list(rolled):
        rid = r["route_id"]
        entry = by_route.setdefault(rid, {"sample_count": 0, "on_time_count": 0, "avg_sum": 0.0, "max_delay": 0})
        entry["sample_count"] += r["sample_count"] or 0
        entry["on_time_count"] += r["on_time_count"] or 0
        entry["avg_sum"] += (r["avg_delay_seconds"] or 0) * (r["sample_count"] or 0)
        entry["max_delay"] = max(entry["max_delay"], r["max_delay_seconds"] or 0)

    per_route = []
    per_agency = {}
    for rid, e in by_route.items():
        if e["sample_count"] == 0:
            continue
        meta = route_meta(rid)
        avg_delay = e["avg_sum"] / e["sample_count"]
        on_time_pct = 100.0 * e["on_time_count"] / e["sample_count"]
        per_route.append({
            **meta,
            "sample_count": e["sample_count"],
            "on_time_pct": round(on_time_pct, 1),
            "avg_delay_seconds": round(avg_delay, 1),
            "max_delay_seconds": e["max_delay"],
        })
        agg = per_agency.setdefault(meta["agency_name"], {"sample_count": 0, "on_time_count": 0, "avg_sum": 0.0})
        agg["sample_count"] += e["sample_count"]
        agg["on_time_count"] += e["on_time_count"]
        agg["avg_sum"] += avg_delay * e["sample_count"]

    agency_stats = []
    for name, a in per_agency.items():
        if a["sample_count"] == 0:
            continue
        agency_stats.append({
            "agency_name": name,
            "sample_count": a["sample_count"],
            "on_time_pct": round(100.0 * a["on_time_count"] / a["sample_count"], 1),
            "avg_delay_seconds": round(a["avg_sum"] / a["sample_count"], 1),
        })

    per_route.sort(key=lambda x: -x["sample_count"])
    agency_stats.sort(key=lambda x: -x["sample_count"])

    return jsonify({
        "on_time_threshold_seconds": ON_TIME_MAX_DELAY,
        "per_agency": agency_stats,
        "per_route": per_route,
    })


CANCELLATION_RANGE_DAYS = {"today": 1, "week": 7, "2weeks": 14, "30d": 30}
EARLIEST_POSSIBLE_DATE = "2000-01-01"  # ondergrens voor range=all
WEEKDAY_NAMES_NL = ["Maandag", "Dinsdag", "Woensdag", "Donderdag", "Vrijdag", "Zaterdag", "Zondag"]


@app.route("/uitval")
def uitval_page():
    return render_template("cancellations.html")


def _cancellation_date_bounds(range_key):
    today_str = date.today().isoformat()
    if range_key == "all":
        return EARLIEST_POSSIBLE_DATE, today_str
    days = CANCELLATION_RANGE_DAYS.get(range_key, 1)
    since_date = (date.today() - timedelta(days=days - 1)).isoformat()
    return since_date, today_str


@app.route("/api/cancellations")
def api_cancellations():
    """Aantal uitgevallen (CANCELED) ritten over een gekozen periode.

    'Uitvalpercentage' wordt berekend als vervallen / (vervallen + daadwerkelijk
    gereden), waarbij 'gereden' is afgeleid uit de realtime feed (ritten waarvoor
    we minstens één halte-update zagen). Dit is een praktische benadering op
    basis van wat de realtime feeds daadwerkelijk rapporteren, geen exacte
    telling tegen de volledige dienstregeling."""
    range_key = request.args.get("range", "today")
    # Bovengrens op vandaag: agencies melden soms al vervallen ritten voor
    # morgen vooruit, die horen niet thuis in een periode t/m vandaag.
    since_date, today_str = _cancellation_date_bounds(range_key)

    conn = db.get_conn()
    try:
        canceled_rows = conn.execute(
            """SELECT service_date, route_id, start_time, COUNT(*) AS cnt
               FROM trip_cancellations WHERE service_date >= ? AND service_date <= ?
               GROUP BY service_date, route_id, start_time""",
            (since_date, today_str),
        ).fetchall()
        ran_rows = conn.execute(
            """SELECT r.service_date, r.route_id, COUNT(*) AS cnt
               FROM trips_ran_daily r
               WHERE r.service_date >= ? AND r.service_date <= ?
                 AND NOT EXISTS (
                     SELECT 1 FROM trip_cancellations c
                     WHERE c.trip_id = r.trip_id AND c.service_date = r.service_date
                 )
               GROUP BY r.service_date, r.route_id""",
            (since_date, today_str),
        ).fetchall()
    finally:
        conn.close()

    daily = {}
    route_canceled = {}
    route_ran = {}
    weekday_canceled = [0] * 7
    weekday_ran = [0] * 7
    hour_canceled = [0] * 24
    for r in canceled_rows:
        d = daily.setdefault(r["service_date"], {"canceled": 0, "ran": 0})
        d["canceled"] += r["cnt"]
        route_canceled[r["route_id"]] = route_canceled.get(r["route_id"], 0) + r["cnt"]
        weekday_canceled[date.fromisoformat(r["service_date"]).weekday()] += r["cnt"]
        if r["start_time"]:
            try:
                hour = int(r["start_time"].split(":")[0]) % 24
                hour_canceled[hour] += r["cnt"]
            except (ValueError, IndexError):
                pass
    for r in ran_rows:
        d = daily.setdefault(r["service_date"], {"canceled": 0, "ran": 0})
        d["ran"] += r["cnt"]
        route_ran[r["route_id"]] = route_ran.get(r["route_id"], 0) + r["cnt"]
        weekday_ran[date.fromisoformat(r["service_date"]).weekday()] += r["cnt"]

    daily_list = []
    for d in sorted(daily.keys()):
        c, r = daily[d]["canceled"], daily[d]["ran"]
        total = c + r
        daily_list.append({
            "date": d, "canceled": c, "ran": r,
            "cancellation_pct": round(100.0 * c / total, 1) if total else 0.0,
        })

    total_canceled = sum(d["canceled"] for d in daily.values())
    total_ran = sum(d["ran"] for d in daily.values())
    total = total_canceled + total_ran

    per_route = []
    per_agency_acc = {}
    for rid in set(route_canceled) | set(route_ran):
        c = route_canceled.get(rid, 0)
        if c == 0:
            continue  # alleen lijnen met minstens 1 vervallen rit tonen
        r = route_ran.get(rid, 0)
        meta = route_meta(rid)
        rtotal = c + r
        per_route.append({
            **meta, "canceled": c, "ran": r,
            "cancellation_pct": round(100.0 * c / rtotal, 1) if rtotal else 0.0,
        })
        agg = per_agency_acc.setdefault(meta["agency_name"], {"canceled": 0, "ran": 0})
        agg["canceled"] += c
        agg["ran"] += r
    per_route.sort(key=lambda x: -x["canceled"])

    per_agency = []
    for name, a in per_agency_acc.items():
        atotal = a["canceled"] + a["ran"]
        per_agency.append({
            "agency_name": name, "canceled": a["canceled"], "ran": a["ran"],
            "cancellation_pct": round(100.0 * a["canceled"] / atotal, 1) if atotal else 0.0,
        })
    per_agency.sort(key=lambda x: -x["canceled"])

    per_weekday = []
    for i, name in enumerate(WEEKDAY_NAMES_NL):
        c, r = weekday_canceled[i], weekday_ran[i]
        wtotal = c + r
        per_weekday.append({
            "weekday": name, "canceled": c, "ran": r,
            "cancellation_pct": round(100.0 * c / wtotal, 1) if wtotal else 0.0,
        })

    per_hour = [{"hour": h, "canceled": hour_canceled[h]} for h in range(24)]

    return jsonify({
        "range": range_key,
        "since_date": since_date,
        "until_date": today_str,
        "total_canceled": total_canceled,
        "total_ran": total_ran,
        "cancellation_pct": round(100.0 * total_canceled / total, 1) if total else 0.0,
        "daily": daily_list,
        "per_route": per_route,
        "per_agency": per_agency,
        "per_weekday": per_weekday,
        "per_hour": per_hour,
    })


@app.route("/api/cancellations/trips")
def api_cancellation_trips():
    """Drill-down: individuele vervallen ritten, gepagineerd en optioneel
    gefilterd op lijn/operator, voor wie de ruwe data wil inzien."""
    range_key = request.args.get("range", "today")
    since_date, today_str = _cancellation_date_bounds(range_key)
    route_id = request.args.get("route_id")
    agency = request.args.get("agency")
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = max(int(request.args.get("offset", 0)), 0)

    route_ids_filter = None
    if agency:
        route_ids_filter = {rid for rid, r in _index.routes.items() if r["agency_name"] == agency}

    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT trip_id, service_date, route_id, start_time, first_seen, last_seen
               FROM trip_cancellations
               WHERE service_date >= ? AND service_date <= ?
               ORDER BY service_date DESC, start_time DESC""",
            (since_date, today_str),
        ).fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        if route_id and r["route_id"] != route_id:
            continue
        if route_ids_filter is not None and r["route_id"] not in route_ids_filter:
            continue
        meta = route_meta(r["route_id"])
        items.append({
            "trip_id": r["trip_id"],
            "service_date": r["service_date"],
            "start_time": r["start_time"],
            **meta,
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
        })

    return jsonify({
        "total": len(items),
        "limit": limit,
        "offset": offset,
        "items": items[offset:offset + limit],
    })


def create_app():
    db.init_db()
    return app


if __name__ == "__main__":
    from .collector import start_scheduler
    create_app()
    start_scheduler()
    # debug/reloader uit: dit is een langlopende dataverzamelaar, geen reload gewenst.
    app.run(host="127.0.0.1", port=5151, debug=False, use_reloader=False)
