"""Actuele wegsituaties binnen de provincie Utrecht -- wegwerkzaamheden,
ongevallen, obstakels, omleidingen.

Publieke, sleutelloze open-databron: NDW (Nationaal Dataportaal
Wegverkeer, opendata.ndw.nu), DATEX II-formaat. Dit is breder dan alleen
het Rijkswaterstaat-hoofdwegennet: naast RWS-snelwegen (A2/A12/A27/A28,
bron-ID's als RWS01/RWS10/NLRWS) leveren ook provincies en gemeenten mee
via NDW (bron-ID's als NDW06/NDW08/MOS01/TSN01) -- in de praktijk komen
hierdoor ook provinciale wegen (bv. N421) en lokale Utrechtse straten
(bv. een "U"-wegnummer in Leidsche Rijn) voorbij, niet alleen snelwegen.

"Binnen de provincie Utrecht" wordt net als bij ns_rail_alerts.py bepaald
met een echte point-in-polygon-test tegen data/provincies.geojson, hier
toegepast op de coordinaten die elke wegsituatie zelf al meelevert (punt of
lijnstuk) -- geen handmatig samengestelde lijst met wegvakken nodig."""
import gzip
import json
import math
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NDW_ACTUEEL_BEELD_URL = "https://opendata.ndw.nu/actueel_beeld.xml.gz"
REQUEST_TIMEOUT = 30

_NS = {
    "sit": "http://datex2.eu/schema/3/situation",
    "mc": "http://datex2.eu/schema/3/messageContainer",
    "com": "http://datex2.eu/schema/3/common",
    "loc": "http://datex2.eu/schema/3/locationReferencing",
}
_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"

# Dutch labels + prioriteit: als een situatie meerdere situationRecords heeft
# (in de praktijk gemiddeld ~2 per situatie), telt de meest urgente/relevante
# als het "type" van de hele situatie.
_TYPE_LABELS = {
    "Accident": "Ongeval",
    "AbnormalTraffic": "Abnormale verkeersdrukte",
    "VehicleObstruction": "Voertuig op de weg",
    "GeneralObstruction": "Obstakel op de weg",
    "ReroutingManagement": "Omleiding",
    "RoadOrCarriagewayOrLaneManagement": "Wegwerkzaamheden",
    "SpeedManagement": "Snelheidsmaatregel",
    "GeneralNetworkManagement": "Verkeersmaatregel",
}
_TYPE_PRIORITY = list(_TYPE_LABELS)

# Deze typen tellen als "ernstig" voor de meldingenlijst/kaart op de site --
# de rest is vooral routine wegwerkzaamheden/snelheidsmaatregelen, geen
# incident. VehicleObstruction leek eerst een logische toevoeging, maar
# bleek in de praktijk (provincie Utrecht, steekproef) voor 78% gewoon een
# "Pijlwagen" (mobiele wegwerk-begeleiding) te zijn -- geen incident, dus
# bewust NIET meegenomen. Zelfde les als bij de ADDITIONAL_SERVICE-
# blanket-regel voor bus-meldingen. GeneralObstruction (bv. lading/object op
# de weg) is wel een echt incident.
SEVERE_ROAD_TYPES = {"Accident", "AbnormalTraffic", "GeneralObstruction"}

# Smallere subset voor de RSS-feed: GeneralObstruction mag wel op de site
# getoond worden (zie hierboven), maar hoeft geen RSS-melding te geven -- op
# verzoek, waarschijnlijk omdat een obstakel op de weg minder vaak een echt
# meldingswaardig incident is dan een ongeval/abnormale verkeersdrukte.
RSS_ROAD_TYPES = {"Accident", "AbnormalTraffic"}

# DATEX II overallSeverity-waarde die overeenkomt met "Gering" in de UI (zie
# ROAD_SEVERITY_LABELS in templates/index.html en lite.html) -- op verzoek
# nooit urgent en nooit een RSS-melding, ongeacht het record_type.
NEGLIGIBLE_SEVERITY = "lowest"

# Demonstraties komen in de NDW-feed vrijwel altijd binnen met een op zich
# onschuldig record_type (in de praktijk RoadOrCarriagewayOrLaneManagement,
# "Wegwerkzaamheden") terwijl een demonstratie wel degelijk tot afsluitingen/
# omleidingen kan leiden -- op verzoek daarom altijd urgent, los van
# record_type, via het cause-veld (zie ovreader_road_situations_cause_gap
# memory: dit veld werd al geparsed/getoond, maar telde nog niet mee in de
# urgentie-logica).
DEMONSTRATION_CAUSE = "demonstratie"


def is_demonstration(cause):
    return bool(cause) and DEMONSTRATION_CAUSE in cause.lower()


# Oorzaak van de RWS-verkeerscentrale (1 okt 2026). Een ongeval dat NDW
# automatisch detecteert (bron NDW06, "managedByAutomation") heeft zelf geen
# oorzaak; de verkeerscentrale zet er vaak een eigen situatie naast (bron
# NLRWS, bv. een rijstrookafsluiting) met causeDescription "Defecte
# vrachtwagen", "Ongeval(len)", "Veiligheidsmaatregelen" ... Voorbeeld A12 bij
# Nieuwerbrug: NDW06-ongeval en NLRWS-record "Defecte vrachtwagen" 114 m uit
# elkaar, zelfde rijrichting, 8 minuten na elkaar. Zo'n oorzaak lenen we:
# binnen CAUSE_RADIUS_M, rijrichting (als beide bekend) binnen
# CAUSE_BEARING_MAX_DIFF graden, begin binnen CAUSE_MAX_TIME_DIFF_S. Werk
# ("Wegwerkzaamheden") lenen we niet uit: een ongeluk in een werkvak is nog
# steeds een ongeluk. Een file leent ook niets uit (zie parse_road_situations).
CAUSE_RADIUS_M = 300
CAUSE_BEARING_MAX_DIFF = 60
CAUSE_MAX_TIME_DIFF_S = 2 * 3600
_WORK_CAUSE_WORDS = ("werkzaamheden",)

# Bij deze typen gaat de oorzaak voor op het generieke type-label (op verzoek:
# "laat de RWS-oorzaak voorgaan"). Het automatische "Ongeval" wordt dan
# bijvoorbeeld "Defecte vrachtwagen", een rijstrookafsluiting ("Wegwerkzaamheden")
# door een demonstratie "Demonstratie". Bij een file (AbnormalTraffic) blijft
# het label staan; de oorzaak komt er dan als los veld bij. record_type, en
# daarmee de urgentie en de RSS-keuze, verandert nergens.
_LABEL_FROM_CAUSE_TYPES = {"Accident", "GeneralObstruction", "VehicleObstruction", "RoadOrCarriagewayOrLaneManagement"}


def clean_cause(text):
    """RWS-schrijfwijze naar een label: "Ongeval(len)" -> "Ongeval"."""
    if not text:
        return None
    text = text.replace("(len)", "").replace("(en)", "").strip()
    return text[:1].upper() + text[1:] if text else None


def _is_work_cause(cause):
    return bool(cause) and any(w in cause.lower() for w in _WORK_CAUSE_WORDS)


def _distance_m(a, b):
    """Afstand in meters tussen twee (lng, lat)-punten (equirectangulair, ruim
    nauwkeurig genoeg op een paar honderd meter)."""
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((a[0] - b[0]) * kx, (a[1] - b[1]) * 110570)


def _point_segment_m(p, a, b):
    kx = 111320 * math.cos(math.radians(p[1]))
    ax, ay = (a[0] - p[0]) * kx, (a[1] - p[1]) * 110570
    bx, by = (b[0] - p[0]) * kx, (b[1] - p[1]) * 110570
    vx, vy = bx - ax, by - ay
    lengte2 = vx * vx + vy * vy
    t = 0 if lengte2 == 0 else max(0.0, min(1.0, -(ax * vx + ay * vy) / lengte2))
    return math.hypot(ax + t * vx, ay + t * vy)


def _geometry_distance_m(lines_a, lines_b):
    """Kleinste afstand tussen twee geometrieën, elk een lijst lijnen (een punt
    is een lijn van één punt): elk hoekpunt tegen elk lijnstuk van de ander."""
    best = math.inf
    for la, lb in ((lines_a, lines_b), (lines_b, lines_a)):
        for line in la:
            for p in line:
                for other in lb:
                    if len(other) == 1:
                        best = min(best, _distance_m(p, other[0]))
                    for i in range(len(other) - 1):
                        best = min(best, _point_segment_m(p, other[i], other[i + 1]))
    return best


def _bearing_between(a, b):
    """Kompaskoers van punt a naar b (graden, 0 = noord)."""
    dx = (b[0] - a[0]) * math.cos(math.radians((a[1] + b[1]) / 2))
    dy = b[1] - a[1]
    return (math.degrees(math.atan2(dx, dy)) + 360) % 360


def _bearing_diff(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def _parse_time(text):
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() if text else None
    except ValueError:
        return None


def _load_utrecht_rings():
    geojson = json.loads((DATA_DIR / "provincies.geojson").read_text(encoding="utf-8"))
    feature = next(f for f in geojson["features"] if f["properties"]["statnaam"] == "Utrecht")
    geom = feature["geometry"]
    if geom["type"] == "Polygon":
        return [geom["coordinates"][0]]
    if geom["type"] == "MultiPolygon":
        return [polygon[0] for polygon in geom["coordinates"]]
    raise ValueError(f"Onverwacht geometry-type voor provincie Utrecht: {geom['type']}")


_UTRECHT_RINGS = _load_utrecht_rings()

# AlertC-locatiecodes -> [wegnummer, naam1, naam2], gegenereerd door
# app/build_vild_index.py. Ontbreekt dat bestand, dan blijft alles werken --
# de meldingen tonen dan alleen geen wegnummer.
_VILD_PATH = DATA_DIR / "vild_locations.json"
_VILD_LOCATIONS = (
    json.loads(_VILD_PATH.read_text(encoding="utf-8")) if _VILD_PATH.exists() else {}
)
# Zie build_vild_index.VILD_TABLE_VERSION: verwijst de feed naar een andere
# tabelversie, dan kunnen codes verschoven zijn en is een nieuwe build nodig.
_EXPECTED_TABLE_NUMBER, _EXPECTED_TABLE_VERSION = "6.13", "A"
_table_version_warned = False


def _warn_on_table_version(root):
    """Eenmalige waarschuwing in het collector-log zodra NDW naar een andere
    VILD-versie overstapt dan waarmee vild_locations.json is gebouwd."""
    global _table_version_warned
    if _table_version_warned or not _VILD_LOCATIONS:
        return
    number = root.find(".//loc:alertCLocationTableNumber", _NS)
    version = root.find(".//loc:alertCLocationTableVersion", _NS)
    if number is None or version is None:
        return
    if number.text != _EXPECTED_TABLE_NUMBER or version.text != _EXPECTED_TABLE_VERSION:
        _table_version_warned = True
        print(
            f"[road_situations] LET OP: feed gebruikt VILD-tabel "
            f"{number.text}.{version.text}, maar data/vild_locations.json is gebouwd "
            f"voor {_EXPECTED_TABLE_NUMBER}.{_EXPECTED_TABLE_VERSION}. "
            f"Draai 'python -m app.build_vild_index' opnieuw (en pas de versie daarin aan).",
            flush=True,
        )


def _road_label(record_els):
    """Wegnummer + leesbare plaatsaanduiding bij een situatie, opgezocht via
    de AlertC-code(s) in de locatiereferentie. Geeft (None, None) als de
    situatie geen code heeft of die niet in de tabel staat."""
    for rec in record_els:
        for code_el in rec.findall(".//loc:specificLocation", _NS):
            entry = _VILD_LOCATIONS.get((code_el.text or "").strip())
            if not entry:
                continue
            road = entry[0] or None
            names = [n for n in entry[1:] if n]
            return road, " - ".join(names) or None
    return None, None


def _point_in_ring(lng, lat, ring):
    """Ray-casting point-in-polygon (even-odd rule) -- voorkomt een externe
    geo-dependency (bv. shapely) voor deze test."""
    inside = False
    n = len(ring)
    x1, y1 = ring[0]
    for i in range(1, n + 1):
        x2, y2 = ring[i % n]
        if (y1 > lat) != (y2 > lat) and lng < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
        x1, y1 = x2, y2
    return inside


def _in_utrecht(lng, lat):
    return any(_point_in_ring(lng, lat, ring) for ring in _UTRECHT_RINGS)


def fetch_road_situations_feed():
    """Haalt actueel_beeld.xml.gz op en ontpakt 'm -- dit is een letterlijk
    gzip-bestand als download (geen HTTP Content-Encoding), dus handmatig
    decomprimeren i.p.v. leunen op requests' automatische gzip-afhandeling."""
    resp = requests.get(NDW_ACTUEEL_BEELD_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    xml_bytes = gzip.decompress(resp.content)
    return ET.fromstring(xml_bytes)


def _extract_lines(record_el):
    """Zelfde punten als _extract_points, maar per posList een eigen lijn (en
    elk los punt een lijn van één punt), voor de afstand tussen situaties."""
    lines = []
    for pt in record_el.findall(".//loc:pointCoordinates", _NS):
        lat_el = pt.find("loc:latitude", _NS)
        lon_el = pt.find("loc:longitude", _NS)
        if lat_el is not None and lon_el is not None and lat_el.text and lon_el.text:
            lines.append([(float(lon_el.text), float(lat_el.text))])
    for poslist in record_el.findall(".//loc:posList", _NS):
        if not poslist.text:
            continue
        coords = poslist.text.split()
        line = [(float(coords[i + 1]), float(coords[i])) for i in range(0, len(coords) - 1, 2)]
        if line:
            lines.append(line)
    return lines


def _record_bearing(record_el, lines):
    """loc:bearing als die er is, anders de koers van begin naar eind van de
    langste lijn (een file of afsluiting is over een paar km vrijwel recht)."""
    b = record_el.find(".//loc:bearing", _NS)
    if b is not None and b.text:
        try:
            return float(b.text)
        except ValueError:
            pass
    longest = max(lines, key=len, default=[])
    if len(longest) >= 2 and _distance_m(longest[0], longest[-1]) > 50:
        return _bearing_between(longest[0], longest[-1])
    return None


def _extract_points(record_el):
    """Alle lat/lon-punten uit een situationRecord's locatiereferentie
    (puntlocatie of lijnstuk), voor de Utrecht-relevantiecheck."""
    points = []
    for pt in record_el.findall(".//loc:pointCoordinates", _NS):
        lat_el = pt.find("loc:latitude", _NS)
        lon_el = pt.find("loc:longitude", _NS)
        if lat_el is not None and lon_el is not None and lat_el.text and lon_el.text:
            points.append((float(lon_el.text), float(lat_el.text)))
    for poslist in record_el.findall(".//loc:posList", _NS):
        if not poslist.text:
            continue
        coords = poslist.text.split()
        for i in range(0, len(coords) - 1, 2):  # posList: "lat lon lat lon ..."
            points.append((float(coords[i + 1]), float(coords[i])))
    return points


def parse_road_situations(root):
    """Filtert de landelijke NDW-wegsituaties tot alleen situaties die
    minstens één punt binnen de provincie Utrecht hebben."""
    _warn_on_table_version(root)
    results = []
    # Alle situaties in heel Nederland met een oorzaak: een melding net over
    # de provinciegrens kan een Utrechtse melding verklaren.
    donors = []
    geometries = {}
    for situation in root.findall(".//sit:situation", _NS):
        records = situation.findall("sit:situationRecord", _NS)
        if not records:
            continue

        severity_el = situation.find("sit:overallSeverity", _NS)
        severity = severity_el.text if severity_el is not None else None

        all_points = []
        record_types = []
        comment = None
        cause = None
        start_time = end_time = None
        for rec in records:
            rtype = rec.get(_XSI_TYPE, "")
            if rtype.startswith("sit:"):
                rtype = rtype[len("sit:"):]
            record_types.append(rtype)
            all_points.extend(_extract_points(rec))
            if comment is None:
                c = rec.find(".//sit:generalPublicComment/sit:comment/com:values/com:value", _NS)
                if c is not None and c.text:
                    comment = c.text
            # bv. "Demonstratie"/"Wegwerkzaamheden"/"Door grenscontrole" --
            # los van generalPublicComment, dat vaak leeg is terwijl dit
            # veld wel gevuld is (zie ovreader_road_situations_cause_gap
            # memory: dit veld werd voorheen helemaal niet geparsed).
            ca = rec.find(".//sit:cause/sit:causeDescription/com:values/com:value", _NS)
            if ca is not None and ca.text:
                if cause is None:
                    cause = ca.text
                # Een file (AbnormalTraffic) leent niets uit: zijn lijn is de hele
                # staart, kilometers terug, en een voertuig aan het eind daarvan
                # (vaak een pijlwagen) zou dan "Defecte vrachtwagen" heten.
                lines = _extract_lines(rec) if rtype != "AbnormalTraffic" else []
                if lines:
                    st = rec.find(".//com:overallStartTime", _NS)
                    donors.append({
                        "situation_id": situation.get("id"), "cause": clean_cause(ca.text), "lines": lines,
                        "bearing": _record_bearing(rec, lines), "start": _parse_time(st.text if st is not None else None),
                    })
            if start_time is None:
                s = rec.find(".//com:overallStartTime", _NS)
                if s is not None and s.text:
                    start_time = s.text
            if end_time is None:
                e = rec.find(".//com:overallEndTime", _NS)
                if e is not None and e.text:
                    end_time = e.text

        # De DATEX II-locatiereferentie in deze feed gebruikt AlertC-codes
        # (een losse, hier niet meegeleverde opzoektabel) i.p.v. leesbare
        # weg-/plaatsnamen -- vandaar geen "A12"-achtig veld. Coordinaten zijn
        # er wel, dus het EERSTE punt binnen Utrecht bewaren we als
        # representatieve locatie (voor een kaartlink), i.p.v. 'm na de
        # relevantie-check weg te gooien.
        utrecht_point = next(((lng, lat) for lng, lat in all_points if _in_utrecht(lng, lat)), None)
        if utrecht_point is None:
            continue

        record_type = next((t for t in _TYPE_PRIORITY if t in record_types), record_types[0])
        road_number, road_location = _road_label(records)
        main_rec = next(r for r, t in zip(records, record_types) if t == record_type)
        main_lines = _extract_lines(main_rec)
        geometries[situation.get("id")] = (main_lines, _record_bearing(main_rec, main_lines))
        results.append({
            "situation_id": situation.get("id"),
            "record_type": record_type,
            "type_label": _TYPE_LABELS.get(record_type, "Verkeersmelding"),
            "comment": comment,
            "cause": cause,
            "severity": severity,
            "start_time": start_time,
            "end_time": end_time,
            "lon": utrecht_point[0],
            "lat": utrecht_point[1],
            "road_number": road_number,
            "road_location": road_location,
        })

    for r in results:
        if r["cause"] is None:
            lines, bearing = geometries.get(r["situation_id"], ([], None))
            r["cause"] = _borrow_cause(r, lines, bearing, donors)
        else:
            r["cause"] = clean_cause(r["cause"])
        if r["cause"] and r["record_type"] in _LABEL_FROM_CAUSE_TYPES:
            r["type_label"] = r["cause"]
    return results


def _borrow_cause(result, lines, bearing, donors):
    """Oorzaak van de dichtstbijzijnde andere situatie die past (zie
    CAUSE_RADIUS_M e.v.), of None."""
    if not lines:
        return None
    start = _parse_time(result["start_time"])
    best = None
    for d in donors:
        if d["situation_id"] == result["situation_id"] or _is_work_cause(d["cause"]):
            continue
        if bearing is not None and d["bearing"] is not None and _bearing_diff(bearing, d["bearing"]) > CAUSE_BEARING_MAX_DIFF:
            continue
        if start is not None and d["start"] is not None and abs(start - d["start"]) > CAUSE_MAX_TIME_DIFF_S:
            continue
        afstand = _geometry_distance_m(lines, d["lines"])
        if afstand <= CAUSE_RADIUS_M and (best is None or afstand < best[0]):
            best = (afstand, d["cause"])
    return best[1] if best else None


def fetch_utrecht_road_situations():
    return parse_road_situations(fetch_road_situations_feed())
