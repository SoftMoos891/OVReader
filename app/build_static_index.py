"""
Bouwt een gefilterde index van GTFS statische data voor U-OV: de concessie
voor het openbaar vervoer in de provincie Utrecht, uitgevoerd door Keolis en
Transdev onder de gezamenlijke merknaam U-OV (agency_id "UOV" in de
landelijke feed).

Downloadt (indien nodig) de landelijke statische GTFS-feed van OVapi, filtert
routes.txt op agency_id "UOV" en route_type bus of U-tram, en leidt daaruit de
bijbehorende trips/haltes af. Resultaat wordt weggeschreven als compacte
JSON-bestanden die de realtime-fetchers gebruiken om alleen U-OV-data te
verwerken (geen Qbuzz/Connexxion/Arriva/GVB/NS-bussen die toevallig de
provincie doorkruisen).

Herhaal dit script periodiek (bv. wekelijks) om dienstregelingswijzigingen
bij te houden; de statische feed zelf verandert niet elke minuut.
"""
import csv
import io
import json
import math
import sys
import time
import zipfile
from pathlib import Path

import requests

from .concession_mapping import classify_operator, UNKNOWN

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
GTFS_ZIP_URL = "https://gtfs.ovapi.nl/nl/gtfs-nl.zip"
GTFS_ZIP_PATH = DATA_DIR / "gtfs-nl.zip"
OUT_STOPS = DATA_DIR / "utrecht_stops.json"
OUT_ROUTES = DATA_DIR / "utrecht_routes.json"
OUT_TRIPS = DATA_DIR / "utrecht_trips.json"
OUT_CALENDAR = DATA_DIR / "utrecht_calendar.json"
OUT_TRIP_META = DATA_DIR / "utrecht_trip_meta.json"
OUT_STOP_TIMES = DATA_DIR / "utrecht_stop_times.json"
OUT_SHAPES = DATA_DIR / "utrecht_shapes.json"
OUT_REALTIME_TRIPS = DATA_DIR / "utrecht_realtime_trips.json"
# Bevat zowel feed_info.txt-velden als de HTTP-cachekopjes van de vorige
# download; zie load_feed_state()/download_gtfs_zip().
FEED_STATE_PATH = DATA_DIR / "gtfs_feed_info.json"

# Ophogen zodra de vórm van de weggeschreven bestanden verandert (nieuw
# bestand, nieuw veld). Zonder dit zou een build met ongewijzigde
# feed_version worden overgeslagen en dus nooit de nieuwe velden opleveren.
BUILD_VERSION = 4

# Tolerantie voor de Ramer-Douglas-Peucker-vereenvoudiging van routelijnen,
# in graden (~0.00005 graden is ~5m op deze breedtegraad) -- ver onder wat op
# een kaart nog zichtbaar verschil maakt, maar scheelt in de praktijk ~80% van
# de punten (785k -> 165k voor heel U-OV).
SHAPE_SIMPLIFY_TOLERANCE_DEGREES = 0.00005

WEEKDAY_FIELDS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

TARGET_AGENCY_ID = "UOV"
BUS_ROUTE_TYPE = "3"
TRAM_ROUTE_TYPE = "0"  # U-tram 20/21/22 (Transdev, Utrecht Binnen)

csv.field_size_limit(sys.maxsize)


def log(msg):
    print(f"[build_static_index] {msg}", flush=True)


def load_feed_state():
    """Wat we van de vorige geslaagde build onthouden (HTTP-cachekopjes +
    feed_version), of een leeg dict als dit de eerste keer is."""
    if not FEED_STATE_PATH.exists():
        return {}
    try:
        return json.loads(FEED_STATE_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}  # corrupt/onleesbaar: gewoon opnieuw opbouwen


def download_gtfs_zip(state, force=False):
    """Haalt de landelijke GTFS-feed op, maar slaat de download over als de
    server zelf zegt dat er niets is gewijzigd. Geeft True terug als er
    daadwerkelijk nieuwe bytes zijn opgehaald.

    Let op het verschil met de bug die hier ooit zat: die versie sloeg de
    download over zodra data/gtfs-nl.zip simpelweg bestond, waardoor het
    script na de eerste keer voorgoed op een bevroren zip bleef bouwen -- de
    trip_id's liepen stilzwijgend uit de pas met de live feed en elke rit
    toonde 'Onbekend'. Hier neemt niet het bestaan van een bestand die
    beslissing, maar de server: we sturen de Last-Modified/ETag terug die we
    bij de vorige download van diezelfde server kregen, en alleen als die
    met 304 (Not Modified) antwoordt hergebruiken we de kopie op schijf.
    Wijzigt de feed, dan krijgen we gewoon 200 met verse bytes.

    Extra vangnetten: zonder bestaande zip (of met --force) wordt er
    sowieso gedownload, en een onleesbare/afgekapte zip wordt door de
    zipfile-module in main() alsnog als fout gemeld."""
    headers = {}
    if not force and GTFS_ZIP_PATH.exists():
        if state.get("http_last_modified"):
            headers["If-Modified-Since"] = state["http_last_modified"]
        if state.get("http_etag"):
            headers["If-None-Match"] = state["http_etag"]

    log("Controleer of de landelijke statische GTFS-feed is gewijzigd...")
    with requests.get(GTFS_ZIP_URL, headers=headers, stream=True, timeout=120) as r:
        if r.status_code == 304:
            log("Feed ongewijzigd sinds de vorige build (HTTP 304) -- download overgeslagen.")
            return False
        r.raise_for_status()
        log("Feed is gewijzigd; download (~230 MB)...")
        with open(GTFS_ZIP_PATH, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
        state["http_last_modified"] = r.headers.get("Last-Modified")
        state["http_etag"] = r.headers.get("ETag")
    log("Download klaar.")
    return True


def read_feed_info(zf):
    """feed_info.txt uit de GTFS-feed: publicatieversie en geldigheidsduur
    van de dienstregeling. Daarmee kunnen we (a) een ongewijzigde feed
    herkennen en het dure herbouwen overslaan, en (b) in de app tonen welke
    dienstregelingversie er eigenlijk geladen is."""
    try:
        with zf.open("feed_info.txt") as f:
            row = next(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")), None)
    except KeyError:
        return {}
    if not row:
        return {}
    return {
        "feed_version": row.get("feed_version", ""),
        "feed_start_date": row.get("feed_start_date", ""),
        "feed_end_date": row.get("feed_end_date", ""),
        "feed_publisher_name": row.get("feed_publisher_name", ""),
    }


def find_uov_routes(zf):
    """Filtert routes.txt op de U-OV-concessie: bus (route_type 3) en de
    U-tram (route_type 0 -- alleen Transdev/Utrecht Binnen rijdt tram in
    deze concessie)."""
    routes = {}
    with zf.open("routes.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            if row.get("agency_id") == TARGET_AGENCY_ID and row.get("route_type") in (BUS_ROUTE_TYPE, TRAM_ROUTE_TYPE):
                routes[row["route_id"]] = {
                    "agency_id": row["agency_id"],
                    "short_name": row.get("route_short_name", ""),
                    "long_name": row.get("route_long_name", ""),
                    "route_type": row.get("route_type", ""),
                    # Officiële merkkleur van de lijn (hex zonder '#'), voor
                    # een kleurherkenbare weergave los van de vertragingskleur
                    # op de kaart. Leeg als de feed 'm niet meegeeft.
                    "color": row.get("route_color", ""),
                    "text_color": row.get("route_text_color", ""),
                }
    return routes


def load_agency_name(zf):
    with zf.open("agency.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            if row["agency_id"] == TARGET_AGENCY_ID:
                return row.get("agency_name", TARGET_AGENCY_ID)
    return TARGET_AGENCY_ID


def find_trips_for_routes(zf, route_ids):
    """Filtert trips.txt op de gevonden U-OV buslijnen. Geeft naast de simpele
    trip->route-mapping (gebruikt door de realtime-fetchers) ook trip_meta
    terug (service_id + headsign, gebruikt door de haltezoeker/dienstregeling).

    shape_id/direction_id gaan niet in trip_meta (die blijft klein en wordt
    per trip opgevraagd): ze worden hier apart geteld zodat
    find_dominant_shapes() alleen de meest gebruikte shape per lijn+richting
    hoeft te bewaren, niet elke shape_id per trip."""
    trip_to_route = {}
    trip_meta = {}
    shape_counts = {}  # (route_id, direction_id) -> {shape_id: aantal trips}
    realtime_trips = {}  # realtime_trip_id -> {route_id, headsign}
    with zf.open("trips.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            if row["route_id"] in route_ids:
                trip_to_route[row["trip_id"]] = row["route_id"]
                trip_meta[row["trip_id"]] = {
                    "route_id": row["route_id"],
                    "service_id": row.get("service_id", ""),
                    "headsign": row.get("trip_headsign", ""),
                }
                # OVapi's eigen, semantische ritsleutel
                # ("KEOLIS:5056:40001") -- overleeft een hernummering van
                # trip_id's bij een dienstregelingwijziging, zie
                # UtrechtIndex.realtime_trip_meta_for() in gtfs_rt.py. Veel
                # trip_id's (dienstdagen) delen er één, maar route en
                # headsign zijn per realtime_trip_id eenduidig, dus
                # overschrijven is hier onschadelijk.
                realtime_trip_id = row.get("realtime_trip_id")
                if realtime_trip_id:
                    realtime_trips[realtime_trip_id] = {
                        "route_id": row["route_id"],
                        "headsign": row.get("trip_headsign", ""),
                    }
                shape_id = row.get("shape_id")
                if shape_id:
                    key = (row["route_id"], row.get("direction_id", ""))
                    counts = shape_counts.setdefault(key, {})
                    counts[shape_id] = counts.get(shape_id, 0) + 1
    return trip_to_route, trip_meta, shape_counts, realtime_trips


def find_dominant_shapes(shape_counts):
    """Kiest per (lijn, richting) de shape_id die door de meeste trips wordt
    gebruikt -- dat is in de praktijk het 'normale' tracé; zeldzame
    omleidingsvarianten met een eigen shape_id (soms tientallen per lijn,
    bv. na dienstregelingswijzigingen) vallen zo weg. Geeft terug:
    route_id -> {direction_id: shape_id}. De richting blijft hier expliciet
    bewaard (i.p.v. afgevlakt tot één lijst) zodat de kaart later alleen het
    traject van de kant op kan tekenen die het geselecteerde voertuig ook
    daadwerkelijk rijdt, in plaats van heen- en terugrit altijd over elkaar
    heen te tonen."""
    route_shapes = {}
    for (route_id, direction), counts in shape_counts.items():
        dominant_shape = max(counts.items(), key=lambda kv: kv[1])[0]
        route_shapes.setdefault(route_id, {})[direction] = dominant_shape
    return route_shapes


def _rdp_simplify(points, epsilon):
    """Ramer-Douglas-Peucker, in pure Python (geen extra geo-dependency,
    zelfde aanpak als de handmatige point-in-polygon-check in
    ns_rail_alerts.py). points is een lijst van (lat, lon)-tuples."""
    if len(points) < 3:
        return points

    def perp_distance(pt, start, end):
        if start == end:
            return math.hypot(pt[0] - start[0], pt[1] - start[1])
        x1, y1 = start
        x2, y2 = end
        x0, y0 = pt
        num = abs((y2 - y1) * x0 - (x2 - x1) * y0 + x2 * y1 - y2 * x1)
        den = math.hypot(y2 - y1, x2 - x1)
        return num / den

    max_dist, max_idx = 0.0, 0
    for i in range(1, len(points) - 1):
        d = perp_distance(points[i], points[0], points[-1])
        if d > max_dist:
            max_dist, max_idx = d, i

    if max_dist > epsilon:
        left = _rdp_simplify(points[:max_idx + 1], epsilon)
        right = _rdp_simplify(points[max_idx:], epsilon)
        return left[:-1] + right
    return [points[0], points[-1]]


def load_and_simplify_shapes(zf, shape_ids):
    """Leest shapes.txt (landelijk, dus wederom een groot bestand), beperkt
    tot de gevraagde shape_ids, en vereenvoudigt elke shape met RDP -- zie
    SHAPE_SIMPLIFY_TOLERANCE_DEGREES."""
    raw = {}
    with zf.open("shapes.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            sid = row["shape_id"]
            if sid not in shape_ids:
                continue
            raw.setdefault(sid, []).append(
                (int(row["shape_pt_sequence"]), float(row["shape_pt_lat"]), float(row["shape_pt_lon"]))
            )

    simplified = {}
    for sid, pts in raw.items():
        ordered = [(lat, lon) for _seq, lat, lon in sorted(pts)]
        simplified[sid] = _rdp_simplify(ordered, SHAPE_SIMPLIFY_TOLERANCE_DEGREES)
    return simplified


def find_calendar_for_services(zf, service_ids):
    """Parseert calendar.txt + calendar_dates.txt, beperkt tot de service_ids
    die daadwerkelijk door U-OV-trips gebruikt worden."""
    calendar = {}
    try:
        with zf.open("calendar.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                if row["service_id"] not in service_ids:
                    continue
                calendar[row["service_id"]] = {
                    "days": [row.get(day) == "1" for day in WEEKDAY_FIELDS],
                    "start_date": row.get("start_date", ""),
                    "end_date": row.get("end_date", ""),
                    "added": [],
                    "removed": [],
                }
    except KeyError:
        pass  # sommige GTFS-feeds laten calendar.txt weg en gebruiken alleen calendar_dates.txt

    try:
        with zf.open("calendar_dates.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                sid = row["service_id"]
                if sid not in service_ids:
                    continue
                entry = calendar.setdefault(
                    sid, {"days": [False] * 7, "start_date": "", "end_date": "", "added": [], "removed": []}
                )
                bucket = "added" if row.get("exception_type") == "1" else "removed"
                entry[bucket].append(row["date"])
    except KeyError:
        pass

    return calendar


def find_stop_times_for_trips(zf, trip_ids):
    """Streamt stop_times.txt (groot bestand, landelijk -- vaak tientallen
    miljoenen regels waarvan er maar een klein deel U-OV is) om te bepalen
    welke haltes deze trips aandoen (gebruikt door de alerts-fallback) en om
    de vertrektijden per halte te verzamelen (gebruikt door de haltezoeker/
    dienstregeling).

    Gebruikt bewust csv.reader (niet DictReader) met handmatige kolomindex, en
    slaat een match op als compacte tuple (niet als dict met vier keys): een
    dict per rij voor de volledige landelijke feed kost op een kleine VPS al
    gauw te veel geheugen/CPU-tijd.

    Houdt ook, per trip, de halte met de laagste en hoogste stop_sequence bij
    (trip_termini) -- dit is het echte begin- en eindpunt van de rit volgens
    de dienstregeling, in tegenstelling tot een 'vorige/volgende halte'
    afgeleid uit live posities (bleek onbetrouwbaar: die kon bv. een halte in
    de tegenovergestelde richting tonen).

    pickup_type bepaalt of een halte-tijd überhaupt een *vertrek* is:

      0  normaal instappen                          -> (trip_id, seq, tijd)
      1  niet instappen, alleen uitstappen          -> helemaal overgeslagen
      2  alleen op afroep (U-Flex)                  -> (trip_id, seq, tijd, 2)

    Type 1 is in de praktijk de eindhalte van vrijwel elke rit (184.237 van
    de 4,07 mln U-OV halte-tijden, tegenover 177.076 ritten): daar komt de
    bus aan en gaat 'ie uit dienst. Die stonden in de haltezoeker als gewoon
    vertrek tussen de rest -- een tijd waarop je niet mee kunt.

    De halte zelf blijft wél in stop_ids meetellen, ook als er alleen
    type 1-rijen voor bestaan: anders zou een halte die uitsluitend
    eindpunt is helemaal uit utrecht_stops.json verdwijnen en daarmee ook
    uit het zoeken-op-naam en de meldingen."""
    stop_times_by_stop = {}
    trip_termini = {}  # trip_id -> [min_seq, min_stop_id, max_seq, max_stop_id]
    with zf.open("stop_times.txt") as f:
        text = io.TextIOWrapper(f, encoding="utf-8-sig")
        reader = csv.reader(text)
        header = next(reader)
        idx = {name: i for i, name in enumerate(header)}
        i_trip, i_stop, i_seq = idx["trip_id"], idx["stop_id"], idx["stop_sequence"]
        i_arr, i_dep = idx["arrival_time"], idx["departure_time"]
        i_pickup = idx.get("pickup_type")
        for i, row in enumerate(reader):
            trip_id = row[i_trip]
            if trip_id in trip_ids:
                try:
                    stop_sequence = int(row[i_seq])
                except (ValueError, IndexError):
                    stop_sequence = 0
                time_str = row[i_dep] or row[i_arr]
                stop_id = row[i_stop]
                pickup = row[i_pickup] if i_pickup is not None else "0"
                # setdefault ook bij een overgeslagen vertrek: de halte moet
                # blijven bestaan, alleen deze tijd hoort er niet bij.
                entries = stop_times_by_stop.setdefault(stop_id, [])
                if pickup != "1":
                    # Vierde element alleen bij op-afroep (0,3% van de rijen);
                    # standaard weggelaten om dit bestand -- 4 mln rijen, ~117
                    # MB, volledig in het geheugen van elke webworker -- niet
                    # onnodig te laten groeien. Lezers moeten dus op lengte
                    # controleren, zie Timetable.next_departures().
                    entries.append(
                        (trip_id, stop_sequence, time_str, 2) if pickup == "2"
                        else (trip_id, stop_sequence, time_str)
                    )
                termini = trip_termini.get(trip_id)
                if termini is None:
                    trip_termini[trip_id] = [stop_sequence, stop_id, stop_sequence, stop_id]
                else:
                    if stop_sequence < termini[0]:
                        termini[0], termini[1] = stop_sequence, stop_id
                    if stop_sequence > termini[2]:
                        termini[2], termini[3] = stop_sequence, stop_id
            if i % 2_000_000 == 0 and i:
                log(f"  ...{i:,} stop_times regels verwerkt, {len(stop_times_by_stop):,} haltes gevonden")
    return set(stop_times_by_stop), stop_times_by_stop, trip_termini


def load_stop_info(zf, stop_ids):
    stops = {}
    with zf.open("stops.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            if row["stop_id"] in stop_ids:
                try:
                    lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
                except (KeyError, ValueError):
                    continue
                stop = {"name": row.get("stop_name", ""), "lat": lat, "lon": lon}
                # Officieel perronveld uit de GTFS-feed. Veel bruikbaarder dan
                # het perron uit de haltenaam raden: gevuld voor ~250 haltes
                # tegenover ~60 met een "(C1)"-achtig achtervoegsel in de naam.
                # Zonder dit veld bleef de perronkolom leeg op precies de
                # plekken waar 'ie het hardst nodig is -- bv. de zeven perrons
                # van Utrecht CS Jaarbeurszijde heten allemaal exact hetzelfde.
                if row.get("platform_code"):
                    stop["platform_code"] = row["platform_code"]
                stops[row["stop_id"]] = stop
    return stops


ALL_OUTPUTS = (
    OUT_STOPS, OUT_ROUTES, OUT_TRIPS, OUT_CALENDAR,
    OUT_TRIP_META, OUT_STOP_TIMES, OUT_SHAPES, OUT_REALTIME_TRIPS,
)


def main():
    force = "--force" in sys.argv
    DATA_DIR.mkdir(exist_ok=True)
    state = load_feed_state()
    download_gtfs_zip(state, force=force)

    with zipfile.ZipFile(GTFS_ZIP_PATH) as zf:
        feed_info = read_feed_info(zf)
        if feed_info.get("feed_version"):
            log(
                f"Dienstregelingversie {feed_info['feed_version']} "
                f"(geldig {feed_info.get('feed_start_date','?')}-{feed_info.get('feed_end_date','?')})."
            )

        # Alleen overslaan als álles klopt: zelfde dienstregelingversie,
        # zelfde outputformaat, en elk verwacht bestand daadwerkelijk
        # aanwezig. Dat laatste vangt ook het geval af waarin deze code een
        # nieuw bestand is gaan schrijven dat er nog niet is.
        unchanged = (
            not force
            and feed_info.get("feed_version")
            and state.get("feed_version") == feed_info["feed_version"]
            and state.get("build_version") == BUILD_VERSION
            and all(p.exists() for p in ALL_OUTPUTS)
        )
        if unchanged:
            log("Dienstregeling en outputformaat ongewijzigd -- herbouw overgeslagen (--force forceert).")
            return

        log(f"Filter routes.txt op agency_id={TARGET_AGENCY_ID!r}, bus (route_type=3) + U-tram (route_type=0)...")
        routes = find_uov_routes(zf)
        agency_name = load_agency_name(zf)
        for r in routes.values():
            r["agency_name"] = agency_name
        log(f"{len(routes)} U-OV lijnen gevonden (bus + tram).")

        trip_to_route, trip_meta, shape_counts, realtime_trips = find_trips_for_routes(zf, set(routes))
        log(f"{len(trip_to_route):,} trips gevonden voor deze lijnen "
            f"({len(realtime_trips):,} unieke realtime_trip_id's).")

        service_ids = {m["service_id"] for m in trip_meta.values() if m["service_id"]}
        log(f"Parse calendar.txt/calendar_dates.txt voor {len(service_ids)} service_ids...")
        calendar = find_calendar_for_services(zf, service_ids)

        log("Bepaal haltes en halte-tijden die deze trips aandoen (doorzoekt een groot bestand, even geduld)...")
        stop_ids, stop_times_by_stop, trip_termini = find_stop_times_for_trips(zf, set(trip_to_route))
        stop_info = load_stop_info(zf, stop_ids)
        log(f"{len(stop_info):,} haltes gevonden, {sum(len(v) for v in stop_times_by_stop.values()):,} halte-tijden.")

        for trip_id, (_min_seq, first_stop_id, _max_seq, last_stop_id) in trip_termini.items():
            if trip_id in trip_meta:
                trip_meta[trip_id]["first_stop_id"] = first_stop_id
                trip_meta[trip_id]["last_stop_id"] = last_stop_id

        route_shapes = find_dominant_shapes(shape_counts)
        needed_shape_ids = {sid for shapes in route_shapes.values() for sid in shapes.values()}
        log(f"Bepaal {len(needed_shape_ids)} dominante routetraject(en) (van {sum(len(c) for c in shape_counts.values())} totaal) en vereenvoudig ze...")
        simplified_shapes = load_and_simplify_shapes(zf, needed_shape_ids)
        route_shape_points = {
            route_id: {
                direction: simplified_shapes[sid]
                for direction, sid in shapes.items() if sid in simplified_shapes
            }
            for route_id, shapes in route_shapes.items()
        }
        total_points = sum(len(s) for shapes in route_shape_points.values() for s in shapes.values())
        log(f"Routetrajecten vereenvoudigd: {total_points:,} punten voor {len(route_shape_points)} lijnen.")

    line_names = sorted({r["short_name"] for r in routes.values()}, key=lambda s: (len(s), s))
    log(f"Lijnnummers ({len(line_names)}): {', '.join(line_names)}")

    for r in routes.values():
        r["operator"] = classify_operator(r["short_name"], r["long_name"])
    operator_counts = {}
    unknown_lines = []
    for r in routes.values():
        operator_counts[r["operator"]] = operator_counts.get(r["operator"], 0) + 1
        if r["operator"] == UNKNOWN:
            unknown_lines.append(f"{r['short_name']} ({r['long_name']})")
    log(f"Verdeling per operator/modaliteit: {operator_counts}")
    if unknown_lines:
        log(
            "WAARSCHUWING: onherkende lijn(en), niet toegewezen aan Keolis of "
            "Transdev -- voeg toe aan app/concession_mapping.py: " + "; ".join(unknown_lines)
        )

    OUT_STOPS.write_text(json.dumps(stop_info, ensure_ascii=False), encoding="utf-8")
    OUT_ROUTES.write_text(json.dumps(routes, ensure_ascii=False), encoding="utf-8")
    OUT_TRIPS.write_text(json.dumps(trip_to_route, ensure_ascii=False), encoding="utf-8")
    OUT_CALENDAR.write_text(json.dumps(calendar, ensure_ascii=False), encoding="utf-8")
    OUT_TRIP_META.write_text(json.dumps(trip_meta, ensure_ascii=False), encoding="utf-8")
    OUT_STOP_TIMES.write_text(json.dumps(stop_times_by_stop, ensure_ascii=False), encoding="utf-8")
    OUT_SHAPES.write_text(json.dumps(route_shape_points, ensure_ascii=False), encoding="utf-8")
    OUT_REALTIME_TRIPS.write_text(json.dumps(realtime_trips, ensure_ascii=False), encoding="utf-8")
    log(
        f"Weggeschreven: {OUT_STOPS.name}, {OUT_ROUTES.name}, {OUT_TRIPS.name}, "
        f"{OUT_CALENDAR.name}, {OUT_TRIP_META.name}, {OUT_STOP_TIMES.name}, "
        f"{OUT_SHAPES.name}, {OUT_REALTIME_TRIPS.name}"
    )

    # Pas ná een geslaagde build wegschrijven: crasht het script halverwege,
    # dan blijft de oude state staan en probeert de volgende run het opnieuw
    # i.p.v. de halve build als "klaar" te beschouwen.
    state.update(feed_info)
    state["build_version"] = BUILD_VERSION
    state["built_at"] = int(time.time())
    FEED_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
