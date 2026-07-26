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


def download_gtfs_zip():
    if GTFS_ZIP_PATH.exists():
        log(f"Gebruik gecachte GTFS-zip ({GTFS_ZIP_PATH.stat().st_size / 1e6:.0f} MB)")
        return
    log("Download landelijke statische GTFS-feed (~230 MB)...")
    with requests.get(GTFS_ZIP_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(GTFS_ZIP_PATH, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    log("Download klaar.")


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


# Regio-vergelijking (uitvalpercentage stadsvervoer): GVB/RET/HTM zitten in
# dezelfde landelijke feed als U-OV, dus geen aparte databron nodig. Bewust
# GEEN eigen trip_id->route_id-mapping of stop_times zoals bij U-OV -- de
# realtime feed geeft voor deze agency's altijd al een route_id direct mee
# (gecontroleerd), en voor deze vergelijking is alleen het uitvalpercentage
# nodig, geen haltezoeker/dienstregeling-detail. Dat scheelt de twee duurste
# bestanden (utrecht_trips.json ~4 MB, utrecht_stop_times.json ~117 MB) voor
# deze uitbreiding volledig.
COMPARISON_AGENCIES = {"GVB": "Amsterdam", "RET": "Rotterdam", "HTM": "Den Haag"}
# Tram/metro/bus -- geen ferry (route_type 4, GVB's gratis IJ-veren): geen
# vergelijkbare modaliteit met U-OV, zou de vergelijking scheeftrekken.
COMPARISON_ROUTE_TYPES = {"0", "1", "3"}
OUT_COMPARISON_ROUTES = DATA_DIR / "regio_vergelijking_routes.json"


def find_comparison_routes(zf):
    """Filtert routes.txt op GVB/RET/HTM (tram/metro/bus), voor de
    uitval-vergelijking op /uitval. Losstaand bestand van utrecht_routes.json
    (andere scope/doel), maar dezelfde compacte vorm."""
    routes = {}
    with zf.open("routes.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            city = COMPARISON_AGENCIES.get(row.get("agency_id"))
            if city and row.get("route_type") in COMPARISON_ROUTE_TYPES:
                routes[row["route_id"]] = {
                    "agency_id": row["agency_id"],
                    "city": city,
                    "short_name": row.get("route_short_name", ""),
                    "route_type": row.get("route_type", ""),
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
                shape_id = row.get("shape_id")
                if shape_id:
                    key = (row["route_id"], row.get("direction_id", ""))
                    counts = shape_counts.setdefault(key, {})
                    counts[shape_id] = counts.get(shape_id, 0) + 1
    return trip_to_route, trip_meta, shape_counts


def find_dominant_shapes(shape_counts):
    """Kiest per (lijn, richting) de shape_id die door de meeste trips wordt
    gebruikt -- dat is in de praktijk het 'normale' tracé; zeldzame
    omleidingsvarianten met een eigen shape_id (soms tientallen per lijn,
    bv. na dienstregelingswijzigingen) vallen zo weg. Geeft terug:
    route_id -> lijst met unieke shape_id's (meestal 1-2: heen en terug)."""
    route_shapes = {}
    for (route_id, _direction), counts in shape_counts.items():
        dominant_shape = max(counts.items(), key=lambda kv: kv[1])[0]
        route_shapes.setdefault(route_id, [])
        if dominant_shape not in route_shapes[route_id]:
            route_shapes[route_id].append(dominant_shape)
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
    gauw te veel geheugen/CPU-tijd."""
    stop_times_by_stop = {}
    with zf.open("stop_times.txt") as f:
        text = io.TextIOWrapper(f, encoding="utf-8-sig")
        reader = csv.reader(text)
        header = next(reader)
        idx = {name: i for i, name in enumerate(header)}
        i_trip, i_stop, i_seq = idx["trip_id"], idx["stop_id"], idx["stop_sequence"]
        i_arr, i_dep = idx["arrival_time"], idx["departure_time"]
        for i, row in enumerate(reader):
            trip_id = row[i_trip]
            if trip_id in trip_ids:
                try:
                    stop_sequence = int(row[i_seq])
                except (ValueError, IndexError):
                    stop_sequence = 0
                time_str = row[i_dep] or row[i_arr]
                stop_times_by_stop.setdefault(row[i_stop], []).append((trip_id, stop_sequence, time_str))
            if i % 2_000_000 == 0 and i:
                log(f"  ...{i:,} stop_times regels verwerkt, {len(stop_times_by_stop):,} haltes gevonden")
    return set(stop_times_by_stop), stop_times_by_stop


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
                stops[row["stop_id"]] = {"name": row.get("stop_name", ""), "lat": lat, "lon": lon}
    return stops


def main():
    DATA_DIR.mkdir(exist_ok=True)
    download_gtfs_zip()

    with zipfile.ZipFile(GTFS_ZIP_PATH) as zf:
        log(f"Filter routes.txt op agency_id={TARGET_AGENCY_ID!r}, bus (route_type=3) + U-tram (route_type=0)...")
        routes = find_uov_routes(zf)
        agency_name = load_agency_name(zf)

        log("Filter routes.txt op GVB/RET/HTM (regio-vergelijking uitvalpercentage)...")
        comparison_routes = find_comparison_routes(zf)
        log(f"{len(comparison_routes)} vergelijkingslijnen gevonden ({', '.join(sorted(set(COMPARISON_AGENCIES.values())))}).")
        for r in routes.values():
            r["agency_name"] = agency_name
        log(f"{len(routes)} U-OV lijnen gevonden (bus + tram).")

        trip_to_route, trip_meta, shape_counts = find_trips_for_routes(zf, set(routes))
        log(f"{len(trip_to_route):,} trips gevonden voor deze lijnen.")

        service_ids = {m["service_id"] for m in trip_meta.values() if m["service_id"]}
        log(f"Parse calendar.txt/calendar_dates.txt voor {len(service_ids)} service_ids...")
        calendar = find_calendar_for_services(zf, service_ids)

        log("Bepaal haltes en halte-tijden die deze trips aandoen (doorzoekt een groot bestand, even geduld)...")
        stop_ids, stop_times_by_stop = find_stop_times_for_trips(zf, set(trip_to_route))
        stop_info = load_stop_info(zf, stop_ids)
        log(f"{len(stop_info):,} haltes gevonden, {sum(len(v) for v in stop_times_by_stop.values()):,} halte-tijden.")

        route_shapes = find_dominant_shapes(shape_counts)
        needed_shape_ids = {sid for shapes in route_shapes.values() for sid in shapes}
        log(f"Bepaal {len(needed_shape_ids)} dominante routetraject(en) (van {sum(len(c) for c in shape_counts.values())} totaal) en vereenvoudig ze...")
        simplified_shapes = load_and_simplify_shapes(zf, needed_shape_ids)
        route_shape_points = {
            route_id: [simplified_shapes[sid] for sid in shape_ids if sid in simplified_shapes]
            for route_id, shape_ids in route_shapes.items()
        }
        total_points = sum(len(s) for shapes in route_shape_points.values() for s in shapes)
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
    OUT_COMPARISON_ROUTES.write_text(json.dumps(comparison_routes, ensure_ascii=False), encoding="utf-8")
    log(
        f"Weggeschreven: {OUT_STOPS.name}, {OUT_ROUTES.name}, {OUT_TRIPS.name}, "
        f"{OUT_CALENDAR.name}, {OUT_TRIP_META.name}, {OUT_STOP_TIMES.name}, {OUT_SHAPES.name}, "
        f"{OUT_COMPARISON_ROUTES.name}"
    )


if __name__ == "__main__":
    main()
