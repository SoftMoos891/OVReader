"""Automatische 'record'-signalering: hoogste uitvalpercentage, netwerkbreed
en per operator (Keolis/Transdev), op basis van de al opgebouwde
trip_cancellations/trips_ran_daily-tellingen -- zodat je niet zelf door de
historie hoeft te spitten om te zien of er iets nieuwswaardigs zit.

Bevat bewust GEEN 'op tijd'-records meer: dat vereiste een GROUP BY over de
volledige trip_delays-tabel (op productieschaal tientallen miljoenen rijen),
verreweg de zwaarste query op /trends. trip_cancellations/trips_ran_daily
zijn intrinsiek compact (hoogstens 1 rij per rit per dag), dus deze module
blijft ook op grote installaties goedkoop."""
from datetime import date, timedelta

# Drempel om te voorkomen dat een dag met nauwelijks ritten (bv. de eerste
# dag dat de collector draaide, of een feed-storing) een "record" lijkt
# terwijl het gewoon te weinig data is.
MIN_TRIPS_CANCELLATION = 20

# Daarnaast: een minimum aan daadwerkelijk GEREDEN ritten op die dag. Als de
# collector (vrijwel) de hele dag plat lag, registreert hij wel de vooraf
# aangekondigde uitval (die blijft in de feed staan en wordt bij de eerste
# fetch alsnog opgepikt) maar nauwelijks gereden ritten -- zo'n dag lijkt dan
# op "100% uitval" terwijl het gewoon een gat in de eigen data is. Het netwerk
# rijdt normaal ruim duizend ritten per dag (per operator honderden), dus een
# dag met minder dan dit aantal waargenomen ritten is vrijwel zeker een
# datagat, geen echte uitvaldag.
MIN_RAN_TRIPS_CANCELLATION = 50

# Begrenst hoe ver terug de records-scan gaat. Gelijk aan
# CANCELLATION_HISTORY_RETENTION_DAYS in app/collector.py (de bewaartermijn
# van trip_cancellations/trips_ran_daily) -- verder terugkijken dan die
# tabellen bewaard blijven heeft toch geen zin.
MAX_HISTORY_DAYS = 1825  # 5 jaar


def _history_cutoff():
    return (date.today() - timedelta(days=MAX_HISTORY_DAYS)).isoformat()


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


def find_records(conn, index):
    """Bouwt de curated lijst van uitval-records: netwerkbreed en per
    operator (Keolis/Transdev), voor 'ooit' en 'deze maand'. Transdev tram
    komt hier nooit in voor -- _cancellation_daily() filtert al op
    is_bus_route(), consistent met de rest van de uitvalcijfers in de app."""
    month_start = date.today().replace(day=1).isoformat()

    cancel_by_route = _cancellation_daily(conn, index)

    cancel_network_by_day = {}
    cancel_operator_by_key = {}
    for (day, route_id), e in cancel_by_route.items():
        total = e["canceled"] + e["ran"]
        if total == 0:
            continue
        operator = index.routes.get(route_id, {}).get("operator", "Onbekend")
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

    def cancel_worst(series, min_total, since_day=None):
        # Dubbele drempel: genoeg ritten in totaal EN genoeg daadwerkelijk
        # gereden (zie MIN_RAN_TRIPS_CANCELLATION) -- anders wint een
        # collector-uitvaldag met schijnbaar "100% uitval" altijd.
        reliable = [r for r in series if r["ran"] >= MIN_RAN_TRIPS_CANCELLATION]
        return _extreme(reliable, "cancellation_pct", "total", min_total, "max", since_day)

    operators = sorted({op for _day, op in cancel_operator_by_key.keys()})

    result = {
        "cancellations": {
            "worst_all_time": cancel_worst(cancel_network_series, MIN_TRIPS_CANCELLATION),
            "worst_month": cancel_worst(cancel_network_series, MIN_TRIPS_CANCELLATION, month_start),
        },
        "cancellations_by_operator": {
            op: {
                "worst_all_time": cancel_worst(
                    [r for r in cancel_operator_series if r["operator"] == op], MIN_TRIPS_CANCELLATION),
                "worst_month": cancel_worst(
                    [r for r in cancel_operator_series if r["operator"] == op], MIN_TRIPS_CANCELLATION, month_start),
            }
            for op in operators
        },
    }
    thresholds = {
        "cancellation_total": MIN_TRIPS_CANCELLATION,
        "cancellation_ran": MIN_RAN_TRIPS_CANCELLATION,
    }
    return result, thresholds
