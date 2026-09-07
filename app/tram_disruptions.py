"""Verstoringen op de U-tram (lijn 20/21/22) uit de U-OV-meldingenfeed.

De tram loopt in dit project overal mee als bijvangst: de meldingenfeed
(app/gtfs_rt.fetch_alerts) is er één voor heel U-OV, en de tram is de enige
modaliteit daarin waarvoor uitvalcijfers ontbreken (zie is_bus_route() in
app/gtfs_rt.py). Daardoor was er tot nu toe nergens te zien hoe vaak de tram
er nou eigenlijk uit ligt. Deze module beantwoordt dat uit de al verzamelde
`alerts`-tabel, die -- anders dan trip_delays/vehicle_positions -- nooit wordt
opgeschoond en dus de volledige historie bevat.

Het lastige is dat de feed niet zegt of een melding over de tram gaat. Zie
is_tram_alert() voor hoe dat wel bepaald wordt.
"""
import re
import time
from collections import Counter
from datetime import date, timedelta

# Meldingen komen per stuk binnen, maar één storing levert er meestal een reeks
# van op (eerst "vertraging", dan "traject vervalt", dan "opgelost") die elkaar
# opvolgen. Meldingen die binnen dit venster op elkaar aansluiten worden tot één
# storingsperiode samengevoegd, zodat "hoe vaak ligt de tram eruit" niet de
# praatgraagheid van de vervoerder meet. Ruim genomen: het gat tussen twee
# meldingen over dezelfde storing is in de praktijk minuten, tussen twee losse
# storingen op een dag uren.
INCIDENT_MERGE_GAP_SECONDS = 30 * 60

# Een melding die ruim vóór het beginmoment van de situatie zelf verschijnt is
# een aankondiging (werkzaamheden, evenement), geen storing. Twee uur is ver
# voorbij wat een spontane storing ooit haalt -- die staat er binnen minuten.
ANNOUNCEMENT_LEAD_SECONDS = 2 * 3600

# Een situatie met een officiële geldigheidsperiode van meer dan een dag is
# per definitie ingepland: een acute storing heeft er geen, of hooguit een van
# een paar uur. Vangt de aankondigingen die noch een geplande oorzaak melden
# noch ver vooruit verschijnen ("Halte is buiten gebruik van 7 t/m 11
# september", oorzaak "Stremming", vanaf morgen).
MULTI_DAY_WINDOW_SECONDS = 24 * 3600

# GTFS-RT Alert.cause-waarden die een geplande situatie aanduiden.
PLANNED_GTFS_CAUSES = {"MAINTENANCE", "CONSTRUCTION"}

# De oorzaak zoals U-OV die zelf in de beschrijving zet ("Oorzaak : Stremming").
# Veel specifieker dan Alert.cause, dat in de praktijk blijft steken op
# OTHER_CAUSE/UNKNOWN_CAUSE.
_REPORTED_CAUSE_RE = re.compile(r"^\s*Oorzaak\s*:\s*(.+?)\s*$", re.MULTILINE)
_REPORTED_EFFECT_RE = re.compile(r"^\s*Effect\s*:\s*(.+?)\s*$", re.MULTILINE)

# Oorzaken (zoals U-OV ze schrijft) die op gepland werk duiden.
PLANNED_REPORTED_CAUSES = {"werkzaamheden"}

# "tram", "trams", "tramverkeer", "tramstoring", "tramhalte"...
_TRAM_WORD_RE = re.compile(r"\btram\w*", re.IGNORECASE)

# Meldingen die met een lijnnummer beginnen ("Lijn 31: bus rijdt niet verder
# dan...") gaan over díe lijn, ook als het woord "tram" verderop valt omdat
# reizigers erop kunnen overstappen.
_LEADING_LINE_RE = re.compile(r"^\s*lijn\s+([0-9]+[A-Za-z]?)\b", re.IGNORECASE)

# Nette Nederlandse labels voor de GTFS-RT-oorzaken, gebruikt als U-OV zelf
# geen "Oorzaak :"-regel meestuurt.
GTFS_CAUSE_LABELS = {
    "ACCIDENT": "Ongeval",
    "CONSTRUCTION": "Werkzaamheden",
    "DEMONSTRATION": "Demonstratie",
    "HOLIDAY": "Feestdag",
    "MAINTENANCE": "Onderhoud",
    "MEDICAL_EMERGENCY": "Medisch incident",
    "OTHER_CAUSE": "Overig",
    "POLICE_ACTIVITY": "Politieoptreden",
    "STRIKE": "Staking",
    "TECHNICAL_PROBLEM": "Technische storing",
    "UNKNOWN_CAUSE": "Niet gemeld",
    "WEATHER": "Weer",
}

WEEKDAY_LABELS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def _local_date(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def _local_hour(epoch):
    return time.localtime(epoch).tm_hour


def _weekday(iso_date):
    return date.fromisoformat(iso_date).weekday()


def _dates_between(since_date, until_date):
    """Alle dagen in de periode, ook die zonder melding -- anders zou een
    grafiek van meldingen per dag de rustige dagen gewoon weglaten en daarmee
    precies het tegenovergestelde beeld geven."""
    day, last = date.fromisoformat(since_date), date.fromisoformat(until_date)
    days = []
    while day <= last:
        days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def _split_ids(value):
    return {part for part in (value or "").split(",") if part}


def is_tram_alert(row, index):
    """Gaat deze U-OV-melding over de tram?

    De feed zelf zegt het niet: van de trammeldingen in de historie had er
    géén enkele een route_id ingevuld. Wat ze wél hebben is een lijst betrokken
    haltes, en tramhaltes zijn exclusief -- geen van de 62 haltes van lijn
    20/21/22 wordt ook door een bus bediend. Dat maakt een halte-overlap een
    harde match, en die dekt ruim tweederde van de gevallen, inclusief de
    generieke teksten ("vanwege een storing is de dienstregeling tijdelijk
    verstoord") waar geen enkel woord verraadt dat het om de tram gaat.

    De tekstregel is het vangnet voor meldingen zonder haltes ("Wegens een
    tramstoring rijden de trams niet"). Die mag niet aanslaan op een busmelding
    die de tram alleen als overstapadvies noemt, vandaar de twee uitsluitingen:
    een melding die aan een buslijn hangt, of die met een busnummer begint, is
    geen trammelding.
    """
    tram_routes = index.tram_route_ids()
    route_ids = _split_ids(row["route_ids"])
    if route_ids & tram_routes:
        return True
    if _split_ids(row["stop_ids"]) & index.tram_stop_ids:
        return True

    header = row["header"] or ""
    if not _TRAM_WORD_RE.search(f"{header} {row['description'] or ''}"):
        return False
    if route_ids - tram_routes:
        return False
    leading_line = _LEADING_LINE_RE.match(header)
    if leading_line:
        tram_line_names = {index.routes[rid].get("short_name") for rid in tram_routes}
        return leading_line.group(1) in tram_line_names
    return True


def reported_cause(row):
    """De oorzaak zoals U-OV die meldt, met de GTFS-oorzaak als terugval."""
    match = _REPORTED_CAUSE_RE.search(row["description"] or "")
    if match:
        return match.group(1)
    return GTFS_CAUSE_LABELS.get(row["cause"], "Niet gemeld")


def reported_effect(row):
    match = _REPORTED_EFFECT_RE.search(row["description"] or "")
    return match.group(1) if match else "Niet gemeld"


def is_planned(row):
    """Gepland werk/aangekondigde situatie i.p.v. een storing.

    Vier signalen, in volgorde van betrouwbaarheid: de gemelde oorzaak, de
    GTFS-oorzaak, en -- voor aankondigingen die geen van beide invullen, zoals
    "op 5 september rijden er geen trams i.v.m. FC Utrecht" -- de officiële
    geldigheidsperiode: die begint ruim later dan de melding verschijnt, of
    beslaat meer dan een dag. Geverifieerd tegen de volledige historie: elke
    trammelding met een meerdaagse geldigheidsperiode was gepland werk."""
    if reported_cause(row).strip().lower() in PLANNED_REPORTED_CAUSES:
        return True
    if row["cause"] in PLANNED_GTFS_CAUSES:
        return True
    valid_from, valid_until = row["valid_from"], row["valid_until"]
    if valid_from and valid_from > row["first_seen"] + ANNOUNCEMENT_LEAD_SECONDS:
        return True
    return bool(valid_from and valid_until
                and valid_until - valid_from >= MULTI_DAY_WINDOW_SECONDS)


def group_into_incidents(alerts, merge_gap_seconds=INCIDENT_MERGE_GAP_SECONDS):
    """Voegt meldingen die in de tijd op elkaar aansluiten samen tot één
    storingsperiode. Verwacht `alerts` oplopend op eerste waarneming."""
    incidents = []
    for alert in alerts:
        current = incidents[-1] if incidents else None
        if current and alert["first_seen"] <= current["ended_at"] + merge_gap_seconds:
            current["ended_at"] = max(current["ended_at"], alert["last_seen"])
            current["alerts"].append(alert)
        else:
            incidents.append({
                "started_at": alert["first_seen"],
                "ended_at": alert["last_seen"],
                "alerts": [alert],
            })
    for incident in incidents:
        incident["duration_seconds"] = incident["ended_at"] - incident["started_at"]
        # De oorzaak van een storingsperiode is die van de eerste melding: de
        # vervolgmeldingen beschrijven vaak het gevolg ("Eerdere verstoring")
        # in plaats van wat er gebeurd is.
        incident["cause"] = incident["alerts"][0]["cause_label"]
        incident["header"] = incident["alerts"][0]["header"]
    return incidents


def _counted(counter, total):
    """Aflopende telling met aandeel, voor de oorzaak-/gevolgverdelingen."""
    return [
        {"label": label, "count": count,
         "share_pct": round(100.0 * count / total, 1) if total else 0.0}
        for label, count in counter.most_common()
    ]


def _alert_view(row):
    """Eén melding, teruggebracht tot wat de tramanalyse nodig heeft."""
    return {
        "alert_id": row["alert_id"],
        "header": row["header"],
        "description": row["description"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "duration_seconds": row["last_seen"] - row["first_seen"],
        "cause_label": reported_cause(row),
        "effect_label": reported_effect(row),
        "gtfs_cause": row["cause"],
        "planned": is_planned(row),
    }


def build_overview(rows, since_date, until_date, max_incidents=40):
    """Bouwt het volledige antwoord van /api/tram/disruptions uit de al op tram
    gefilterde meldingen (oplopend op first_seen) binnen [since_date, until_date].

    Geplande situaties (werkzaamheden, aangekondigde evenementen) worden apart
    geteld en tellen niet mee in de storingscijfers: die zeggen iets over hoe
    druk de omleidingskalender is, niet over hoe vaak de tram er onaangekondigd
    uit ligt."""
    alerts = [_alert_view(row) for row in rows]
    disruptions = [a for a in alerts if not a["planned"]]
    planned = [a for a in alerts if a["planned"]]
    incidents = group_into_incidents(disruptions)

    days = _dates_between(since_date, until_date)
    per_day = {day: {"date": day, "alerts": 0, "incidents": 0, "disrupted_seconds": 0} for day in days}
    for alert in disruptions:
        day = per_day.get(_local_date(alert["first_seen"]))
        if day:
            day["alerts"] += 1
    for incident in incidents:
        day = per_day.get(_local_date(incident["started_at"]))
        if day:
            day["incidents"] += 1
            day["disrupted_seconds"] += incident["duration_seconds"]

    weekday_incidents = Counter()
    weekday_days = Counter()
    for day in days:
        weekday_days[_weekday(day)] += 1
    for incident in incidents:
        weekday_incidents[_weekday(_local_date(incident["started_at"]))] += 1

    hour_incidents = Counter(_local_hour(i["started_at"]) for i in incidents)
    durations = sorted(i["duration_seconds"] for i in incidents)
    days_with_disruption = sum(1 for d in per_day.values() if d["incidents"])

    return {
        "since_date": since_date,
        "until_date": until_date,
        "summary": {
            "days_in_range": len(days),
            "alert_count": len(disruptions),
            "incident_count": len(incidents),
            "planned_count": len(planned),
            "days_with_disruption": days_with_disruption,
            "days_with_disruption_pct": round(100.0 * days_with_disruption / len(days), 1) if days else 0.0,
            "incidents_per_week": round(7.0 * len(incidents) / len(days), 1) if days else 0.0,
            "total_disrupted_seconds": sum(durations),
            "median_duration_seconds": durations[len(durations) // 2] if durations else 0,
            "longest_incident": max(incidents, key=lambda i: i["duration_seconds"], default=None),
        },
        "per_day": [per_day[day] for day in days],
        "per_weekday": [
            {
                "weekday": index,
                "label": label,
                "incidents": weekday_incidents[index],
                "days_observed": weekday_days[index],
                # Per waargenomen dag, want een periode van 30 dagen bevat niet
                # van elke weekdag evenveel exemplaren -- zonder deling zou de
                # weekdag die één keer vaker voorkomt vanzelf "de slechtste" zijn.
                "incidents_per_day": round(weekday_incidents[index] / weekday_days[index], 2)
                if weekday_days[index] else 0.0,
            }
            for index, label in enumerate(WEEKDAY_LABELS)
        ],
        "per_hour": [{"hour": hour, "incidents": hour_incidents[hour]} for hour in range(24)],
        "causes": _counted(Counter(i["cause"] for i in incidents), len(incidents)),
        "effects": _counted(Counter(a["effect_label"] for a in disruptions), len(disruptions)),
        "planned_causes": _counted(Counter(a["cause_label"] for a in planned), len(planned)),
        # Nieuwste eerst: de lijst is om "wat was er laatst aan de hand" te lezen.
        "incidents": [
            {
                "started_at": i["started_at"],
                "ended_at": i["ended_at"],
                "duration_seconds": i["duration_seconds"],
                "cause": i["cause"],
                "header": i["header"],
                "alerts": i["alerts"],
            }
            for i in reversed(incidents[-max_incidents:])
        ],
    }
