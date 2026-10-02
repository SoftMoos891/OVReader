"""Weer, KNMI-waarschuwingen en luchtkwaliteit via de DVZ RSS-bridge.

Sinds 2 okt 2026 haalt de bridge van de DVZ Reader (dvz-rss-server.js, poort
3001, module knmi.js) het KNMI en RIVM Luchtmeetnet op, en deze collector vraagt
het daar op in plaats van zelf. Zo gebruikt nog maar één partij de KNMI-sleutel
(de limiet werd gedeeld; op 3 aug 2026 knapte die af na een reeks herstarts).
knmi_warnings.py, knmi_weather.py en luchtkwaliteit.py hielden de oude eigen
ophaalcode; de collector gebruikt daarvan alleen nog format_active_from.

Het endpoint /v1/knmi-ruw is alleen intern (de bridge weigert alles wat via
nginx binnenkomt) en geeft de velden in dezelfde vorm als de oude functies
hieronder teruggaven, plus per deel de tijd waarop de bridge het binnenkreeg
(ms). Is een deel ouder dan MAX_LEEFTIJD, dan geldt dat als fout: de job slaat
de ronde over en de bestaande "verouderd"-signalering (knmi_fetch_status e.d.)
doet zijn werk, in plaats van dat oude data als vers wordt opgeslagen.
"""
import os
import time

import requests

BRIDGE_URL = os.environ.get("DVZ_BRIDGE_WEER_URL", "http://127.0.0.1:3001/v1/knmi-ruw")
REQUEST_TIMEOUT = 10
# De bridge haalt het KNMI elke 10 minuten op, Luchtmeetnet elke 30.
MAX_LEEFTIJD_KNMI_S = 30 * 60
MAX_LEEFTIJD_LUCHT_S = 90 * 60


def _deel(naam, tijd_veld, max_leeftijd_s):
    resp = requests.get(BRIDGE_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    waarde, tijd = data.get(naam), data.get(tijd_veld)
    if waarde is None or not tijd:
        fout = (data.get("fouten") or {}).get(naam)
        raise RuntimeError(f"bridge heeft nog geen {naam}" + (f" ({fout})" if fout else ""))
    leeftijd = time.time() - tijd / 1000
    if leeftijd > max_leeftijd_s:
        raise RuntimeError(f"{naam} in de bridge is {int(leeftijd // 60)} minuten oud")
    return waarde


def fetch_utrecht_warnings():
    """Zelfde lijst als knmi_warnings.fetch_utrecht_warnings gaf."""
    return _deel("waarschuwingen", "waarschTijd", MAX_LEEFTIJD_KNMI_S)


def fetch_de_bilt_weather():
    """Zelfde dict als knmi_weather.fetch_de_bilt_weather gaf."""
    return _deel("weer", "weerTijd", MAX_LEEFTIJD_KNMI_S)


def fetch_air_quality():
    """Zelfde dict als luchtkwaliteit.fetch_air_quality gaf (met concentrations)."""
    return _deel("lucht", "luchtTijd", MAX_LEEFTIJD_LUCHT_S)
