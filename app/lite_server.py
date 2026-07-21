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
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template

from . import db
from .concession_mapping import TRANSDEV_TRAM

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Aantal dagen voor de "uitvalpercentage per dag"-trendgrafiek op /lite --
# vast en niet instelbaar (geen ?range=-parameter zoals bij de volledige
# /uitval-dashboard), consistent met de "basale" lite-scope.
CHART_DAYS = 14

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
