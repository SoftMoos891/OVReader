"""Publieke 'lite'-versie van de OV-monitor: alleen actuele storingen en
uitvalcijfers, zonder Basic Auth, bedoeld voor breed publiek gebruik naast
de volledige (met Basic Auth afgeschermde) OV-reader in app/server.py.

Draait als eigen proces/systemd-service (zie deploy/utrecht-bus-lite.service)
en wordt via de reverse proxy (nginx, zie deploy/nginx-lite.conf) op hetzelfde
domein onder /lite ontsloten -- geen apart subdomein, wel volledige
procesisolatie van de hoofd-webservice.

Bewust GEEN import van .server of .gtfs_rt.UtrechtIndex: server.py
instantieert bij import al een UtrechtIndex() EN een Timetable(), en die
laatste laadt data/utrecht_stop_times.json (~117 MB op schijf) voor de
haltezoeker/vertrektijden -- data die deze lite-scope (storingen + uitval)
nooit nodig heeft. Deze module leest daarom alleen het kleine
utrecht_routes.json (~23 KB) in, zodat het hele proces met een fractie van
het geheugen van de hoofd-webservice kan draaien."""
import json
import time
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template

from . import db
from .collector import FETCH_INTERVAL_SECONDS
from .concession_mapping import TRANSDEV_TRAM

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Aantal dagen voor de "uitvalpercentage per dag"-trendgrafiek op /lite --
# vast en niet instelbaar (geen ?range=-parameter zoals bij de volledige
# /uitval-dashboard), consistent met de "basale" lite-scope.
CHART_DAYS = 30

# Zelfde definitie als app/server.py's /api/health.
VEHICLE_FRESHNESS_SECONDS = 90
CANCELLATION_STALE_AFTER_SECONDS = 26 * 3600  # ruim over 24u: uitval komt sporadisch binnen, geen 30s-heartbeat
RAIL_ALERTS_STALE_AFTER_SECONDS = 600  # zelfde marge als in app/server.py (job draait elke 2 min)

app = Flask(
    __name__,
    template_folder=str(PROJECT_ROOT / "templates"),
    static_folder=str(PROJECT_ROOT / "static"),
    static_url_path="/lite/static",
)


class _LiteRouteIndex:
    """Minimale route-index voor de lite-app: leest alleen utrecht_routes.json
    in (lijnnaam/operator-lookup) -- niet de trip-/halte-mappings uit
    gtfs_rt.UtrechtIndex, die alleen nodig zijn om trip_id's te resolven bij
    het ophalen van de realtime feed. De rijen die deze app leest (alerts/
    trip_cancellations/trips_ran_daily) hebben route_id al klaarstaan."""

    def __init__(self):
        self.routes = {}
        self.reload()

    def reload(self):
        path = DATA_DIR / "utrecht_routes.json"
        self.routes = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def is_bus_route(self, route_id):
        """Zelfde definitie als gtfs_rt.UtrechtIndex.is_bus_route(): telt niet
        mee als de route niet (meer) bestaat, of de U-tram is (die realtime
        feed levert geen bruikbare uitvalcijfers)."""
        route = self.routes.get(route_id)
        if route is None:
            return False
        return route.get("operator") != TRANSDEV_TRAM


_index = _LiteRouteIndex()


def route_meta(route_id):
    r = _index.routes.get(route_id, {})
    return {
        "route_id": route_id,
        "short_name": r.get("short_name", "?"),
        "long_name": r.get("long_name", ""),
        "agency_name": r.get("agency_name", "?"),
        "operator": r.get("operator", "Onbekend"),
    }


@app.route("/lite")
def lite_index():
    return render_template("lite.html")


@app.route("/lite/api/health")
def lite_api_health():
    """Zelfde vorm en logica als het volledige /api/health in app/server.py,
    zodat de statusknop op /lite exact hetzelfde gedrag vertoont."""
    now = int(time.time())
    conn = db.get_conn()
    try:
        vp_last = conn.execute("SELECT MAX(fetched_at) AS t FROM vehicle_positions").fetchone()["t"]
        td_last = conn.execute("SELECT MAX(fetched_at) AS t FROM trip_delays").fetchone()["t"]
        cancel_last = conn.execute("SELECT MAX(last_seen) AS t FROM trip_cancellations").fetchone()["t"]
        ns_status = conn.execute(
            "SELECT last_success_at, last_error_at FROM ns_fetch_status WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    def component(last_fetched_at, stale_after=VEHICLE_FRESHNESS_SECONDS):
        if last_fetched_at is None:
            return {"last_fetched_at": None, "seconds_ago": None, "status": "no_data"}
        seconds_ago = now - last_fetched_at
        status = "ok" if seconds_ago <= stale_after else "stale"
        return {"last_fetched_at": last_fetched_at, "seconds_ago": seconds_ago, "status": status}

    if ns_status is None:
        rail_alerts_component = {"last_fetched_at": None, "seconds_ago": None, "status": "not_configured"}
    else:
        rail_alerts_component = component(ns_status["last_success_at"], stale_after=RAIL_ALERTS_STALE_AFTER_SECONDS)

    components = {
        "vehicle_positions": component(vp_last),
        "trip_delays": component(td_last),
        "cancellations": component(cancel_last, stale_after=CANCELLATION_STALE_AFTER_SECONDS),
        "rail_alerts": rail_alerts_component,
    }
    latest = max((t for t in (vp_last, td_last) if t is not None), default=None)
    overall_status = component(latest)["status"] if latest is not None else "no_data"

    return jsonify({
        "now": now,
        "collector_interval_seconds": FETCH_INTERVAL_SECONDS,
        "stale_after_seconds": VEHICLE_FRESHNESS_SECONDS,
        "cancellation_stale_after_seconds": CANCELLATION_STALE_AFTER_SECONDS,
        "rail_alerts_stale_after_seconds": RAIL_ALERTS_STALE_AFTER_SECONDS,
        "status": overall_status,
        "components": components,
    })


@app.route("/lite/api/rail-alerts")
def lite_api_rail_alerts():
    """Zelfde vorm als het volledige /api/rail-alerts in app/server.py --
    storingen op het spoor (NS) binnen de provincie Utrecht."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM rail_alerts WHERE active=1 ORDER BY first_seen DESC"
        ).fetchall()
    finally:
        conn.close()
    alerts = [
        {
            "alert_id": r["alert_id"],
            "disruption_type": r["disruption_type"],
            "type_label": r["type_label"],
            "title": r["title"],
            "description": r["description"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "impact": r["impact"],
            "stations": [s for s in (r["stations"] or "").split(",") if s],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
        }
        for r in rows
    ]
    return jsonify({"alerts": alerts, "count": len(alerts)})


@app.route("/lite/api/alerts")
def lite_api_alerts():
    """Zelfde vorm als het bestaande /api/alerts in app/server.py, zodat de
    frontend-logica (incl. de Stremming/Storing/Verstoring-badge) ongewijzigd
    hergebruikt kan worden."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE active=1 ORDER BY first_seen DESC"
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


@app.route("/lite/api/uitval")
def lite_api_uitval():
    """Basale uitvalcijfers voor vandaag: totaal + per-operator-uitsplitsing.
    Bewust geen ?range=/weekday/hour/week/month-opsplitsing zoals
    server.api_cancellations() -- dat is precies wat de lite-scope weglaat.
    Leunt op de bestaande indexen idx_cancel_date/idx_ran_date (zie app/db.py),
    geen nieuwe index nodig."""
    today = date.today().isoformat()
    conn = db.get_conn()
    try:
        canceled_rows = conn.execute(
            "SELECT route_id, COUNT(*) AS cnt FROM trip_cancellations "
            "WHERE service_date = ? GROUP BY route_id",
            (today,),
        ).fetchall()
        ran_rows = conn.execute(
            """
            SELECT r.route_id, COUNT(*) AS cnt
            FROM trips_ran_daily r
            WHERE r.service_date = ? AND NOT EXISTS (
                SELECT 1 FROM trip_cancellations c
                WHERE c.trip_id = r.trip_id AND c.service_date = r.service_date
            )
            GROUP BY r.route_id
            """,
            (today,),
        ).fetchall()
    finally:
        conn.close()

    per_operator = {}
    total_canceled = total_ran = 0
    for r in canceled_rows:
        if not _index.is_bus_route(r["route_id"]):
            continue
        op = per_operator.setdefault(route_meta(r["route_id"])["operator"], {"canceled": 0, "ran": 0})
        op["canceled"] += r["cnt"]
        total_canceled += r["cnt"]
    for r in ran_rows:
        if not _index.is_bus_route(r["route_id"]):
            continue
        op = per_operator.setdefault(route_meta(r["route_id"])["operator"], {"canceled": 0, "ran": 0})
        op["ran"] += r["cnt"]
        total_ran += r["cnt"]

    per_operator_list = [
        {
            "operator": name,
            "canceled": a["canceled"],
            "ran": a["ran"],
            "cancellation_pct": round(100.0 * a["canceled"] / (a["canceled"] + a["ran"]), 1)
            if (a["canceled"] + a["ran"]) else 0.0,
        }
        for name, a in per_operator.items()
    ]
    per_operator_list.sort(key=lambda x: -x["canceled"])

    total = total_canceled + total_ran
    return jsonify({
        "date": today,
        "total_canceled": total_canceled,
        "total_ran": total_ran,
        "cancellation_pct": round(100.0 * total_canceled / total, 1) if total else 0.0,
        "per_operator": per_operator_list,
    })


@app.route("/lite/api/uitval/daily")
def lite_api_uitval_daily():
    """Uitvalpercentage per dag (totaal + per operator) over de laatste
    CHART_DAYS dagen, voor de trendgrafiek op /lite. Zelfde dag-uitsplitsing
    als server.api_cancellations()'s daily/daily_by_operator-velden, maar
    zonder de overige (weekday/hour/week/month/previous-period)
    breakdowns -- dat is precies wat de lite-scope bewust weglaat."""
    until_date = date.today()
    since_date = until_date - timedelta(days=CHART_DAYS - 1)
    since_str, until_str = since_date.isoformat(), until_date.isoformat()

    conn = db.get_conn()
    try:
        canceled_rows = conn.execute(
            "SELECT service_date, route_id, COUNT(*) AS cnt FROM trip_cancellations "
            "WHERE service_date >= ? AND service_date <= ? GROUP BY service_date, route_id",
            (since_str, until_str),
        ).fetchall()
        ran_rows = conn.execute(
            """
            SELECT r.service_date, r.route_id, COUNT(*) AS cnt
            FROM trips_ran_daily r
            WHERE r.service_date >= ? AND r.service_date <= ? AND NOT EXISTS (
                SELECT 1 FROM trip_cancellations c
                WHERE c.trip_id = r.trip_id AND c.service_date = r.service_date
            )
            GROUP BY r.service_date, r.route_id
            """,
            (since_str, until_str),
        ).fetchall()
    finally:
        conn.close()

    daily = {}
    daily_by_op = {}
    for r in canceled_rows:
        if not _index.is_bus_route(r["route_id"]):
            continue
        daily.setdefault(r["service_date"], {"canceled": 0, "ran": 0})["canceled"] += r["cnt"]
        op = route_meta(r["route_id"])["operator"]
        daily_by_op.setdefault(op, {}).setdefault(r["service_date"], {"canceled": 0, "ran": 0})["canceled"] += r["cnt"]
    for r in ran_rows:
        if not _index.is_bus_route(r["route_id"]):
            continue
        daily.setdefault(r["service_date"], {"canceled": 0, "ran": 0})["ran"] += r["cnt"]
        op = route_meta(r["route_id"])["operator"]
        daily_by_op.setdefault(op, {}).setdefault(r["service_date"], {"canceled": 0, "ran": 0})["ran"] += r["cnt"]

    def pct_list(by_date):
        out = []
        for d in sorted(by_date.keys()):
            c, r = by_date[d]["canceled"], by_date[d]["ran"]
            total = c + r
            out.append({
                "date": d, "canceled": c, "ran": r,
                "cancellation_pct": round(100.0 * c / total, 1) if total else 0.0,
            })
        return out

    return jsonify({
        "since_date": since_str,
        "until_date": until_str,
        "daily": pct_list(daily),
        "daily_by_operator": {op: pct_list(d) for op, d in daily_by_op.items()},
    })


def create_app():
    db.init_db()
    return app


if __name__ == "__main__":
    create_app()
    app.run(host="127.0.0.1", port=5152, debug=False, use_reloader=False)
