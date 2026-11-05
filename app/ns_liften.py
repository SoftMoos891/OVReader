"""Liften op NS-stations binnen de provincie Utrecht.

NS zet de status van elke lift (Beschikbaar / Buiten werking / Status
onbekend) in de Places-API, niet in de storingen van de Reisinformatie-API.
Er staat geen oorzaak of verwachte hersteltijd bij; de duur van een storing
houden we dus zelf bij door statuswisselingen op te slaan.

Provincie Utrecht: dezelfde point-in-polygon-test als ns_rail_alerts.py, op
de coördinaten van de lift zelf.
"""
import time

import requests

from .ns_rail_alerts import _in_utrecht

PLACES_URL = "https://gateway.apiportal.ns.nl/places-api/v2/places"
STATIONS_URL = "https://gateway.apiportal.ns.nl/reisinformatie-api/api/v2/stations"
REQUEST_TIMEOUT = 20

STATUS_LABELS = {"Yes": "beschikbaar", "No": "buiten_werking", "Unknown": "onbekend"}

_station_namen = {}
_station_namen_tijd = 0


def _stationsnamen(api_key):
    """Stationscode -> naam; één keer per dag vernieuwd (verandert zelden)."""
    global _station_namen, _station_namen_tijd
    if _station_namen and time.time() - _station_namen_tijd < 86400:
        return _station_namen
    try:
        resp = requests.get(
            STATIONS_URL,
            headers={"Ocp-Apim-Subscription-Key": api_key},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        _station_namen = {s["code"]: s["namen"]["lang"] for s in resp.json().get("payload", [])}
        _station_namen_tijd = time.time()
    except (requests.RequestException, ValueError, KeyError):
        # Zonder namen tonen we de code; volgende ronde opnieuw proberen.
        pass
    return _station_namen


def fetch_utrecht_liften(api_key):
    resp = requests.get(
        PLACES_URL,
        params={"type": "stationfacility", "identifier": "lift", "lang": "nl", "limit": 2000},
        headers={"Ocp-Apim-Subscription-Key": api_key},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json().get("payload", [])
    namen = _stationsnamen(api_key)
    liften = []
    for place in payload:
        if "lift" not in (place.get("identifiers") or []):
            continue
        for loc in place.get("locations", []):
            lift_id = (loc.get("nearbyMeLocationId") or {}).get("value")
            lat, lng = loc.get("lat"), loc.get("lng")
            if not lift_id or lat is None or lng is None or not _in_utrecht(lng, lat):
                continue
            code = loc.get("stationCode") or ""
            liften.append({
                "lift_id": lift_id,
                "station_code": code,
                "station_naam": namen.get(code, code),
                "naam": (loc.get("description") or loc.get("name") or "Lift").strip(),
                "lat": lat,
                "lng": lng,
                "status": STATUS_LABELS.get(loc.get("open"), "onbekend"),
            })
    if not liften:
        # Een lege lijst zou alle liften als 'verdwenen' markeren; liever overslaan.
        raise ValueError("NS Places-API gaf geen liften in de provincie Utrecht terug")
    return liften
