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
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import quote as url_quote
from xml.sax.saxutils import escape as xml_escape
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from . import db
from .collector import FETCH_INTERVAL_SECONDS
from .concession_mapping import TRANSDEV_TRAM
from .road_situations import NEGLIGIBLE_SEVERITY, SEVERE_ROAD_TYPES

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Aantal dagen voor de "uitvalpercentage per dag"-trendgrafiek op /lite --
# vast en niet instelbaar (geen ?range=-parameter zoals bij de volledige
# /uitval-dashboard), consistent met de "basale" lite-scope.
CHART_DAYS = 30

# Vanaf welk uitvalpercentage (van vandaag, per vervoerder) de RSS-feed
# hieronder een melding opneemt.
CANCELLATION_ALERT_THRESHOLD_PCT = 6.0
LITE_BASE_URL = "https://ovreader.dvznet.nl/lite"

# Zelfde definitie als app/server.py's /api/health.
VEHICLE_FRESHNESS_SECONDS = 90
CANCELLATION_STALE_AFTER_SECONDS = 26 * 3600  # ruim over 24u: uitval komt sporadisch binnen, geen 30s-heartbeat
RAIL_ALERTS_STALE_AFTER_SECONDS = 600  # zelfde marge als in app/server.py (job draait elke 2 min)
ROAD_SITUATIONS_STALE_AFTER_SECONDS = 1200  # zelfde marge als in app/server.py (job draait elke 5 min)
KNMI_WARNINGS_STALE_AFTER_SECONDS = 5400  # zelfde marge als in app/server.py (job draait elke 30 min)
KNMI_WEATHER_STALE_AFTER_SECONDS = 2700  # zelfde marge als in app/server.py (job draait elke 15 min)
AIR_QUALITY_STALE_AFTER_SECONDS = 5400  # zelfde marge als in app/server.py (job draait elke 30 min)

app = Flask(
    __name__,
    template_folder=str(PROJECT_ROOT / "templates"),
    static_folder=str(PROJECT_ROOT / "static"),
    static_url_path="/lite/static",
)


class _LiteRouteIndex:
    """Minimale route-index voor de lite-app: leest alleen utrecht_routes.json
    en utrecht_stops.json in (lijnnaam/operator- resp. haltenaam-lookup) --
    niet de trip-mappings uit gtfs_rt.UtrechtIndex, die alleen nodig zijn om
    trip_id's te resolven bij het ophalen van de realtime feed. De rijen die
    deze app leest (alerts/trip_cancellations/trips_ran_daily) hebben
    route_id al klaarstaan; stops zijn alleen nodig om bij een halte-only
    melding (geen route/trip in informed_entity) de haltenaam te tonen."""

    def __init__(self):
        self.routes = {}
        self.stops = {}
        self.reload()

    def reload(self):
        path = DATA_DIR / "utrecht_routes.json"
        self.routes = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        stops_path = DATA_DIR / "utrecht_stops.json"
        self.stops = json.loads(stops_path.read_text(encoding="utf-8")) if stops_path.exists() else {}

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


def stop_meta(stop_id):
    s = _index.stops.get(stop_id, {})
    return {"stop_id": stop_id, "name": s.get("name", "?")}


@app.route("/lite")
def lite_index():
    return render_template("lite.html")


@app.route("/lite/geschiedenis")
def lite_geschiedenis():
    return render_template("lite_geschiedenis.html", limit=HISTORY_ITEM_LIMIT)


# Mensleesbare labels voor rss_feed_items.kind (zie collector.py:
# check_cancellation_alerts_job()/fetch_rail_alerts_job()/fetch_road_situations_job()/
# de alerts-sync in collect_once() -- dit zijn de vier 'kind'-waarden die
# worden weggeschreven).
_HISTORY_KIND_LABELS = {
    "cancellation": "Uitval",
    "rail_alert": "NS-storing",
    "bus_alert": "U-OV-melding",
    "road_situation": "Wegsituatie",
}
HISTORY_ITEM_LIMIT = 200


@app.route("/lite/api/geschiedenis")
def lite_api_geschiedenis():
    """Mensleesbare versie van de RSS-feed (/lite/rss.xml): dezelfde
    permanente log uit rss_feed_items, voor wie geen RSS-lezer gebruikt.
    Iets ruimer gelimiteerd dan de feed zelf (die is bewust compact
    gehouden) -- dit is een geschiedenispagina, geen actuele feed.

    Optioneel ?kind=cancellation/rail_alert/bus_alert/road_situation om tot
    één soort te beperken (voor het filter op de pagina zelf).

    'today_by_kind' telt, ongeacht het filter, hoeveel items er sinds
    middernacht (lokale tijd) zijn bijgekomen per soort -- voor de
    KPI-tegel bovenaan de pagina."""
    kind = request.args.get("kind")
    today_start = int(
        datetime.combine(date.today(), datetime.min.time()).timestamp()
    )
    conn = db.get_conn()
    try:
        if kind:
            rows = conn.execute(
                "SELECT * FROM rss_feed_items WHERE kind = ? ORDER BY pub_date DESC LIMIT ?",
                (kind, HISTORY_ITEM_LIMIT),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM rss_feed_items ORDER BY pub_date DESC LIMIT ?",
                (HISTORY_ITEM_LIMIT,),
            ).fetchall()
        today_rows = conn.execute(
            "SELECT kind, COUNT(*) AS cnt FROM rss_feed_items WHERE pub_date >= ? GROUP BY kind",
            (today_start,),
        ).fetchall()
    finally:
        conn.close()
    items = [
        {
            "kind": r["kind"],
            "kind_label": _HISTORY_KIND_LABELS.get(r["kind"], r["kind"]),
            "title": r["title"],
            "description": r["description"],
            "pub_date": r["pub_date"],
        }
        for r in rows
    ]
    today_by_kind = {r["kind"]: r["cnt"] for r in today_rows}
    return jsonify({
        "items": items,
        "count": len(items),
        "today_by_kind": today_by_kind,
        "today_total": sum(today_by_kind.values()),
    })


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
        road_status = conn.execute(
            "SELECT last_success_at, last_error_at FROM road_fetch_status WHERE id = 1"
        ).fetchone()
        knmi_status = conn.execute(
            "SELECT last_success_at, last_error_at FROM knmi_fetch_status WHERE id = 1"
        ).fetchone()
        knmi_weather_status = conn.execute(
            "SELECT last_success_at, last_error_at FROM knmi_weather_fetch_status WHERE id = 1"
        ).fetchone()
        air_quality_status = conn.execute(
            "SELECT last_success_at, last_error_at FROM air_quality_fetch_status WHERE id = 1"
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
    road_situations_component = component(
        road_status["last_success_at"] if road_status else None,
        stale_after=ROAD_SITUATIONS_STALE_AFTER_SECONDS,
    )
    if knmi_status is None:
        knmi_warnings_component = {"last_fetched_at": None, "seconds_ago": None, "status": "not_configured"}
    else:
        knmi_warnings_component = component(knmi_status["last_success_at"], stale_after=KNMI_WARNINGS_STALE_AFTER_SECONDS)
    if knmi_weather_status is None:
        knmi_weather_component = {"last_fetched_at": None, "seconds_ago": None, "status": "not_configured"}
    else:
        knmi_weather_component = component(knmi_weather_status["last_success_at"], stale_after=KNMI_WEATHER_STALE_AFTER_SECONDS)
    air_quality_component = component(
        air_quality_status["last_success_at"] if air_quality_status else None,
        stale_after=AIR_QUALITY_STALE_AFTER_SECONDS,
    )

    components = {
        "vehicle_positions": component(vp_last),
        "trip_delays": component(td_last),
        "cancellations": component(cancel_last, stale_after=CANCELLATION_STALE_AFTER_SECONDS),
        "rail_alerts": rail_alerts_component,
        "road_situations": road_situations_component,
        "knmi_warnings": knmi_warnings_component,
        "knmi_weather": knmi_weather_component,
        "air_quality": air_quality_component,
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


def _active_rail_alerts():
    """Actieve NS-spoorstoringen (provincie Utrecht) -- gedeeld door
    lite_api_rail_alerts() en de RSS-feed hieronder."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM rail_alerts WHERE active=1 ORDER BY first_seen DESC"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "alert_id": r["alert_id"],
            "disruption_type": r["disruption_type"],
            "type_label": r["type_label"],
            "title": r["title"],
            "description": r["description"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "impact": r["impact"],
            "consequence_level": r["consequence_level"],
            "stations": [s for s in (r["stations"] or "").split(",") if s],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
        }
        for r in rows
    ]


@app.route("/lite/api/rail-alerts")
def lite_api_rail_alerts():
    """Zelfde vorm als het volledige /api/rail-alerts in app/server.py --
    storingen op het spoor (NS) binnen de provincie Utrecht."""
    alerts = _active_rail_alerts()
    return jsonify({"alerts": alerts, "count": len(alerts)})


@app.route("/lite/api/road-situations")
def lite_api_road_situations():
    """Zelfde vorm als het volledige /api/road-situations in app/server.py --
    actuele wegsituaties (NDW open data -- RWS-snelwegen, provinciale en
    lokale wegen) binnen de provincie Utrecht, beperkt tot de urgente
    typen (zie aldaar) en nooit ernst "gering" (NEGLIGIBLE_SEVERITY)."""
    types = tuple(sorted(SEVERE_ROAD_TYPES))
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM road_situations WHERE active=1 "
            f"AND record_type IN ({','.join('?' * len(types))}) "
            "AND (severity IS NULL OR severity != ?) ORDER BY first_seen DESC",
            types + (NEGLIGIBLE_SEVERITY,),
        ).fetchall()
    finally:
        conn.close()
    situations = [
        {
            "situation_id": r["situation_id"],
            "record_type": r["record_type"],
            "type_label": r["type_label"],
            "comment": r["comment"],
            "cause": r["cause"],
            "severity": r["severity"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "lat": r["lat"],
            "lon": r["lon"],
            "road_number": r["road_number"],
            "road_location": r["road_location"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
        }
        for r in rows
    ]
    return jsonify({"situations": situations, "count": len(situations)})


@app.route("/lite/api/weather-warnings")
def lite_api_weather_warnings():
    """Zelfde vorm als het volledige /api/weather-warnings in app/server.py --
    actuele KNMI-weerwaarschuwingen voor provincie Utrecht."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM knmi_warnings ORDER BY "
            "CASE color WHEN 'RED' THEN 3 WHEN 'ORANGE' THEN 2 WHEN 'YELLOW' THEN 1 ELSE 0 END DESC"
        ).fetchall()
    finally:
        conn.close()
    warnings = [
        {
            "phenomenon_id": r["phenomenon_id"],
            "phenomenon_label": r["phenomenon_label"],
            "color": r["color"],
            "color_label": r["color_label"],
            "active_from": r["active_from"],
            "worst_at": r["worst_at"],
            "header": r["header"],
            "description": r["description"],
        }
        for r in rows
    ]
    return jsonify({"warnings": warnings, "count": len(warnings)})


@app.route("/lite/api/weather")
def lite_api_weather():
    """Zelfde vorm als het volledige /api/weather in app/server.py -- actuele
    weerwaarneming (De Bilt, provincie Utrecht)."""
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM knmi_weather WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row is None:
        return jsonify({"weather": None})
    return jsonify({"weather": {
        "station": row["station"],
        "observed_at": row["observed_at"],
        "temperature": row["temperature"],
        "dew_point": row["dew_point"],
        "humidity": row["humidity"],
        "wind_speed_ms": row["wind_speed_ms"],
        "wind_speed_bft": row["wind_speed_bft"],
        "wind_gust_ms": row["wind_gust_ms"],
        "wind_direction": row["wind_direction"],
        "wind_direction_compass": row["wind_direction_compass"],
        "pressure": row["pressure"],
        "cloud_cover_okta": row["cloud_cover_okta"],
    }})


@app.route("/lite/api/air-quality")
def lite_api_air_quality():
    """Zelfde vorm als het volledige /api/air-quality in app/server.py --
    actuele luchtkwaliteit (Utrecht-Griftpark, RIVM Luchtmeetnet)."""
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM air_quality WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row is None:
        return jsonify({"air_quality": None})
    return jsonify({"air_quality": {
        "station": row["station"],
        "measured_at": row["measured_at"],
        "lki": row["lki"],
        "lki_label": row["lki_label"],
        "lki_color": row["lki_color"],
        "concentrations": json.loads(row["concentrations"]) if row["concentrations"] else {},
    }})


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
        stop_ids = [sid for sid in (r["stop_ids"] or "").split(",") if sid]
        alerts.append({
            "alert_id": r["alert_id"],
            "header": r["header"],
            "description": r["description"],
            "effect": r["effect"],
            "cause": r["cause"],
            "routes": [route_meta(rid) for rid in route_ids],
            "stops": [stop_meta(sid) for sid in stop_ids],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "valid_from": r["valid_from"],
            "valid_until": r["valid_until"],
        })
    return jsonify({"alerts": alerts, "count": len(alerts)})


def _uitval_per_operator_vandaag(up_to_now=False):
    """Rauwe canceled/ran-telling per operator voor vandaag -- gedeeld door
    lite_api_uitval() en de RSS-feed hieronder, zodat er maar één plek is die
    de trip_cancellations/trips_ran_daily-query en de is_bus_route-filter
    kent.

    up_to_now=True: zelfde correctie als ?up_to_now=1 op het volledige
    /api/cancellations (server.py) -- vervoerders melden een uitgevallen rit
    soms al ruim voordat de geplande vertrektijd is verstreken, wat het
    percentage van de nog lopende dag omhoog scheeft (uitval hele dag vs.
    gereden tot nu toe). Genegeerd: vervallen ritten van vandaag met een nog
    niet verstreken start_time."""
    today = date.today().isoformat()
    now_time_str = datetime.now().strftime("%H:%M:%S")
    conn = db.get_conn()
    try:
        canceled_rows = conn.execute(
            "SELECT route_id, start_time, COUNT(*) AS cnt FROM trip_cancellations "
            "WHERE service_date = ? GROUP BY route_id, start_time",
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
    for r in canceled_rows:
        if not _index.is_bus_route(r["route_id"]):
            continue
        if up_to_now and r["start_time"] and r["start_time"] > now_time_str:
            continue  # vooraf aangekondigde uitval voor een vertrektijd die nog moet komen
        op = per_operator.setdefault(route_meta(r["route_id"])["operator"], {"canceled": 0, "ran": 0})
        op["canceled"] += r["cnt"]
    for r in ran_rows:
        if not _index.is_bus_route(r["route_id"]):
            continue
        op = per_operator.setdefault(route_meta(r["route_id"])["operator"], {"canceled": 0, "ran": 0})
        op["ran"] += r["cnt"]
    return today, per_operator


@app.route("/lite/api/uitval")
def lite_api_uitval():
    """Basale uitvalcijfers voor vandaag: totaal + per-operator-uitsplitsing.
    Bewust geen ?range=/weekday/hour/week/month-opsplitsing zoals
    server.api_cancellations() -- dat is precies wat de lite-scope weglaat.
    Leunt op de bestaande indexen idx_cancel_date/idx_ran_date (zie app/db.py),
    geen nieuwe index nodig.

    Optioneel ?up_to_now=1: zelfde correctie als op het volledige
    /api/cancellations, zie _uitval_per_operator_vandaag()."""
    up_to_now = request.args.get("up_to_now") in ("1", "true", "yes")
    today, per_operator = _uitval_per_operator_vandaag(up_to_now=up_to_now)
    total_canceled = sum(a["canceled"] for a in per_operator.values())
    total_ran = sum(a["ran"] for a in per_operator.values())

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
        "up_to_now": up_to_now,
        "total_canceled": total_canceled,
        "total_ran": total_ran,
        "cancellation_pct": round(100.0 * total_canceled / total, 1) if total else 0.0,
        "per_operator": per_operator_list,
    })


# Aantal items dat de RSS-feed maximaal toont -- rss_feed_items zelf wordt
# nooit opgeschoond (log, geen live-status), maar zonder limiet zou de feed
# na maanden/jaren onbeperkt blijven groeien.
RSS_FEED_ITEM_LIMIT = 100


@app.route("/lite/rss.xml")
def lite_rss_uitval():
    """RSS-feed met de geschiedenis van drie soorten meldingen, allemaal
    linkend naar de lite-pagina: uitval boven CANCELLATION_ALERT_THRESHOLD_PCT
    per vervoerder, nieuwe NS-spoorstoringen (provincie Utrecht), en ernstige
    U-OV-meldingen (bus/tram -- zelfde ernst-detectie als de rode badge op
    het dashboard, zie _is_severe_alert() in collector.py). Alle drie worden
    vastgelegd door de collector zodra ze zich voordoen (zie
    check_cancellation_alerts_job()/fetch_rail_alerts_job()/de alerts-sync in
    collect_once(), allemaal in collector.py) en blijven daarna in de feed
    staan -- dit is bewust een LOG van gebeurtenissen, geen weerspiegeling
    van de actuele live-status. Een melding verdwijnt dus niet vanzelf zodra
    de situatie weer normaal is."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM rss_feed_items ORDER BY pub_date DESC LIMIT ?",
            (RSS_FEED_ITEM_LIMIT,),
        ).fetchall()
    finally:
        conn.close()

    # <link> kreeg vroeger voor elk item dezelfde LITE_BASE_URL -- <guid> was
    # dan wel al uniek, maar sommige RSS-lezers gebruiken (mede) de link om
    # items te herkennen/als gelezen te markeren, dus identieke links lieten
    # zo'n lezer bij het openen van 1 melding alles als gelezen zien. Het
    # guid als fragment erachter plakken maakt elke link uniek zonder dat er
    # een echte pagina achter dat fragment hoeft te bestaan.
    items = [f"""
    <item>
      <title>{xml_escape(r['title'])}</title>
      <link>{xml_escape(LITE_BASE_URL)}#{xml_escape(url_quote(r['guid'], safe=''))}</link>
      <guid isPermaLink="false">{xml_escape(r['guid'])}</guid>
      <pubDate>{format_datetime(datetime.fromtimestamp(r['pub_date'], tz=timezone.utc))}</pubDate>
      <description>{xml_escape(r['description'])}</description>
    </item>""" for r in rows]

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>OV Utrecht - Storingen en uitval-signalering</title>
    <link>{xml_escape(LITE_BASE_URL)}</link>
    <description>Meldingen bij uitval van Keolis of Transdev boven de {CANCELLATION_ALERT_THRESHOLD_PCT:.0f}%, bij nieuwe NS-storingen op het spoor in de provincie Utrecht, en bij ernstige U-OV-meldingen (bus/tram).</description>
    <lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>{''.join(items)}
  </channel>
</rss>"""
    return Response(xml, mimetype="application/rss+xml")


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
