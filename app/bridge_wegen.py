"""Wegsituaties via de DVZ RSS-bridge.

Sinds 2 okt 2026 maakt de bridge van de DVZ Reader (wegsituaties.js) de
wegsituaties: de DVZ OV Reader doet alleen nog OV (Danny: scherpe taakverdeling).
De bridge downloadt het actuele beeld van NDW toch al elke 2 minuten en parset
het met een letterlijke kopie van road_situations.py (nagemeten: zelfde
situaties, zelfde velden). Deze collector vraagt de volledige lijst daar op
in plaats van NDW zelf te downloaden; de rest (tabel road_situations, de
RSS-meldingen, de pagina's) blijft zoals het was.

/v1/wegsituaties-ruw is alleen intern (de bridge weigert alles wat via nginx
binnenkomt). Is het beeld in de bridge ouder dan MAX_LEEFTIJD_S, dan geldt dat
als fout: de job slaat de ronde over en road_fetch_status laat zien dat er iets
hapert, in plaats van dat een oude lijst situaties als vers wordt opgeslagen
(en dan als "voorbij" zou tellen wat er nog is).
"""
import os
import time

import requests

BRIDGE_URL = os.environ.get("DVZ_BRIDGE_WEGEN_URL", "http://127.0.0.1:3001/v1/wegsituaties-ruw")
REQUEST_TIMEOUT = 10
MAX_LEEFTIJD_S = 15 * 60  # de bridge ververst elke 2 minuten


def fetch_utrecht_road_situations():
    """Zelfde lijst als road_situations.fetch_utrecht_road_situations gaf."""
    resp = requests.get(BRIDGE_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    situaties, tijd = data.get("situaties"), data.get("tijd")
    if situaties is None or not tijd:
        raise RuntimeError("bridge heeft nog geen wegsituaties")
    leeftijd = time.time() - tijd / 1000
    if leeftijd > MAX_LEEFTIJD_S:
        raise RuntimeError(f"wegsituaties in de bridge zijn {int(leeftijd // 60)} minuten oud")
    return situaties
