"""Flask-app: dashboard + JSON API voor het busvervoer-monitoringssysteem
van U-OV (Keolis en Transdev, gezamenlijke concessiehouder busvervoer
provincie Utrecht)."""
import hashlib
import hmac
import json
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from flask import (
    Flask, Response, jsonify, redirect, render_template, request, send_file,
    session, url_for,
)

from . import db, records
from .collector import FETCH_INTERVAL_SECONDS, RETENTION_DAYS
from .gtfs_rt import UtrechtIndex
from .road_situations import DEMONSTRATION_CAUSE, NEGLIGIBLE_SEVERITY, SEVERE_ROAD_TYPES
from .timetable import Timetable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
app = Flask(
    __name__,
    template_folder=str(PROJECT_ROOT / "templates"),
    static_folder=str(PROJECT_ROOT / "static"),
)
_index = UtrechtIndex()
_timetable = Timetable()

# Vereenvoudigde routetrajecten (zie app/build_static_index.py) voor het
# tekenen van de lijn op de kaart. Bewust alleen hier geladen (niet in
# UtrechtIndex, dat ook door de collector wordt gebruikt) -- de collector
# heeft hier niets aan, en lite_server.py laadt 'm om dezelfde reden als
# utrecht_stop_times.json niet: alleen de hoofd-webservice toont de kaart.
_ROUTE_SHAPES_PATH = PROJECT_ROOT / "data" / "utrecht_shapes.json"
_route_shapes = (
    json.loads(_ROUTE_SHAPES_PATH.read_text(encoding="utf-8")) if _ROUTE_SHAPES_PATH.exists() else {}
)

# Voertuignummer -> bustype (zie app/build_bus_types_index.py), voor de
# voertuig-popup op de kaart. Zelfde bewuste keuze als _route_shapes hierboven:
# alleen hier geladen, niet in UtrechtIndex (collector heeft er niets aan) en
# niet in lite_server.py (die toont geen kaart/popup-detail).
_BUS_TYPES_PATH = PROJECT_ROOT / "data" / "bus_types.json"
_bus_types = (
    json.loads(_BUS_TYPES_PATH.read_text(encoding="utf-8")) if _BUS_TYPES_PATH.exists() else {}
)

ON_TIME_MAX_DELAY = 180  # seconden; conform gangbare NL OV-definitie van "op tijd"
ON_TIME_MIN_DELAY = -120  # meer dan 2 min te vroeg telt niet meer als "op tijd" (Dienstregeling)
VEHICLE_FRESHNESS_SECONDS = 90
# Cache-TTL voor de zware aggregatie-endpoints (/api/stats, /api/stats/trend,
# /api/stats/peak, /api/records): groeperen over tientallen miljoenen rijen
# trip_delays en kosten daardoor seconden tot tientallen seconden per call.
# Punctualiteits-/trendcijfers veranderen sowieso traag, dus verse data elke
# 30 minuten is ruim vers genoeg -- en voorkomt dat /trends beide cores
# verzadigt zodra iemand de pagina laadt of ververst.
STATS_CACHE_TTL_SECONDS = 1800

_response_cache = {}


def _cached(cache_key, ttl_seconds, compute_fn):
    """Simpele in-process TTL-cache voor dure aggregatie-endpoints (/api/stats,
    /api/records) -- voorkomt dat meerdere gelijktijdige bezoekers (of een
    pollende client) dezelfde zware query binnen een paar seconden/minuten
    steeds opnieuw laten uitvoeren. Per gunicorn-worker, niet gedeeld tussen
    workers -- dat hoeft ook niet, het doel is alleen herhaald werk binnen
    één worker te schelen."""
    now = time.time()
    cached = _response_cache.get(cache_key)
    if cached and now - cached[0] < ttl_seconds:
        return cached[1]
    value = compute_fn()
    _response_cache[cache_key] = (now, value)
    return value


TRENDS_REFRESH_HOUR, TRENDS_REFRESH_MINUTE = 3, 30  # servertijd


def _next_daily_boundary(after_ts, hour=TRENDS_REFRESH_HOUR, minute=TRENDS_REFRESH_MINUTE):
    after_dt = datetime.fromtimestamp(after_ts)
    boundary = after_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if boundary <= after_dt:
        boundary += timedelta(days=1)
    return boundary.timestamp()


def _cached_daily(cache_key, compute_fn):
    """Als _cached(), maar in plaats van een vaste TTL-duur blijft het
    resultaat geldig tot de eerstvolgende keer dat de klok
    TRENDS_REFRESH_HOUR:TRENDS_REFRESH_MINUTE slaat -- dus hoogstens één
    herberekening per dag (per gunicorn-worker), ongeacht hoe laat op de dag
    de vorige berekening precies plaatsvond. Voor de zware /trends-data, die
    toch maar eens per nacht hoeft te verversen."""
    now = time.time()
    cached = _response_cache.get(cache_key)
    if cached:
        cached_at, value = cached
        if now < _next_daily_boundary(cached_at):
            return value
    value = compute_fn()
    _response_cache[cache_key] = (now, value)
    return value


# HTTP Basic Auth: staat standaard UIT (handig voor lokaal gebruik). Zet de
# omgevingsvariabele BUS_MONITOR_PASSWORD op de server om dit verplicht te
# maken -- doe dit altijd voordat de app vanaf het internet bereikbaar is.
# Een tweede account (bv. een read-only kijker) kan via BUS_MONITOR_USER2 /
# BUS_MONITOR_PASSWORD2 worden ingesteld.
AUTH_USER = os.environ.get("BUS_MONITOR_USER", "admin")
AUTH_PASSWORD = os.environ.get("BUS_MONITOR_PASSWORD")
AUTH_USER2 = os.environ.get("BUS_MONITOR_USER2")
AUTH_PASSWORD2 = os.environ.get("BUS_MONITOR_PASSWORD2")

# Sessie-secret voor de inlogpagina (zie login()/logout() hieronder). Afgeleid
# van BUS_MONITOR_PASSWORD i.p.v. een losse env-var: zo blijven ingelogde
# sessies geldig over een herstart/redeploy heen (de deploy-webhook herstart
# de service bij elke push) zonder een tweede geheim te hoeven beheren. Zonder
# BUS_MONITOR_PASSWORD is er sowieso geen auth actief.
app.secret_key = hashlib.sha256(f"ovreader-session:{AUTH_PASSWORD or 'dev'}".encode()).hexdigest()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True


def _valid_credentials(username, password):
    if not username or not password:
        return False
    if hmac.compare_digest(username, AUTH_USER) and hmac.compare_digest(password, AUTH_PASSWORD):
        return True
    return bool(
        AUTH_USER2 and AUTH_PASSWORD2
        and hmac.compare_digest(username, AUTH_USER2)
        and hmac.compare_digest(password, AUTH_PASSWORD2)
    )


@app.before_request
def require_auth():
    if not AUTH_PASSWORD:
        return None
    # Onbekende paden nooit naar /login sturen: anders krijgt elke scanner die
    # op goed geluk paden probeert (bv. webshell-scans op /fw.php) alsnog een
    # 200'de loginpagina te zien, en missen bot-detectiescenario's (die op 404
    # letten) dit verkeer. request.url_rule staat hier al vast (Flask matcht
    # de route voordat before_request draait), dus onbekende paden kunnen
    # gewoon direct de normale 404-afhandeling in.
    if request.url_rule is None:
        return None
    if request.path == "/login" or request.path.startswith("/static/"):
        return None
    if session.get("authenticated"):
        return None
    # Basic Auth blijft ook werken (naast de sessie-login hieronder) -- o.a.
    # /api/backup/latest wordt vanaf een andere machine met `curl -u`
    # opgehaald, en dat scriptje moet niet stuk gaan door deze wijziging.
    auth = request.authorization
    if auth is not None and _valid_credentials(auth.username, auth.password):
        return None
    if request.path.startswith("/api/"):
        return Response(
            "Authenticatie vereist", 401, {"WWW-Authenticate": 'Basic realm="Bus Monitor"'}
        )
    return redirect(url_for("login", next=request.full_path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_PASSWORD:
        return redirect(url_for("index"))
    next_url = request.values.get("next") or url_for("index")
    # Open-redirect-guard: alleen relatieve paden toestaan. Backslashes worden
    # eerst genormaliseerd naar '/', want browsers doen dat ook -- zonder dit
    # zou '/\evil.com' langs de '//'-check heen glippen en na normalisatie
    # alsnog als protocol-relatieve URL naar een extern domein uitkomen.
    normalized = next_url.replace("\\", "/")
    if not normalized.startswith("/") or normalized.startswith("//"):
        next_url = url_for("index")
    error = None
    if request.method == "POST":
        if _valid_credentials(request.form.get("username", ""), request.form.get("password", "")):
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            return redirect(next_url)
        error = "Onjuiste gebruikersnaam of wachtwoord."
    if error:
        return render_template("login.html", error=error, next=next_url), 401
    return render_template("login.html", error=error, next=next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def route_meta(route_id):
    r = _index.routes.get(route_id, {})
    return {
        "route_id": route_id,
        "short_name": r.get("short_name", "?"),
        "long_name": r.get("long_name", ""),
        "agency_name": r.get("agency_name", "?"),
        "operator": r.get("operator", "Onbekend"),
        # Officiële merkkleur van de lijn (hex zonder '#', zoals in de GTFS-
        # feed) -- los van de vertragingskleur op de kaart, die functioneel
        # blijft (status i.p.v. lijnidentiteit).
        "color": r.get("color") or None,
        "text_color": r.get("text_color") or None,
    }


def stop_meta(stop_id):
    s = _index.stops.get(stop_id, {})
    return {
        "stop_id": stop_id,
        "name": s.get("name", "?"),
        # lat/lon zodat de frontend de halte direct op de kaart kan tonen
        # (bv. een 'halte vervalt'-marker) zonder aparte lookup.
        "lat": s.get("lat"),
        "lon": s.get("lon"),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/meta")
def api_meta():
    agencies = sorted({r["agency_name"] for r in _index.routes.values()})
    operators = sorted({r.get("operator", "Onbekend") for r in _index.routes.values()})
    routes = [
        {"route_id": rid, **route_meta(rid)}
        for rid in _index.routes
    ]
    return jsonify({
        "agencies": agencies, "operators": operators,
        "routes": routes, "route_count": len(routes),
    })


CANCELLATION_STALE_AFTER_SECONDS = 26 * 3600  # ruim over 24u: uitval komt sporadisch binnen, geen 30s-heartbeat
# fetch_rail_alerts_job() draait elke 2 minuten (zie collector.py); 10
# minuten geeft ruimte voor een paar gemiste cycli (bv. een tijdelijke
# NS-API-hik) voordat dit als stilgevallen wordt gemeld.
RAIL_ALERTS_STALE_AFTER_SECONDS = 600
# fetch_road_situations_job() draait elke 5 minuten; 20 minuten geeft ruimte
# voor een paar gemiste cycli. Publieke, sleutelloze bron (geen
# 'not_configured' zoals bij NS_API_KEY) -- hoort altijd te draaien.
ROAD_SITUATIONS_STALE_AFTER_SECONDS = 1200
# fetch_knmi_warnings_job() draait elke 30 minuten; 90 minuten geeft ruimte
# voor een paar gemiste cycli. Zonder KNMI_API_KEY 'not_configured', net als
# bij NS_API_KEY.
KNMI_WARNINGS_STALE_AFTER_SECONDS = 5400
# fetch_knmi_weather_job() draait elke 15 minuten; 45 minuten geeft ruimte
# voor een paar gemiste cycli.
KNMI_WEATHER_STALE_AFTER_SECONDS = 2700
# fetch_air_quality_job() draait elke 30 minuten; 90 minuten geeft ruimte
# voor een paar gemiste cycli. Publieke, sleutelloze bron (geen
# 'not_configured'), net als road_situations.
AIR_QUALITY_STALE_AFTER_SECONDS = 5400


@app.route("/api/health")
def api_health():
    """Laat zien hoe recent de collector nog data heeft binnengekregen, zodat
    een stilgevallen achtergrondverzamelaar (bv. na een OVapi-storing of een
    gecrashte service) opvalt zonder dat je handmatig journalctl hoeft te
    bekijken.

    'trip_delays' zegt alleen iets over vertragingen -- uitval
    (trip_cancellations) wordt in de collector in een eigen, onafhankelijke
    try/except verwerkt en kan dus stilzwijgend stuklopen terwijl
    vertragingen gewoon doorlopen. Daarom een apart component ervoor, met een
    veel ruimer stale-venster: een dag zonder uitval is normaal, geen teken
    dat de verwerking kapot is.

    'rail_alerts' (NS) is nog een aparte bron: rail_alerts zelf krijgt alleen
    nieuwe rijen zolang er een actieve storing is, dus MAX(last_seen) daarop
    zou "geen storingen" en "de job is stukgelopen" niet uit elkaar houden.
    ns_fetch_status (zie collector.py) houdt daarom apart bij wanneer de job
    voor het laatst succesvol/met een fout afrondde, los van of er iets te
    tonen viel. Zonder NS_API_KEY draait die job niet en blijft
    ns_fetch_status leeg -- status 'not_configured', geen storing."""
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
    # Uitval en NS-spoorstoringen tellen bewust niet mee in de totaalstatus:
    # dat signaal gaat over of de collector-loop voor bus/tram leeft, niet
    # over deze aanvullende, onafhankelijke bronnen.
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


@app.route("/api/backup/latest")
def api_backup_latest():
    """Nieuwste nachtelijke back-up van de historie-tabellen (zie
    backup_history() in app/collector.py), als download. Valt achter dezelfde
    Basic Auth als de rest van de app -- bedoeld om vanaf een andere machine
    periodiek op te halen (curl/geplande taak), zodat de langetermijndata
    ook verlies van de hele VPS overleeft."""
    backup_dir = db.DB_PATH.parent / "backups"
    backups = sorted(backup_dir.glob("history_*.db.gz")) if backup_dir.exists() else []
    if not backups:
        return jsonify({"error": "Nog geen back-up beschikbaar (draait dagelijks om 04:15)"}), 404
    return send_file(backups[-1], as_attachment=True)


@app.route("/api/vehicles")
def api_vehicles():
    cutoff = int(time.time()) - VEHICLE_FRESHNESS_SECONDS
    conn = db.get_conn()
    try:
        # vehicle_id komt uit vehicle.label (zie gtfs_rt.py) en is bij OVapi
        # niet voor elk voertuig gevuld (~98% wel); val in dat geval terug op
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
        # INDEXED BY afgedwongen: zonder ANALYZE-statistieken koos SQLite hier
        # idx_td_trip_id (volledige scan van alle 91M+ rijen in trip_delays,
        # 60+ sec) i.p.v. idx_td_fetched_route_covering, dat via fetched_at
        # direct naar het relevante tijdvenster springt (0,06 sec).
        delay_rows = conn.execute(
            """
            SELECT trip_id, arrival_delay, departure_delay
            FROM trip_delays INDEXED BY idx_td_fetched_route_covering
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
        trip_meta = _timetable.trip_meta.get(r["trip_id"], {})
        stop = _timetable.stops.get(r["stop_id"]) if r["stop_id"] else None
        vehicles.append({
            "ident": r["ident"],
            "vehicle_id": r["vehicle_id"],
            "vehicle_type": _bus_types.get(r["vehicle_id"]) if r["vehicle_id"] else None,
            "trip_id": r["trip_id"],
            "route_id": r["route_id"],
            "route_short_name": meta["short_name"],
            "agency_name": meta["agency_name"],
            "operator": meta["operator"],
            "headsign": trip_meta.get("headsign") or None,
            "direction_id": r["direction_id"],
            "current_status": r["current_status"],
            "current_stop_name": stop["name"] if stop else None,
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
        # first_seen, niet last_seen: de collector stempelt last_seen elke
        # cyclus voor ALLE actieve meldingen bij (zie collector.py), dus die
        # kolom onderscheidt niets tussen ze en sorteert in de praktijk op
        # insertievolgorde -- daarmee zakken net verschenen meldingen naar
        # onderen in plaats van bovenaan te staan.
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


@app.route("/api/rail-alerts")
def api_rail_alerts():
    """Storingen op het spoor (NS) binnen de provincie Utrecht -- aparte
    databron t.o.v. /api/alerts (zie app/ns_rail_alerts.py). Leeg (niet een
    fout) als NS_API_KEY niet is ingesteld: de collector slaat die bron dan
    stilzwijgend over."""
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
            "consequence_level": r["consequence_level"],
            "stations": [s for s in (r["stations"] or "").split(",") if s],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
        }
        for r in rows
    ]
    return jsonify({"alerts": alerts, "count": len(alerts)})


@app.route("/api/road-situations")
def api_road_situations():
    """Actuele wegsituaties (NDW open data -- RWS-snelwegen, provinciale en
    lokale wegen) binnen de provincie Utrecht -- aparte databron t.o.v.
    /api/alerts (zie app/road_situations.py). Publieke, sleutelloze bron (NDW), dus altijd
    actief -- niet afhankelijk van een env var zoals NS_API_KEY.

    Alleen de urgente typen (zie SEVERE_ROAD_TYPES in road_situations.py) en
    nooit ernst "gering" (NEGLIGIBLE_SEVERITY) -- op verzoek. Demonstraties
    (cause-veld, zie DEMONSTRATION_CAUSE) tellen altijd als urgent, ook bij
    ernst "gering" -- NDW tagt demonstraties in de praktijk zelf met severity
    "lowest" terwijl ze wel degelijk tot afsluitingen kunnen leiden, dus de
    gering-uitsluiting zou de demonstratie-uitzondering anders zinloos maken.
    De collector bewaart wel alle situaties -- de rest is vrijwel altijd
    langlopende wegwerkzaamheden/omleidingen die de lijst zouden overspoelen
    zonder iets te zeggen over de actuele situatie."""
    types = tuple(sorted(SEVERE_ROAD_TYPES))
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM road_situations WHERE active=1 "
            f"AND ((record_type IN ({','.join('?' * len(types))}) "
            "AND (severity IS NULL OR severity != ?)) OR cause LIKE '%' || ? || '%') "
            "ORDER BY first_seen DESC",
            types + (NEGLIGIBLE_SEVERITY, DEMONSTRATION_CAUSE),
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


@app.route("/api/weather-warnings")
def api_weather_warnings():
    """Actuele KNMI-weerwaarschuwingen voor provincie Utrecht (zie
    app/knmi_warnings.py). Leeg (niet een fout) als KNMI_API_KEY niet is
    ingesteld: de collector slaat die bron dan stilzwijgend over."""
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


@app.route("/api/weather")
def api_weather():
    """Actuele weerwaarneming (De Bilt, provincie Utrecht) via KNMI (zie
    app/knmi_weather.py). Leeg object (niet een fout) als KNMI_API_KEY niet
    is ingesteld of er nog geen waarneming binnen is."""
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


@app.route("/api/air-quality")
def api_air_quality():
    """Actuele luchtkwaliteit (Utrecht-Griftpark) via het RIVM Luchtmeetnet
    (zie app/luchtkwaliteit.py). Leeg object (niet een fout) als er nog geen
    meting binnen is. Geen key-check nodig, publieke bron."""
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


@app.route("/api/stats")
def api_stats():
    """Punctualiteit per operator en per route, over ruwe data (zie RETENTION_DAYS
    in app/collector.py) aangevuld met opgerolde dagstatistieken voor oudere
    periodes. Optioneel
    ?range=today/week/2weeks/30d/all om tot een periode te beperken (zonder
    parameter: alle historie, zoals voorheen -- gebruikt door het live-dashboard).

    Zonder ?range= (het live dashboard) blijft dit een rollende TTL van
    STATS_CACHE_TTL_SECONDS. Mét ?range= (alleen gebruikt door /trends) volgt
    dit de dagelijkse ververscyclus van die pagina, zie _cached_daily()."""
    range_key = request.args.get("range")
    if range_key is None:
        return jsonify(_cached(("stats", range_key), STATS_CACHE_TTL_SECONDS, lambda: _compute_stats(range_key)))
    return jsonify(_cached_daily(("stats", range_key), lambda: _compute_stats(range_key)))


def _compute_stats(range_key):
    since_date = until_date = since_ts = until_ts = None
    if range_key:
        since_date, until_date = _date_bounds_for_range(range_key)
        since_ts, until_ts = _range_to_epoch(since_date, until_date)

    conn = db.get_conn()
    try:
        raw_sql = """
            SELECT route_id,
                   COUNT(*) AS sample_count,
                   SUM(CASE WHEN COALESCE(arrival_delay, departure_delay, 0) BETWEEN ? AND ? THEN 1 ELSE 0 END) AS on_time_count,
                   AVG(COALESCE(arrival_delay, departure_delay, 0)) AS avg_delay_seconds,
                   MAX(COALESCE(arrival_delay, departure_delay, 0)) AS max_delay_seconds
            FROM trip_delays
            {where}
            GROUP BY route_id
        """
        rolled_sql = """
            SELECT route_id,
                   SUM(sample_count) AS sample_count,
                   SUM(on_time_count) AS on_time_count,
                   AVG(avg_delay_seconds) AS avg_delay_seconds,
                   MAX(max_delay_seconds) AS max_delay_seconds
            FROM route_stats_daily
            {where}
            GROUP BY route_id
        """
        if range_key:
            raw = conn.execute(
                raw_sql.format(where="WHERE fetched_at >= ? AND fetched_at <= ?"),
                (ON_TIME_MIN_DELAY, ON_TIME_MAX_DELAY, since_ts, until_ts),
            ).fetchall()
            rolled = conn.execute(
                rolled_sql.format(where="WHERE day >= ? AND day <= ?"), (since_date, until_date)
            ).fetchall()
        else:
            raw = conn.execute(
                raw_sql.format(where=""), (ON_TIME_MIN_DELAY, ON_TIME_MAX_DELAY)
            ).fetchall()
            rolled = conn.execute(rolled_sql.format(where="")).fetchall()
    finally:
        conn.close()

    by_route = {}
    for r in list(raw) + list(rolled):
        rid = r["route_id"]
        if not _index.is_bus_route(rid):
            # Historische rijen van lijnen die niet meer in de huidige index
            # zitten (bv. na een scope-wijziging of dienstregelingswijziging).
            continue
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
        agg = per_agency.setdefault(meta["operator"], {"sample_count": 0, "on_time_count": 0, "avg_sum": 0.0})
        agg["sample_count"] += e["sample_count"]
        agg["on_time_count"] += e["on_time_count"]
        agg["avg_sum"] += avg_delay * e["sample_count"]

    operator_stats = []
    for name, a in per_agency.items():
        if a["sample_count"] == 0:
            continue
        operator_stats.append({
            "operator": name,
            "sample_count": a["sample_count"],
            "on_time_pct": round(100.0 * a["on_time_count"] / a["sample_count"], 1),
            "avg_delay_seconds": round(a["avg_sum"] / a["sample_count"], 1),
        })

    per_route.sort(key=lambda x: -x["sample_count"])
    operator_stats.sort(key=lambda x: -x["sample_count"])

    return {
        "on_time_threshold_seconds": ON_TIME_MAX_DELAY,
        "on_time_min_delay_seconds": ON_TIME_MIN_DELAY,
        "range": range_key,
        "since_date": since_date,
        "until_date": until_date,
        "per_operator": operator_stats,
        "per_route": per_route,
    }


RANGE_DAYS = {"today": 1, "week": 7, "2weeks": 14, "30d": 30}
EARLIEST_POSSIBLE_DATE = "2000-01-01"  # ondergrens voor range=all
WEEKDAY_NAMES_NL = ["Maandag", "Dinsdag", "Woensdag", "Donderdag", "Vrijdag", "Zaterdag", "Zondag"]


def _iso_week_key(d):
    """ISO-weeksleutel ('2026-W28') voor een date-object, gedeeld door de
    per-week-per-operator-uitvalcijfers op /uitval."""
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


@app.route("/uitval")
def uitval_page():
    return render_template("cancellations.html")


@app.route("/trends")
def trends_page():
    return render_template("trends.html")


def _date_bounds_for_range(range_key):
    """Geeft (since_date, until_date) als ISO-datumstrings voor een range-key
    ('today'/'week'/'2weeks'/'30d'/'all'/'this_month'/'last_month'), gedeeld
    door de uitval- en statistiek-endpoints.

    'this_month'/'last_month' zijn de enige twee ranges waarbij until_date
    niet per se vandaag is: 'last_month' levert een afgesloten periode terug
    (1e t/m laatste dag van de vorige kalendermaand), volledig in het
    verleden. Callers die today_str als variabelenaam gebruikten voor de
    tweede returnwaarde deden dat omdat until_date voorheen altijd vandaag
    was -- dat klopt sinds deze uitbreiding niet meer, dus daar waar de
    daadwerkelijke datum van vandaag nodig is (los van de gekozen range)
    moet een aparte date.today() gebruikt worden."""
    today = date.today()
    today_str = today.isoformat()
    if range_key == "all":
        return EARLIEST_POSSIBLE_DATE, today_str
    if range_key == "this_month":
        return today.replace(day=1).isoformat(), today_str
    if range_key == "last_month":
        last_day_prev_month = today.replace(day=1) - timedelta(days=1)
        first_day_prev_month = last_day_prev_month.replace(day=1)
        return first_day_prev_month.isoformat(), last_day_prev_month.isoformat()
    days = RANGE_DAYS.get(range_key, 1)
    since_date = (today - timedelta(days=days - 1)).isoformat()
    return since_date, today_str


def _range_to_epoch(since_date, until_date):
    """Zet een (since_date, until_date) ISO-datumrange om in unix-epoch-grenzen
    (lokale tijd, hele dagen) voor het filteren van raw fetched_at-timestamps."""
    since_dt = datetime.combine(date.fromisoformat(since_date), dtime.min)
    until_dt = datetime.combine(date.fromisoformat(until_date), dtime.max)
    return int(since_dt.timestamp()), int(until_dt.timestamp())


def _route_ids_filter(route_id, operator):
    """Bepaalt op welke route_ids een verzoek beperkt moet worden op basis van
    de optionele ?route_id=/?operator=-parameters. None betekent: geen filter
    (alle lijnen in de huidige index)."""
    if route_id:
        return {route_id}
    if operator:
        return {rid for rid, r in _index.routes.items() if r.get("operator") == operator}
    return None


@app.route("/api/stats/trend")
def api_stats_trend():
    """Dagreeks (op-tijd % en gem. vertraging) voor een lijn/operator/alle
    lijnen, over een gekozen periode -- voor de trendgrafiek op /trends."""
    range_key = request.args.get("range", "30d")
    route_id = request.args.get("route_id")
    operator = request.args.get("operator")
    cache_key = ("stats-trend", range_key, route_id, operator)
    return jsonify(_cached_daily(cache_key, lambda: _compute_stats_trend(range_key, route_id, operator)))


def _compute_stats_trend(range_key, route_id, operator):
    since_date, until_date = _date_bounds_for_range(range_key)
    since_ts, until_ts = _range_to_epoch(since_date, until_date)
    route_ids = _route_ids_filter(route_id, operator)

    # route_stats_daily wordt elk uur bijgewerkt tot en met gisteren (zie
    # collector.rollup_completed_days), dus ruwe trip_delays hoeft alleen nog
    # voor vandaag doorzocht te worden -- in plaats van de hele gevraagde
    # periode (tot 30 dagen ruwe data, tientallen miljoenen rijen).
    today_start_ts = int(datetime.combine(date.today(), dtime.min).timestamp())
    raw_since_ts = max(since_ts, today_start_ts)

    conn = db.get_conn()
    try:
        raw = conn.execute(
            """
            SELECT strftime('%Y-%m-%d', fetched_at, 'unixepoch', 'localtime') AS day,
                   route_id,
                   COUNT(*) AS sample_count,
                   SUM(CASE WHEN COALESCE(arrival_delay, departure_delay, 0) BETWEEN ? AND ? THEN 1 ELSE 0 END) AS on_time_count,
                   AVG(COALESCE(arrival_delay, departure_delay, 0)) AS avg_delay_seconds
            FROM trip_delays
            WHERE fetched_at >= ? AND fetched_at <= ?
            GROUP BY day, route_id
            """,
            (ON_TIME_MIN_DELAY, ON_TIME_MAX_DELAY, raw_since_ts, until_ts),
        ).fetchall()
        rolled = conn.execute(
            """
            SELECT day, route_id, sample_count, on_time_count, avg_delay_seconds
            FROM route_stats_daily
            WHERE day >= ? AND day <= ?
            """,
            (since_date, until_date),
        ).fetchall()
    finally:
        conn.close()

    by_day = {}
    for r in list(raw) + list(rolled):
        if not _index.is_bus_route(r["route_id"]):
            continue
        if route_ids is not None and r["route_id"] not in route_ids:
            continue
        entry = by_day.setdefault(r["day"], {"sample_count": 0, "on_time_count": 0, "avg_sum": 0.0})
        entry["sample_count"] += r["sample_count"] or 0
        entry["on_time_count"] += r["on_time_count"] or 0
        entry["avg_sum"] += (r["avg_delay_seconds"] or 0) * (r["sample_count"] or 0)

    daily = []
    for day in sorted(by_day):
        e = by_day[day]
        if e["sample_count"] == 0:
            continue
        daily.append({
            "date": day,
            "sample_count": e["sample_count"],
            "on_time_pct": round(100.0 * e["on_time_count"] / e["sample_count"], 1),
            "avg_delay_seconds": round(e["avg_sum"] / e["sample_count"], 1),
        })

    return {"range": range_key, "since_date": since_date, "until_date": until_date, "daily": daily}


@app.route("/api/stats/peak")
def api_stats_peak():
    """Punctualiteit gesplitst naar spits (07-09 en 16-18) vs. dal, voor een
    lijn/operator/alle lijnen, over een gekozen periode."""
    range_key = request.args.get("range", "30d")
    route_id = request.args.get("route_id")
    operator = request.args.get("operator")
    cache_key = ("stats-peak", range_key, route_id, operator)
    return jsonify(_cached_daily(cache_key, lambda: _compute_stats_peak(range_key, route_id, operator)))


def _compute_stats_peak(range_key, route_id, operator):
    since_date, until_date = _date_bounds_for_range(range_key)
    since_ts, until_ts = _range_to_epoch(since_date, until_date)
    route_ids = _route_ids_filter(route_id, operator)

    # Zelfde redenering als _compute_stats_trend: route_stats_period_daily
    # dekt alles tot en met gisteren, ruwe data hoeft alleen voor vandaag.
    today_start_ts = int(datetime.combine(date.today(), dtime.min).timestamp())
    raw_since_ts = max(since_ts, today_start_ts)

    conn = db.get_conn()
    try:
        raw = conn.execute(
            f"""
            SELECT route_id, {db.period_hour_sql()} AS period,
                   COUNT(*) AS sample_count,
                   SUM(CASE WHEN COALESCE(arrival_delay, departure_delay, 0) BETWEEN ? AND ? THEN 1 ELSE 0 END) AS on_time_count,
                   AVG(COALESCE(arrival_delay, departure_delay, 0)) AS avg_delay_seconds
            FROM trip_delays
            WHERE fetched_at >= ? AND fetched_at <= ?
            GROUP BY route_id, period
            """,
            (ON_TIME_MIN_DELAY, ON_TIME_MAX_DELAY, raw_since_ts, until_ts),
        ).fetchall()
        rolled = conn.execute(
            """
            SELECT route_id, period, sample_count, on_time_count, avg_delay_seconds
            FROM route_stats_period_daily
            WHERE day >= ? AND day <= ?
            """,
            (since_date, until_date),
        ).fetchall()
    finally:
        conn.close()

    by_period = {
        "peak": {"sample_count": 0, "on_time_count": 0, "avg_sum": 0.0},
        "offpeak": {"sample_count": 0, "on_time_count": 0, "avg_sum": 0.0},
    }
    for r in list(raw) + list(rolled):
        if not _index.is_bus_route(r["route_id"]):
            continue
        if route_ids is not None and r["route_id"] not in route_ids:
            continue
        e = by_period[r["period"]]
        e["sample_count"] += r["sample_count"] or 0
        e["on_time_count"] += r["on_time_count"] or 0
        e["avg_sum"] += (r["avg_delay_seconds"] or 0) * (r["sample_count"] or 0)

    periods = []
    for period in ("peak", "offpeak"):
        e = by_period[period]
        if e["sample_count"] == 0:
            periods.append({"period": period, "sample_count": 0, "on_time_pct": None, "avg_delay_seconds": None})
            continue
        periods.append({
            "period": period,
            "sample_count": e["sample_count"],
            "on_time_pct": round(100.0 * e["on_time_count"] / e["sample_count"], 1),
            "avg_delay_seconds": round(e["avg_sum"] / e["sample_count"], 1),
        })

    return {
        "range": range_key, "since_date": since_date, "until_date": until_date,
        "peak_hours": sorted(db.PEAK_HOURS),
        "periods": periods,
    }


@app.route("/api/stats/trips")
def api_stats_trips():
    """Drill-down: individuele ritten met hun hoogst waargenomen vertraging,
    voor een lijn/operator over een gekozen periode. Werkt alleen binnen het
    raw-retentievenster (zie RETENTION_DAYS in app/collector.py) -- oudere
    metingen zijn al opgerold tot dagstatistieken en per-rit-detail is dan
    niet meer beschikbaar."""
    range_key = request.args.get("range", "week")
    since_date, until_date = _date_bounds_for_range(range_key)
    since_ts, until_ts = _range_to_epoch(since_date, until_date)
    route_id = request.args.get("route_id")
    operator = request.args.get("operator")
    route_ids = _route_ids_filter(route_id, operator)
    min_delay = int(request.args.get("min_delay_seconds", 300))
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = max(int(request.args.get("offset", 0)), 0)

    conn = db.get_conn()
    try:
        rows = conn.execute(
            """
            SELECT trip_id, route_id,
                   MAX(COALESCE(arrival_delay, departure_delay, 0)) AS max_delay_seconds,
                   MAX(fetched_at) AS last_seen
            FROM trip_delays
            WHERE fetched_at >= ? AND fetched_at <= ?
            GROUP BY trip_id, route_id
            HAVING max_delay_seconds >= ?
            ORDER BY max_delay_seconds DESC
            """,
            (since_ts, until_ts, min_delay),
        ).fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        if not _index.is_bus_route(r["route_id"]):
            continue
        if route_ids is not None and r["route_id"] not in route_ids:
            continue
        items.append({
            "trip_id": r["trip_id"],
            **route_meta(r["route_id"]),
            "max_delay_seconds": r["max_delay_seconds"],
            "last_seen": r["last_seen"],
        })

    return jsonify({
        "range": range_key, "since_date": since_date, "until_date": until_date,
        "raw_retention_days": RETENTION_DAYS,
        "total": len(items),
        "limit": limit, "offset": offset,
        "items": items[offset:offset + limit],
    })


@app.route("/api/records")
def api_records():
    """Curated 'record'-signalering: hoogste uitvalpercentage, netwerkbreed
    en per operator ('ooit'/'deze maand') -- zie app/records.py. Volgt de
    dagelijkse ververscyclus van /trends (zie _cached_daily()) i.p.v. een
    vaste TTL."""
    def compute():
        conn = db.get_conn()
        try:
            result, thresholds = records.find_records(conn, _index)
        finally:
            conn.close()
        return {"min_samples": thresholds, **result}

    data = _cached_daily(("records",), compute)
    return jsonify({"generated_at": int(time.time()), **data})


@app.route("/api/stops")
def api_stops():
    query = request.args.get("q", "")
    return jsonify({"stops": _timetable.search_stops(query)})


@app.route("/api/stops/<stop_id>/departures")
def api_stop_departures(stop_id):
    if stop_id not in _timetable.stops:
        return jsonify({"error": "Onbekende halte"}), 404
    window_minutes = min(int(request.args.get("window_minutes", 90)), 240)
    departures = _timetable.next_departures(stop_id, int(time.time()), window_minutes=window_minutes)
    for d in departures:
        d.update(route_meta(d["route_id"]))
    stop = _timetable.stops[stop_id]
    return jsonify({
        "stop": {"stop_id": stop_id, "name": stop.get("name", ""), "lat": stop.get("lat"), "lon": stop.get("lon")},
        "departures": departures,
        "count": len(departures),
    })


@app.route("/api/routes/<route_id>/shape")
def api_route_shape(route_id):
    """Vereenvoudigd routetraject (lijst van [lat,lon]-lijsten) voor het
    tekenen van de lijn op de kaart. Zonder ?direction_id= worden alle
    bekende richtingen teruggegeven (meestal 2: heen en terug, over elkaar
    getekend); met een geldige ?direction_id=0|1 alleen het traject van die
    kant op, zodat een specifiek voertuig niet per ongeluk mede de terugrit
    laat zien. Leeg als de lijn geen shape_id had in de GTFS-feed of niet
    bestaat."""
    shapes_by_direction = _route_shapes.get(route_id, {})
    direction_id = request.args.get("direction_id")
    if isinstance(shapes_by_direction, list):
        # Oude data/utrecht_shapes.json (vóór de richting-uitsplitsing, zie
        # build_static_index.py) -- voorkomt een crash totdat de statische
        # index opnieuw is gebouwd; valt terug op alle richtingen tonen.
        shapes = shapes_by_direction
    elif direction_id is not None and direction_id in shapes_by_direction:
        shapes = [shapes_by_direction[direction_id]]
    else:
        shapes = list(shapes_by_direction.values())
    return jsonify({"route_id": route_id, "shapes": shapes})


@app.route("/api/trips/<trip_id>/nearby-stops")
def api_trip_nearby_stops(trip_id):
    """Begin- en eindpunt van de rit (voor het kaart-popup), rechtstreeks uit
    de statische dienstregeling (eerste/laatste halte op stop_sequence,
    gevuld door build_static_index.py). Eerder werd dit afgeleid uit live
    trip_delays-posities ('vorige/volgende halte t.o.v. nu'), maar dat bleek
    onbetrouwbaar -- kon zelfs een halte in de tegenovergestelde richting
    tonen (trip_delays-snapshots liepen soms niet netjes op stop_sequence
    op)."""
    meta = _timetable.trip_meta.get(trip_id, {})

    def stop_info(stop_id):
        if not stop_id:
            return None
        return {"stop_id": stop_id, "name": _index.stops.get(stop_id, {}).get("name", stop_id)}

    return jsonify({
        "trip_id": trip_id,
        "first_stop": stop_info(meta.get("first_stop_id")),
        "last_stop": stop_info(meta.get("last_stop_id")),
    })


def _cancellation_totals(since_date, until_date):
    """Compacte uitval-samenvatting (totalen + percentage per operator) over
    een datumrange -- gebruikt voor de vergelijking met de vorige periode in
    /api/cancellations. Zelfde tel-definitie als daar: vervallen ritten vs.
    daadwerkelijk waargenomen gereden ritten, alleen buslijnen."""
    conn = db.get_conn()
    try:
        canceled_rows = conn.execute(
            """SELECT route_id, COUNT(*) AS cnt FROM trip_cancellations
               WHERE service_date >= ? AND service_date <= ? GROUP BY route_id""",
            (since_date, until_date),
        ).fetchall()
        ran_rows = conn.execute(
            """SELECT r.route_id, COUNT(*) AS cnt
               FROM trips_ran_daily r
               WHERE r.service_date >= ? AND r.service_date <= ?
                 AND NOT EXISTS (
                     SELECT 1 FROM trip_cancellations c
                     WHERE c.trip_id = r.trip_id AND c.service_date = r.service_date
                 )
               GROUP BY r.route_id""",
            (since_date, until_date),
        ).fetchall()
    finally:
        conn.close()

    total_canceled = total_ran = 0
    per_operator = defaultdict(lambda: {"canceled": 0, "ran": 0})
    for r in canceled_rows:
        if not _index.is_bus_route(r["route_id"]):
            continue
        total_canceled += r["cnt"]
        per_operator[route_meta(r["route_id"])["operator"]]["canceled"] += r["cnt"]
    for r in ran_rows:
        if not _index.is_bus_route(r["route_id"]):
            continue
        total_ran += r["cnt"]
        per_operator[route_meta(r["route_id"])["operator"]]["ran"] += r["cnt"]

    total = total_canceled + total_ran
    return {
        "since_date": since_date,
        "until_date": until_date,
        "total_canceled": total_canceled,
        "total_ran": total_ran,
        "cancellation_pct": round(100.0 * total_canceled / total, 1) if total else 0.0,
        "per_operator": {
            op: {
                "canceled": a["canceled"], "ran": a["ran"],
                "cancellation_pct": round(100.0 * a["canceled"] / (a["canceled"] + a["ran"]), 1)
                if (a["canceled"] + a["ran"]) else 0.0,
            }
            for op, a in per_operator.items()
        },
    }


@app.route("/api/cancellations")
def api_cancellations():
    """Aantal uitgevallen (CANCELED) ritten over een gekozen periode.

    'Uitvalpercentage' wordt berekend als vervallen / (vervallen + daadwerkelijk
    gereden), waarbij 'gereden' is afgeleid uit de realtime feed (ritten waarvoor
    we minstens één halte-update zagen). Dit is een praktische benadering op
    basis van wat de realtime feeds daadwerkelijk rapporteren, geen exacte
    telling tegen de volledige dienstregeling.

    Optioneel ?up_to_now=1: vervoerders melden een uitgevallen rit soms al
    ruim voordat de geplande vertrektijd is verstreken. Zonder deze vlag telt
    zo'n vooraf aangekondigde uitval voor later vandaag al mee in de teller,
    terwijl 'gereden' vanzelfsprekend alleen ritten bevat die al daadwerkelijk
    zijn waargenomen -- dat scheeft het percentage van de nog lopende dag
    omhoog (uitval hele dag vs. gereden tot nu toe). Met ?up_to_now=1 worden
    vervallen ritten van VANDAAG met een nog niet verstreken start_time
    genegeerd, ongeacht welke ?range= is gekozen; voltooide dagen in de
    periode zijn hoe dan ook al compleet en blijven ongewijzigd."""
    range_key = request.args.get("range", "today")
    up_to_now = request.args.get("up_to_now") in ("1", "true", "yes")
    # Bovengrens is niet per se vandaag (bv. 'last_month' is een afgesloten,
    # volledig verleden periode) -- today_str blijft apart nodig voor de
    # up_to_now-vergelijking hieronder, die altijd de daadwerkelijke datum
    # van vandaag bedoelt, ongeacht de gekozen range.
    since_date, until_date = _date_bounds_for_range(range_key)
    today_str = date.today().isoformat()
    now_time_str = datetime.now().strftime("%H:%M:%S")

    conn = db.get_conn()
    try:
        canceled_rows = conn.execute(
            """SELECT service_date, route_id, start_time, COUNT(*) AS cnt
               FROM trip_cancellations WHERE service_date >= ? AND service_date <= ?
               GROUP BY service_date, route_id, start_time""",
            (since_date, until_date),
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
            (since_date, until_date),
        ).fetchall()
    finally:
        conn.close()

    daily = {}
    route_canceled = {}
    route_ran = {}
    weekday_canceled = [0] * 7
    weekday_ran = [0] * 7
    hour_canceled = [0] * 24
    # Zelfde tellingen, maar gesplitst per operator (Keolis/Transdev) zodat
    # het uitval-dashboard een verschil tussen de twee concessies kan tonen.
    daily_by_op = defaultdict(lambda: defaultdict(lambda: {"canceled": 0, "ran": 0}))
    weekday_by_op = defaultdict(lambda: {"canceled": [0] * 7, "ran": [0] * 7})
    hour_by_op = defaultdict(lambda: [0] * 24)
    week_by_op = defaultdict(lambda: defaultdict(lambda: {"canceled": 0, "ran": 0}))
    month_by_op = defaultdict(lambda: defaultdict(lambda: {"canceled": 0, "ran": 0}))
    for r in canceled_rows:
        if not _index.is_bus_route(r["route_id"]):
            continue  # historische rij van een lijn die niet meer in de huidige index zit
        if (up_to_now and r["service_date"] == today_str
                and r["start_time"] and r["start_time"] > now_time_str):
            continue  # vooraf aangekondigde uitval voor een vertrektijd die nog moet komen
        operator = route_meta(r["route_id"])["operator"]
        d = daily.setdefault(r["service_date"], {"canceled": 0, "ran": 0})
        d["canceled"] += r["cnt"]
        daily_by_op[operator][r["service_date"]]["canceled"] += r["cnt"]
        route_canceled[r["route_id"]] = route_canceled.get(r["route_id"], 0) + r["cnt"]
        service_date = date.fromisoformat(r["service_date"])
        weekday = service_date.weekday()
        weekday_canceled[weekday] += r["cnt"]
        weekday_by_op[operator]["canceled"][weekday] += r["cnt"]
        week_by_op[operator][_iso_week_key(service_date)]["canceled"] += r["cnt"]
        month_by_op[operator][r["service_date"][:7]]["canceled"] += r["cnt"]
        if r["start_time"]:
            try:
                hour = int(r["start_time"].split(":")[0]) % 24
                hour_canceled[hour] += r["cnt"]
                hour_by_op[operator][hour] += r["cnt"]
            except (ValueError, IndexError):
                pass
    for r in ran_rows:
        if not _index.is_bus_route(r["route_id"]):
            continue  # historische rij van een lijn die niet meer in de huidige index zit
        operator = route_meta(r["route_id"])["operator"]
        d = daily.setdefault(r["service_date"], {"canceled": 0, "ran": 0})
        d["ran"] += r["cnt"]
        daily_by_op[operator][r["service_date"]]["ran"] += r["cnt"]
        route_ran[r["route_id"]] = route_ran.get(r["route_id"], 0) + r["cnt"]
        service_date = date.fromisoformat(r["service_date"])
        weekday = service_date.weekday()
        weekday_ran[weekday] += r["cnt"]
        weekday_by_op[operator]["ran"][weekday] += r["cnt"]
        week_by_op[operator][_iso_week_key(service_date)]["ran"] += r["cnt"]
        month_by_op[operator][r["service_date"][:7]]["ran"] += r["cnt"]

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
    per_operator_acc = {}
    for rid in set(route_canceled) | set(route_ran):
        c = route_canceled.get(rid, 0)
        r = route_ran.get(rid, 0)
        meta = route_meta(rid)
        # Operator-optelling moet over alle lijnen gaan (ook lijnen zonder
        # uitval die dag), anders wordt het percentage berekend over een
        # instabiele deelverzameling en klopt het niet met het totaal.
        agg = per_operator_acc.setdefault(meta["operator"], {"canceled": 0, "ran": 0})
        agg["canceled"] += c
        agg["ran"] += r
        if c == 0:
            continue  # in de lijnen-tabel alleen lijnen met minstens 1 vervallen rit tonen
        rtotal = c + r
        per_route.append({
            **meta, "canceled": c, "ran": r,
            "cancellation_pct": round(100.0 * c / rtotal, 1) if rtotal else 0.0,
        })
    per_route.sort(key=lambda x: -x["canceled"])

    per_operator = []
    for name, a in per_operator_acc.items():
        atotal = a["canceled"] + a["ran"]
        per_operator.append({
            "operator": name, "canceled": a["canceled"], "ran": a["ran"],
            "cancellation_pct": round(100.0 * a["canceled"] / atotal, 1) if atotal else 0.0,
        })
    per_operator.sort(key=lambda x: -x["canceled"])

    per_weekday = []
    for i, name in enumerate(WEEKDAY_NAMES_NL):
        c, r = weekday_canceled[i], weekday_ran[i]
        wtotal = c + r
        per_weekday.append({
            "weekday": name, "canceled": c, "ran": r,
            "cancellation_pct": round(100.0 * c / wtotal, 1) if wtotal else 0.0,
        })

    per_hour = [{"hour": h, "canceled": hour_canceled[h]} for h in range(24)]

    operators_present = sorted(
        set(daily_by_op) | set(weekday_by_op) | set(hour_by_op) | set(week_by_op) | set(month_by_op)
    )

    daily_by_operator = {}
    for op in operators_present:
        rows_by_date = daily_by_op[op]
        lst = []
        for d in sorted(rows_by_date.keys()):
            c, r = rows_by_date[d]["canceled"], rows_by_date[d]["ran"]
            total_op = c + r
            lst.append({
                "date": d, "canceled": c, "ran": r,
                "cancellation_pct": round(100.0 * c / total_op, 1) if total_op else 0.0,
            })
        daily_by_operator[op] = lst

    per_weekday_by_operator = {}
    for op in operators_present:
        wk = weekday_by_op[op]
        lst = []
        for i, name in enumerate(WEEKDAY_NAMES_NL):
            c, r = wk["canceled"][i], wk["ran"][i]
            wtotal = c + r
            lst.append({
                "weekday": name, "canceled": c, "ran": r,
                "cancellation_pct": round(100.0 * c / wtotal, 1) if wtotal else 0.0,
            })
        per_weekday_by_operator[op] = lst

    per_hour_by_operator = {
        op: [{"hour": h, "canceled": hour_by_op[op][h]} for h in range(24)]
        for op in operators_present
    }

    per_week_by_operator = {}
    for op in operators_present:
        weeks = week_by_op[op]
        lst = []
        for week_key in sorted(weeks.keys()):
            c, r = weeks[week_key]["canceled"], weeks[week_key]["ran"]
            wtotal = c + r
            iso_year, iso_week = week_key.split("-W")
            week_start = date.fromisocalendar(int(iso_year), int(iso_week), 1).isoformat()
            lst.append({
                "week": week_key, "week_start": week_start,
                "canceled": c, "ran": r,
                "cancellation_pct": round(100.0 * c / wtotal, 1) if wtotal else 0.0,
            })
        per_week_by_operator[op] = lst

    per_month_by_operator = {}
    for op in operators_present:
        months = month_by_op[op]
        lst = []
        for month_key in sorted(months.keys()):
            c, r = months[month_key]["canceled"], months[month_key]["ran"]
            mtotal = c + r
            lst.append({
                "month": month_key,
                "canceled": c, "ran": r,
                "cancellation_pct": round(100.0 * c / mtotal, 1) if mtotal else 0.0,
            })
        per_month_by_operator[op] = lst

    # Vorige periode van gelijke lengte, direct voorafgaand aan de gekozen
    # periode -- voor de delta's op de KPI-tegels. 'all' heeft geen vorige
    # periode. NB: up_to_now wordt hier bewust niet toegepast (de vorige
    # periode ligt volledig in het verleden); bij range=today met up_to_now
    # vergelijk je dus vandaag-tot-nu-toe met heel gisteren -- een lichte
    # scheefheid die we accepteren om het simpel te houden.
    previous = None
    if range_key != "all":
        period_days = (date.fromisoformat(until_date) - date.fromisoformat(since_date)).days
        prev_until = date.fromisoformat(since_date) - timedelta(days=1)
        prev_since = prev_until - timedelta(days=period_days)
        previous = _cancellation_totals(prev_since.isoformat(), prev_until.isoformat())
        if previous["total_canceled"] + previous["total_ran"] == 0:
            # Helemaal geen data in de vorige periode (bv. de collector
            # draaide toen nog niet) -- een delta t.o.v. "0%" zou misleidend
            # zijn, dus dan liever geen delta tonen.
            previous = None

    return jsonify({
        "range": range_key,
        "since_date": since_date,
        "until_date": until_date,
        "up_to_now": up_to_now,
        "total_canceled": total_canceled,
        "total_ran": total_ran,
        "cancellation_pct": round(100.0 * total_canceled / total, 1) if total else 0.0,
        "daily": daily_list,
        "daily_by_operator": daily_by_operator,
        "per_route": per_route,
        "per_operator": per_operator,
        "per_weekday": per_weekday,
        "per_weekday_by_operator": per_weekday_by_operator,
        "per_hour": per_hour,
        "per_hour_by_operator": per_hour_by_operator,
        "per_week_by_operator": per_week_by_operator,
        "per_month_by_operator": per_month_by_operator,
        "previous": previous,
    })


@app.route("/api/cancellations/trips")
def api_cancellation_trips():
    """Drill-down: individuele vervallen ritten, gepagineerd en optioneel
    gefilterd op lijn/operator, voor wie de ruwe data wil inzien."""
    range_key = request.args.get("range", "today")
    since_date, until_date = _date_bounds_for_range(range_key)
    route_id = request.args.get("route_id")
    operator = request.args.get("operator")
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = max(int(request.args.get("offset", 0)), 0)

    route_ids_filter = None
    if operator:
        route_ids_filter = {rid for rid, r in _index.routes.items() if r.get("operator") == operator}

    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT trip_id, service_date, route_id, start_time, first_seen, last_seen
               FROM trip_cancellations
               WHERE service_date >= ? AND service_date <= ?
               ORDER BY service_date DESC, start_time DESC""",
            (since_date, until_date),
        ).fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        if not _index.is_bus_route(r["route_id"]):
            continue  # historische rij van een lijn die niet meer in de huidige index zit
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


@app.route("/api/skipped-stops")
def api_skipped_stops():
    """Haltes die vaak worden overgeslagen (rit rijdt door zonder te stoppen --
    GTFS-RT stop_time_update SKIPPED), los van complete rit-uitval. Zelfde
    range-keys als /api/cancellations, standaard de laatste 30 dagen."""
    range_key = request.args.get("range", "30d")
    since_date, until_date = _date_bounds_for_range(range_key)
    limit = min(int(request.args.get("limit", 30)), 100)

    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT stop_id, route_id, COUNT(*) AS cnt
               FROM skipped_stops
               WHERE service_date >= ? AND service_date <= ?
               GROUP BY stop_id, route_id""",
            (since_date, until_date),
        ).fetchall()
    finally:
        conn.close()

    per_stop = {}
    for r in rows:
        if not _index.is_bus_route(r["route_id"]):
            continue  # historische rij van een lijn die niet meer in de huidige index zit
        entry = per_stop.setdefault(r["stop_id"], {"total": 0, "routes": {}})
        entry["total"] += r["cnt"]
        meta = route_meta(r["route_id"])
        entry["routes"][meta["short_name"]] = entry["routes"].get(meta["short_name"], 0) + r["cnt"]

    items = [
        {
            "stop_id": stop_id,
            "stop_name": _index.stops.get(stop_id, {}).get("name", stop_id),
            "count": data["total"],
            "routes": sorted(data["routes"].items(), key=lambda kv: -kv[1]),
        }
        for stop_id, data in per_stop.items()
    ]
    items.sort(key=lambda x: -x["count"])

    return jsonify({
        "since_date": since_date,
        "until_date": until_date,
        "items": items[:limit],
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
