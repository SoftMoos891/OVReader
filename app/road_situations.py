"""Actuele wegsituaties (Rijkswaterstaat-hoofdwegennet) binnen de provincie
Utrecht -- wegwerkzaamheden, ongevallen, obstakels, omleidingen.

Publieke, sleutelloze open-databron: NDW (Nationaal Dataportaal
Wegverkeer, opendata.ndw.nu), DATEX II-formaat. Dit is vooral het
Rijkswaterstaat-hoofdwegennet (snelwegen als A2/A12/A27/A28 en een deel van
de provinciale wegen) -- geen fijnmazige data over binnenstadsstraten, maar
wel bruikbare context bij bus-vertragingen (bv. een afsluiting op de A12
bij Utrecht).

"Binnen de provincie Utrecht" wordt net als bij ns_rail_alerts.py bepaald
met een echte point-in-polygon-test tegen data/provincies.geojson, hier
toegepast op de coordinaten die elke wegsituatie zelf al meelevert (punt of
lijnstuk) -- geen handmatig samengestelde lijst met wegvakken nodig."""
import gzip
import json
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

# Deze typen tellen als "ernstig" (voor de RSS-feed) -- de rest is vooral
# routine wegwerkzaamheden/snelheidsmaatregelen, geen incident. VehicleObstruction
# leek eerst een logische toevoeging, maar bleek in de praktijk (provincie
# Utrecht, steekproef) voor 78% gewoon een "Pijlwagen" (mobiele
# wegwerk-begeleiding) te zijn -- geen incident, dus bewust NIET meegenomen.
# Zelfde les als bij de ADDITIONAL_SERVICE-blanket-regel voor bus-meldingen.
SEVERE_ROAD_TYPES = {"Accident", "AbnormalTraffic"}


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
    for situation in root.findall(".//sit:situation", _NS):
        records = situation.findall("sit:situationRecord", _NS)
        if not records:
            continue

        severity_el = situation.find("sit:overallSeverity", _NS)
        severity = severity_el.text if severity_el is not None else None

        all_points = []
        record_types = []
        comment = None
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
        results.append({
            "situation_id": situation.get("id"),
            "record_type": record_type,
            "type_label": _TYPE_LABELS.get(record_type, "Verkeersmelding"),
            "comment": comment,
            "severity": severity,
            "start_time": start_time,
            "end_time": end_time,
            "lon": utrecht_point[0],
            "lat": utrecht_point[1],
            "road_number": road_number,
            "road_location": road_location,
        })
    return results


def fetch_utrecht_road_situations():
    return parse_road_situations(fetch_road_situations_feed())
