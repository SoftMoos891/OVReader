"""Actuele luchtkwaliteit (Utrecht-Griftpark) via het RIVM Luchtmeetnet.

Bron: RIVM Luchtmeetnet Open API (api.luchtmeetnet.nl) -- publiek, sleutelloos,
geen registratie nodig. Levert de landelijke Luchtkwaliteitsindex (LKI,
schaal 1-11, RIVM berekent zelf al de worst-case van PM2.5/PM10/NO2/O3 tot
één getal) plus de losse concentraties per stof, per meetstation.

Station "NL10643" (Utrecht-Griftpark) is bewust gekozen boven de twee andere
Utrecht-stations (NL10636 Kardinaal de Jongweg, NL10639 Constant
Erzeijstraat): die twee zijn type "Traffic" (vlak naast een drukke weg,
bedoeld om piekbelasting te meten), Griftpark is type "Municipal" -- een
algemene stedelijke achtergrondlocatie, net als De Bilt voor het weer
(knmi_weather.py) een representatieve keuze voor "hoe is de lucht in
Utrecht" in plaats van een lokale verkeershotspot.

Data is ongevalideerd/near-realtime (RIVM corrigeert soms achteraf) -- prima
voor een dashboard, niet bedoeld als officiële rapportage."""
import requests

BASE_URL = "https://api.luchtmeetnet.nl/open_api"
STATION_NUMBER = "NL10643"  # Utrecht-Griftpark
REQUEST_TIMEOUT = 20

_COMPONENT_LABELS = {
    "NO2": "Stikstofdioxide", "O3": "Ozon", "PM10": "Fijnstof (PM10)",
    "PM25": "Fijnstof (PM2.5)", "NO": "Stikstofmonoxide", "NOx": "Stikstofoxiden",
}

# RIVM LKI-schaal: 1 (schoon) t/m 11 (zeer vervuild), bewust in vier
# kleurcategorieen (Atlas Leefomgeving/Luchtmeetnet-indeling).
_LKI_CATEGORIES = [
    (3, "Goed", "BLUE"),
    (6, "Matig", "YELLOW"),
    (8, "Onvoldoende", "ORANGE"),
    (10, "Slecht", "RED"),
    (11, "Zeer slecht", "PURPLE"),
]


def _lki_category(value):
    if value is None:
        return None, None
    for max_value, label, color in _LKI_CATEGORIES:
        if value <= max_value:
            return label, color
    return "Zeer slecht", "PURPLE"


def _latest_lki(station_number):
    resp = requests.get(
        f"{BASE_URL}/lki",
        params={"station_number": station_number, "order_by": "timestamp_measured",
                "order_direction": "desc", "page": 1},
        headers={"Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return data[0] if data else None


def _latest_concentrations(station_number):
    """Nieuwste meting per stof (formula) -- de measurements-feed levert
    aflopend gesorteerd meerdere uren historie in één paginabeurt, dus de
    eerste keer dat een formula voorkomt is de nieuwste waarde daarvan."""
    resp = requests.get(
        f"{BASE_URL}/stations/{station_number}/measurements",
        params={"order_by": "timestamp_measured", "order_direction": "desc", "page": 1},
        headers={"Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    latest = {}
    for m in data:
        formula = m["formula"]
        if formula not in latest:
            latest[formula] = {
                "value": round(m["value"], 1),
                "unit": "µg/m³",
                "label": _COMPONENT_LABELS.get(formula, formula),
                "measured_at": m["timestamp_measured"],
            }
    return latest


def fetch_air_quality(station_number=STATION_NUMBER):
    lki = _latest_lki(station_number)
    concentrations = _latest_concentrations(station_number)
    lki_value = lki["value"] if lki else None
    lki_label, lki_color = _lki_category(lki_value)
    return {
        "station": "Utrecht-Griftpark",
        "measured_at": lki["timestamp_measured"] if lki else None,
        "lki": lki_value,
        "lki_label": lki_label,
        "lki_color": lki_color,
        "concentrations": concentrations,
    }
