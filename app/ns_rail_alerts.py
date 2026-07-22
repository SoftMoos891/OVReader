"""Storingen op het spoor (NS) binnen de provincie Utrecht.

Aparte databron t.o.v. het GTFS-RT-alerts-endpoint van OVapi/NDOV (zie
gtfs_rt.fetch_alerts): NS levert geen data via NDOV, maar heeft een eigen
open-data-API (NS Reisinformatie API, apiportal.ns.nl). Vereist een eigen
abonnementssleutel (env var NS_API_KEY) -- zonder key wordt deze databron
in collector.py overgeslagen, de rest van de app blijft gewoon werken.

"Binnen de provincie Utrecht" wordt bepaald met een echte
point-in-polygon-test tegen data/provincies.geojson (CBS-provinciegrenzen,
al in de repo) toegepast op de station-coordinaten die de NS-respons zelf
al meelevert -- geen handmatig samengestelde/te onderhouden lijst van
stationscodes nodig, en geen extra call naar de NS-stations-endpoint.
"""
import json
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NS_DISRUPTIONS_URL = "https://gateway.apiportal.ns.nl/reisinformatie-api/api/v3/disruptions"
REQUEST_TIMEOUT = 15

_TYPE_LABELS = {
    "DISRUPTION": "Verstoring",
    "MAINTENANCE": "Werkzaamheden",
    "CALAMITY": "Calamiteit",
}


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


def _point_in_ring(lng, lat, ring):
    """Ray-casting point-in-polygon (even-odd rule) -- voorkomt een externe
    geo-dependency (bv. shapely) voor deze ene test op een handjevol punten."""
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


def _collect_stations(obj, out):
    """Loopt recursief door de (geneste, deels wisselende) disruption-JSON
    en verzamelt elk station-object dat NS meelevert (herkenbaar aan
    stationCode+coordinate), ongeacht op welk pad het precies genest zit --
    robuuster dan een vaste sleutelpad-aanname bij toekomstige API-wijzigingen."""
    if isinstance(obj, dict):
        if "stationCode" in obj and "coordinate" in obj:
            out[obj["stationCode"]] = obj
        for v in obj.values():
            _collect_stations(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_stations(item, out)


def fetch_rail_disruptions(api_key):
    resp = requests.get(
        NS_DISRUPTIONS_URL,
        params={"isActive": "true"},
        headers={"Ocp-Apim-Subscription-Key": api_key},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def parse_rail_alerts(disruptions):
    """Filtert de landelijke NS-storingenlijst tot alleen storingen die
    minstens één station in de provincie Utrecht raken."""
    results = []
    for d in disruptions:
        stations = {}
        _collect_stations(d, stations)
        utrecht_stations = sorted({
            s["name"] for s in stations.values()
            if s.get("countryCode") == "NL" and _in_utrecht(s["coordinate"]["lng"], s["coordinate"]["lat"])
        })
        if not utrecht_stations:
            continue
        timespans = d.get("timespans") or []
        situation = timespans[0].get("situation") if timespans else None
        results.append({
            "alert_id": d["id"],
            "disruption_type": d.get("type", "DISRUPTION"),
            "type_label": _TYPE_LABELS.get(d.get("type"), "Melding"),
            "title": d.get("title", ""),
            "description": situation["label"] if situation else "",
            "start_time": d.get("start"),
            "end_time": d.get("end"),
            "impact": (d.get("impact") or {}).get("value"),
            "stations": utrecht_stations,
        })
    return results


def fetch_utrecht_rail_alerts(api_key):
    return parse_rail_alerts(fetch_rail_disruptions(api_key))
