"""Automatische 'record'-signalering: netwerkbreed hoogste uitvalpercentage,
op basis van de al opgebouwde trip_cancellations/trips_ran_daily-tellingen --
zodat je niet zelf door de historie hoeft te spitten om te zien of er iets
nieuwswaardigs zit.

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

# Begrenst hoe ver terug de records-scan gaat. trip_cancellations/
# trips_ran_daily worden nooit (volledig) opgeruimd, dus zonder ondergrens
# zou deze scan blijven groeien met de leeftijd van de installatie. Twee jaar
# is ruim genoeg om "records" zinvol te houden zonder de query onbeperkt te
# laten meegroeien.
MAX_HISTORY_DAYS = 730


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
    """Bouwt de curated lijst van uitval-records: netwerkbreed, voor 'ooit'
    en 'deze maand'."""
    month_start = date.today().replace(day=1).isoformat()

    cancel_by_route = _cancellation_daily(conn, index)

    cancel_network_by_day = {}
    for (day, route_id), e in cancel_by_route.items():
        total = e["canceled"] + e["ran"]
        if total == 0:
            continue
        nd = cancel_network_by_day.setdefault(day, {"canceled": 0, "ran": 0})
        nd["canceled"] += e["canceled"]
        nd["ran"] += e["ran"]

    cancel_network_series = [
        {"day": day, "canceled": e["canceled"], "ran": e["ran"], "total": e["canceled"] + e["ran"],
         "cancellation_pct": round(100.0 * e["canceled"] / (e["canceled"] + e["ran"]), 1)}
        for day, e in cancel_network_by_day.items()
    ]

    def cancel_worst(series, min_total, since_day=None):
        return _extreme(series, "cancellation_pct", "total", min_total, "max", since_day)

    result = {
        "cancellations": {
            "worst_all_time": cancel_worst(cancel_network_series, MIN_TRIPS_CANCELLATION),
            "worst_month": cancel_worst(cancel_network_series, MIN_TRIPS_CANCELLATION, month_start),
        },
    }
    thresholds = {"cancellation_total": MIN_TRIPS_CANCELLATION}
    return result, thresholds
