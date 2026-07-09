"""Automatische 'record'-signalering: slechtste (en beste) dagen op basis van
de al opgebouwde dag- en dienstregelingsstatistieken (route_stats_daily,
trip_delays, trip_cancellations/trips_ran_daily) -- zodat je niet zelf door de
trends hoeft te spitten om te zien of er iets nieuwswaardigs zit.

Zelfde 'op tijd'-definitie als de rest van de app (zie server.py/collector.py):
tussen ON_TIME_MIN_DELAY en ON_TIME_MAX_DELAY seconden telt als op tijd."""
from datetime import date, timedelta

ON_TIME_MIN_DELAY = -120
ON_TIME_MAX_DELAY = 180

# Drempels om te voorkomen dat een dag met nauwelijks metingen (bv. de eerste
# dag dat de collector draaide, of een dag met een feed-storing) een "record"
# lijkt terwijl het gewoon te weinig data is.
MIN_SAMPLES_NETWORK = 100
MIN_SAMPLES_ROUTE = 20
MIN_SAMPLES_OPERATOR = 50
MIN_TRIPS_CANCELLATION = 20

# Begrenst hoe ver terug de records-scan gaat. route_stats_daily/
# trip_cancellations/trips_ran_daily worden nooit (volledig) opgeruimd, dus
# zonder ondergrens zou deze scan blijven groeien met de leeftijd van de
# installatie. Twee jaar is ruim genoeg om "records" zinvol te houden zonder
# de query onbeperkt te laten meegroeien.
MAX_HISTORY_DAYS = 730


def _history_cutoff():
    return (date.today() - timedelta(days=MAX_HISTORY_DAYS)).isoformat()


def _route_ontime_daily(conn, index):
    """Lijst van {day, route_id, ...route_meta, sample_count, on_time_count,
    on_time_pct}, per (dag, lijn) -- raw trip_delays (zie RETENTION_DAYS in
    app/collector.py) aangevuld met opgerolde route_stats_daily voor oudere
    dagen (tot MAX_HISTORY_DAYS terug)."""
    raw = conn.execute(
        """
        SELECT strftime('%Y-%m-%d', fetched_at, 'unixepoch', 'localtime') AS day,
               route_id,
               COUNT(*) AS sample_count,
               SUM(CASE WHEN COALESCE(arrival_delay, departure_delay, 0) BETWEEN ? AND ? THEN 1 ELSE 0 END) AS on_time_count
        FROM trip_delays
        GROUP BY day, route_id
        """,
        (ON_TIME_MIN_DELAY, ON_TIME_MAX_DELAY),
    ).fetchall()
    rolled = conn.execute(
        "SELECT day, route_id, sample_count, on_time_count FROM route_stats_daily WHERE day >= ?",
        (_history_cutoff(),),
    ).fetchall()

    by_key = {}
    for r in list(raw) + list(rolled):
        if not index.is_bus_route(r["route_id"]):
            continue
        key = (r["day"], r["route_id"])
        e = by_key.setdefault(key, {"sample_count": 0, "on_time_count": 0})
        e["sample_count"] += r["sample_count"] or 0
        e["on_time_count"] += r["on_time_count"] or 0

    return by_key


def _cancellation_daily(conn, index):
    """dict: (day, route_id) -> {canceled, ran}, tot MAX_HISTORY_DAYS terug."""
    cutoff = _history_cutoff()
    canceled_rows = conn.execute(
        "SELECT service_date, route_id, COUNT(*) AS cnt FROM trip_cancellations "
        "WHERE service_date >= ? GROUP BY service_date, route_id",
        (cutoff,),
    ).fetchall()
    ran_rows = conn.execute(
        """
        SELECT r.service_date, r.route_id, COUNT(*) AS cnt
        FROM trips_ran_daily r
        WHERE r.service_date >= ? AND NOT EXISTS (
            SELECT 1 FROM trip_cancellations c
            WHERE c.trip_id = r.trip_id AND c.service_date = r.service_date
        )
        GROUP BY r.service_date, r.route_id
        """,
        (cutoff,),
    ).fetchall()

    by_key = {}
    for r in canceled_rows:
        if not index.is_bus_route(r["route_id"]):
            continue
        key = (r["service_date"], r["route_id"])
        by_key.setdefault(key, {"canceled": 0, "ran": 0})["canceled"] += r["cnt"]
    for r in ran_rows:
        if not index.is_bus_route(r["route_id"]):
            continue
        key = (r["service_date"], r["route_id"])
        by_key.setdefault(key, {"canceled": 0, "ran": 0})["ran"] += r["cnt"]

    return by_key


def _extreme(series, value_key, threshold_key, min_value, direction, since_day=None):
    """Geeft het item met de laagste ('min') of hoogste ('max') value_key
    terug uit series, beperkt tot items die minstens min_value van
    threshold_key hebben (en optioneel niet ouder zijn dan since_day)."""
    candidates = [
        r for r in series
        if r[threshold_key] >= min_value and (since_day is None or r["day"] >= since_day)
    ]
    if not candidates:
        return None
    picker = min if direction == "min" else max
    return picker(candidates, key=lambda r: r[value_key])


def find_records(conn, index, route_meta_fn):
    """Bouwt de curated lijst van records: netwerkbreed, per operator, per
    lijn (op tijd) en netwerkbreed/per operator (uitval), voor 'ooit', 'deze
    maand' en (voor het netwerk) 'deze week'."""
    today = date.today()
    month_start = today.replace(day=1).isoformat()
    week_start = (today - timedelta(days=today.weekday())).isoformat()

    # -- Op tijd: per (dag, lijn) ophalen, en daaruit netwerk/operator-niveau afleiden --
    ontime_by_route = _route_ontime_daily(conn, index)

    route_series = []
    network_by_day = {}
    operator_by_key = {}
    for (day, route_id), e in ontime_by_route.items():
        if e["sample_count"] == 0:
            continue
        meta = route_meta_fn(route_id)
        route_series.append({
            "day": day, **meta,
            "sample_count": e["sample_count"], "on_time_count": e["on_time_count"],
            "on_time_pct": round(100.0 * e["on_time_count"] / e["sample_count"], 1),
        })
        nd = network_by_day.setdefault(day, {"sample_count": 0, "on_time_count": 0})
        nd["sample_count"] += e["sample_count"]
        nd["on_time_count"] += e["on_time_count"]
        ok = operator_by_key.setdefault((day, meta["operator"]), {"sample_count": 0, "on_time_count": 0})
        ok["sample_count"] += e["sample_count"]
        ok["on_time_count"] += e["on_time_count"]

    network_series = [
        {"day": day, "sample_count": e["sample_count"],
         "on_time_pct": round(100.0 * e["on_time_count"] / e["sample_count"], 1)}
        for day, e in network_by_day.items() if e["sample_count"] > 0
    ]
    operator_series = [
        {"day": day, "operator": operator, "sample_count": e["sample_count"],
         "on_time_pct": round(100.0 * e["on_time_count"] / e["sample_count"], 1)}
        for (day, operator), e in operator_by_key.items() if e["sample_count"] > 0
    ]

    # -- Uitval: per (dag, lijn), en daaruit netwerk/operator-niveau afleiden --
    cancel_by_route = _cancellation_daily(conn, index)

    cancel_network_by_day = {}
    cancel_operator_by_key = {}
    for (day, route_id), e in cancel_by_route.items():
        total = e["canceled"] + e["ran"]
        if total == 0:
            continue
        operator = route_meta_fn(route_id)["operator"]
        nd = cancel_network_by_day.setdefault(day, {"canceled": 0, "ran": 0})
        nd["canceled"] += e["canceled"]
        nd["ran"] += e["ran"]
        ok = cancel_operator_by_key.setdefault((day, operator), {"canceled": 0, "ran": 0})
        ok["canceled"] += e["canceled"]
        ok["ran"] += e["ran"]

    cancel_network_series = [
        {"day": day, "canceled": e["canceled"], "ran": e["ran"], "total": e["canceled"] + e["ran"],
         "cancellation_pct": round(100.0 * e["canceled"] / (e["canceled"] + e["ran"]), 1)}
        for day, e in cancel_network_by_day.items()
    ]
    cancel_operator_series = [
        {"day": day, "operator": operator, "canceled": e["canceled"], "ran": e["ran"],
         "total": e["canceled"] + e["ran"],
         "cancellation_pct": round(100.0 * e["canceled"] / (e["canceled"] + e["ran"]), 1)}
        for (day, operator), e in cancel_operator_by_key.items()
    ]

    def ontime_worst(series, min_samples, since_day=None):
        return _extreme(series, "on_time_pct", "sample_count", min_samples, "min", since_day)

    def ontime_best(series, min_samples, since_day=None):
        return _extreme(series, "on_time_pct", "sample_count", min_samples, "max", since_day)

    def cancel_worst(series, min_total, since_day=None):
        return _extreme(series, "cancellation_pct", "total", min_total, "max", since_day)

    operators = sorted({r["operator"] for r in operator_series})

    result = {
        "network": {
            "worst_all_time": ontime_worst(network_series, MIN_SAMPLES_NETWORK),
            "worst_month": ontime_worst(network_series, MIN_SAMPLES_NETWORK, month_start),
            "worst_week": ontime_worst(network_series, MIN_SAMPLES_NETWORK, week_start),
            "best_all_time": ontime_best(network_series, MIN_SAMPLES_NETWORK),
        },
        "routes": {
            "worst_all_time": ontime_worst(route_series, MIN_SAMPLES_ROUTE),
            "worst_month": ontime_worst(route_series, MIN_SAMPLES_ROUTE, month_start),
        },
        "operators": {
            op: {
                "worst_all_time": ontime_worst([r for r in operator_series if r["operator"] == op], MIN_SAMPLES_OPERATOR),
                "worst_month": ontime_worst([r for r in operator_series if r["operator"] == op], MIN_SAMPLES_OPERATOR, month_start),
            }
            for op in operators
        },
        "cancellations": {
            "worst_all_time": cancel_worst(cancel_network_series, MIN_TRIPS_CANCELLATION),
            "worst_month": cancel_worst(cancel_network_series, MIN_TRIPS_CANCELLATION, month_start),
        },
    }

    thresholds = {
        "network": MIN_SAMPLES_NETWORK, "route": MIN_SAMPLES_ROUTE,
        "operator": MIN_SAMPLES_OPERATOR, "cancellation_total": MIN_TRIPS_CANCELLATION,
    }
    return result, thresholds
