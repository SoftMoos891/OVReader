"""OV-trendalarm (15 okt 2026, Danny koos suggestie 18): lijnen die de afgelopen week duidelijk
slechter reden dan in de vier weken daarvoor, als tip voor de redactie.

Week = de laatste 7 afgesloten dagen, basis = de 28 dagen daarvoor. Per lijn:
  - uitval: uitgevallen / (gereden + uitgevallen), uit trip_cancellations en trips_ran_daily
    (zelfde telling als /lite/api/uitval/daily);
  - stiptheid: on_time_count / sample_count en de gemiddelde vertraging uit route_stats_daily.
Een lijn telt als "duidelijk slechter" bij:
  - uitval: minstens UITVAL_MIN_RITTEN uitgevallen ritten, minstens UITVAL_MIN_PCT procent, en
    zowel twee keer zo hoog als UITVAL_MIN_VERSCHIL procentpunt hoger dan normaal;
  - stiptheid: genoeg metingen in beide periodes en STIPT_MIN_DALING procentpunt minder op tijd
    (en op minstens één dag meer slecht dan bij uitval: stiptheid schommelt meer);
  - EN op minstens MIN_SLECHTE_DAGEN van de 7 dagen afzonderlijk zo slecht. Eén rare dag telt dus
    niet: op 14 okt 2026 kwamen er tussen 22 en 24 uur duizenden "uitval"-meldingen binnen voor
    ritten die al geweest waren (vermoedelijk een hapering in de OVapi-feed), en zonder deze eis
    stonden toen 82 lijnen in het alarm.
"""
from datetime import date, timedelta

UITVAL_MIN_RITTEN = 15
UITVAL_MIN_PCT = 3.0
UITVAL_MIN_VERSCHIL = 2.0
STIPT_MIN_DALING = 10.0
STIPT_MIN_METINGEN_WEEK = 1000
STIPT_MIN_METINGEN_BASIS = 3000
MIN_SLECHTE_DAGEN = 3
RARE_DAG_MIN_PCT = 20.0
RARE_DAG_FACTOR = 4.0
DAG_MIN_UITVAL = 3
DAG_MIN_METINGEN = 150


def perioden(vandaag):
    week_eind = vandaag - timedelta(days=1)
    week_begin = vandaag - timedelta(days=7)
    basis_eind = week_begin - timedelta(days=1)
    basis_begin = week_begin - timedelta(days=28)
    return (week_begin, week_eind), (basis_begin, basis_eind)


def _uitval(conn, van, tot, is_lijn):
    """{route: {dag: [uitgevallen, gereden]}}"""
    uit = {}
    for r in conn.execute("SELECT service_date AS d, route_id, COUNT(*) AS n FROM trip_cancellations WHERE service_date BETWEEN ? AND ? GROUP BY 1, 2",
                          (van.isoformat(), tot.isoformat())):
        if r["route_id"] and is_lijn(r["route_id"]):
            uit.setdefault(r["route_id"], {}).setdefault(r["d"], [0, 0])[0] += r["n"]
    for r in conn.execute("""SELECT r.service_date AS d, r.route_id, COUNT(*) AS n FROM trips_ran_daily r
                             WHERE r.service_date BETWEEN ? AND ? AND NOT EXISTS (
                               SELECT 1 FROM trip_cancellations c WHERE c.trip_id = r.trip_id AND c.service_date = r.service_date)
                             GROUP BY 1, 2""", (van.isoformat(), tot.isoformat())):
        if r["route_id"] and is_lijn(r["route_id"]):
            uit.setdefault(r["route_id"], {}).setdefault(r["d"], [0, 0])[1] += r["n"]
    return uit


def _som(per_dag):
    return [sum(v[0] for v in per_dag.values()), sum(v[1] for v in per_dag.values())]


def _stiptheid(conn, van, tot, is_lijn):
    """{route: {"metingen", "op_tijd_pct", "vertraging_s", "dagen": {dag: (metingen, op_tijd_pct)}}}"""
    uit = {}
    for r in conn.execute("""SELECT day, route_id, sample_count AS s, on_time_count AS o, avg_delay_seconds AS v
                             FROM route_stats_daily WHERE day BETWEEN ? AND ?""", (van.isoformat(), tot.isoformat())):
        if not (r["route_id"] and is_lijn(r["route_id"]) and r["s"]):
            continue
        x = uit.setdefault(r["route_id"], {"s": 0, "o": 0, "v": 0.0, "dagen": {}})
        x["s"] += r["s"]; x["o"] += r["o"]; x["v"] += r["v"] * r["s"]
        x["dagen"][r["day"]] = (r["s"], 100.0 * r["o"] / r["s"])
    return {k: {"metingen": x["s"], "op_tijd_pct": 100.0 * x["o"] / x["s"], "vertraging_s": x["v"] / x["s"], "dagen": x["dagen"]}
            for k, x in uit.items()}


def rare_dagen(*per_route):
    """Dagen waarop het hele netwerk ineens extreem veel uitval had (> RARE_DAG_MIN_PCT procent én
    RARE_DAG_FACTOR keer de mediaan): vrijwel altijd een hapering in de data, zoals 14 okt 2026
    22-24u. Die dagen tellen nergens mee."""
    tot = {}
    for pr in per_route:
        for dagen in pr.values():
            for d, (c, g) in dagen.items():
                t = tot.setdefault(d, [0, 0]); t[0] += c; t[1] += g
    p = {d: 100.0 * c / (c + g) for d, (c, g) in tot.items() if c + g}
    if not p:
        return set()
    mediaan = sorted(p.values())[len(p) // 2]
    return {d for d, x in p.items() if x > RARE_DAG_MIN_PCT and x > RARE_DAG_FACTOR * mediaan}


def trendalarm(conn, vandaag=None, is_lijn=lambda r: True, meta=lambda r: {"route_id": r}):
    vandaag = vandaag or date.today()
    (wb, we), (bb, be) = perioden(vandaag)
    uw, ub = _uitval(conn, wb, we, is_lijn), _uitval(conn, bb, be, is_lijn)
    sw, sb = _stiptheid(conn, wb, we, is_lijn), _stiptheid(conn, bb, be, is_lijn)
    raar = rare_dagen(uw, ub)
    for pr in (uw, ub):
        for dagen in pr.values():
            for d in raar & set(dagen):
                del dagen[d]
    lijnen = []
    for route in sorted(set(uw) | set(sw)):
        redenen, cijfers = [], {}
        c, g = _som(uw.get(route, {}))
        bc, bg = _som(ub.get(route, {}))
        if c + g and bc + bg:
            pct, bpct = 100.0 * c / (c + g), 100.0 * bc / (bc + bg)
            grens = max(UITVAL_MIN_PCT, 2 * bpct, bpct + UITVAL_MIN_VERSCHIL)
            slecht = sum(1 for dc, dg in uw.get(route, {}).values() if dc >= DAG_MIN_UITVAL and 100.0 * dc / (dc + dg) >= grens)
            cijfers["uitval"] = {"week_ritten": c, "week_pct": round(pct, 1), "normaal_pct": round(bpct, 1), "slechte_dagen": slecht}
            if c >= UITVAL_MIN_RITTEN and pct >= grens and slecht >= MIN_SLECHTE_DAGEN:
                redenen.append("uitval")
        w, b = sw.get(route), sb.get(route)
        if w and b:
            slecht = sum(1 for s, p in w["dagen"].values() if s >= DAG_MIN_METINGEN and b["op_tijd_pct"] - p >= STIPT_MIN_DALING)
            cijfers["stiptheid"] = {"week_pct": round(w["op_tijd_pct"], 1), "normaal_pct": round(b["op_tijd_pct"], 1),
                                    "week_vertraging_s": round(w["vertraging_s"]), "normaal_vertraging_s": round(b["vertraging_s"]),
                                    "slechte_dagen": slecht}
            if (w["metingen"] >= STIPT_MIN_METINGEN_WEEK and b["metingen"] >= STIPT_MIN_METINGEN_BASIS
                    and b["op_tijd_pct"] - w["op_tijd_pct"] >= STIPT_MIN_DALING and slecht >= MIN_SLECHTE_DAGEN + 1):
                redenen.append("stiptheid")
        if redenen:
            lijnen.append({**meta(route), "redenen": redenen, **cijfers})
    # Ergste eerst: uitvalverschil en stiptheidsdaling bij elkaar opgeteld.
    ernst = lambda l: (l.get("uitval", {}).get("week_pct", 0) - l.get("uitval", {}).get("normaal_pct", 0)) + \
                      (l.get("stiptheid", {}).get("normaal_pct", 0) - l.get("stiptheid", {}).get("week_pct", 0))
    lijnen.sort(key=ernst, reverse=True)
    return {"week": [wb.isoformat(), we.isoformat()], "normaal": [bb.isoformat(), be.isoformat()],
            "uitgesloten_dagen": sorted(raar), "lijnen": lijnen}
