"""Weerwaarschuwingen (KNMI) voor de provincie Utrecht.

Bron: KNMI Open Data API, dataset "waarschuwingen_nederland_48h" versie 1.0
(dataplatform.knmi.nl). Vereist een eigen API-key (env var KNMI_API_KEY) --
zonder key wordt deze bron in collector.py overgeslagen, de rest van de app
blijft gewoon werken.

Elk bestand is een "cube": 48 uurlijkse timeslices x 7 fenomenen (wind,
windstoten, gladheid, onweer, mist, hitte, regen) x alle Nederlandse
provincies/gebieden, met per combinatie een kleurcode (GREEN/YELLOW/ORANGE/
RED). We filteren op locatie "UT" (provincie Utrecht) en op nog niet
gepasseerde timeslices, en nemen per fenomeen de ernstigste kleur die nog
ergens in de resterende ~48 uur voorkomt -- geen exacte geldigheidsvensters
per kleurwissel, dat zou de parser onnodig ingewikkeld maken t.o.v. wat de
site nodig heeft (dit fenomeen wordt ergens in de komende 48 uur geel/
oranje/rood).

Het bronbestand is vrij groot (~2-3 MB) en verandert maar een paar keer per
dag -- vandaar een rustig ophaalinterval (zie collector.py), in
tegenstelling tot de veel kleinere/frequentere NS- en NDW-feeds."""
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import requests

DATASET = "waarschuwingen_nederland_48h"
VERSION = "1.0"
BASE_URL = f"https://api.dataplatform.knmi.nl/open-data/v1/datasets/{DATASET}/versions/{VERSION}"
REQUEST_TIMEOUT = 60
LOCATION_ID = "UT"  # provincie Utrecht, zie report_location-definities in het bestand

_PHENOMENON_LABELS = {
    "FX": "Windstoten", "WS": "Wind", "RR": "Regen", "TX": "Hitte",
    "IC": "Gladheid", "VV": "Mist", "LT": "Onweer",
}
_COLOR_LABELS = {"YELLOW": "Geel", "ORANGE": "Oranje", "RED": "Rood"}
_COLOR_SEVERITY = {"GREEN": 0, "GRAY": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3, "BLACK": 4}


def _latest_xml_filename(api_key):
    """Bestandslijst is oplopend op naam/tijd tenzij je sorting=desc meegeeft
    -- zonder dat zou je de oudste (hier: uit 2023) bestanden te pakken
    krijgen i.p.v. de actuele."""
    resp = requests.get(
        f"{BASE_URL}/files",
        params={"maxKeys": 5, "orderBy": "created", "sorting": "desc"},
        headers={"Authorization": api_key},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    files = resp.json()["files"]
    # Naast elk .xml-bestand (de volledige per-provincie data) staat ook een
    # klein .txt-bestand (menselijk-leesbare landelijke samenvatting, bv.
    # "Er zijn geen waarschuwingen") -- daar kunnen we niet specifiek genoeg
    # Utrecht uit halen, dus altijd het .xml-bestand gebruiken.
    xml_files = [f for f in files if f["filename"].endswith(".xml")]
    if not xml_files:
        raise RuntimeError("Geen .xml-bestand gevonden in de nieuwste KNMI-waarschuwingen")
    return xml_files[0]["filename"]


def fetch_warnings_xml(api_key):
    filename = _latest_xml_filename(api_key)
    resp = requests.get(
        f"{BASE_URL}/files/{filename}/url",
        headers={"Authorization": api_key},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    download_url = resp.json()["temporaryDownloadUrl"]
    file_resp = requests.get(download_url, timeout=REQUEST_TIMEOUT)
    file_resp.raise_for_status()
    return ET.fromstring(file_resp.content)


def parse_utrecht_warnings(root):
    """Geeft per fenomeen (alleen als er ergens in de resterende cube-periode
    een actieve kleur -- geel/oranje/rood -- voor Utrecht voorkomt) de
    ernstigste kleur terug, wanneer die het ergst is, en vanaf wanneer het
    fenomeen actief wordt."""
    now = datetime.now(timezone.utc)
    by_phenomenon = {}
    for timeslice in root.iter("timeslice"):
        ts_id_el = timeslice.find("timeslice_id")
        if ts_id_el is None or not ts_id_el.text:
            continue
        try:
            ts_time = datetime.fromisoformat(ts_id_el.text)
        except ValueError:
            continue
        if ts_time < now:
            continue  # al gepasseerde uren negeren
        for phenomenon in timeslice.findall("phenomenon"):
            phen_id = phenomenon.findtext("phenomenon_id")
            for location in phenomenon.findall("location"):
                if location.findtext("location_id") != LOCATION_ID:
                    continue
                color = location.findtext("color_id") or "GREEN"
                if _COLOR_SEVERITY.get(color, 0) == 0:
                    continue  # GREEN/GRAY: geen waarschuwing
                header = (location.findtext("text/text_header") or "").strip()
                description = (location.findtext("text/text_data") or "").strip()
                by_phenomenon.setdefault(phen_id, []).append((ts_time, color, header, description))

    results = []
    for phen_id, entries in by_phenomenon.items():
        entries.sort(key=lambda e: e[0])
        worst = max(entries, key=lambda e: _COLOR_SEVERITY.get(e[1], 0))
        results.append({
            "phenomenon_id": phen_id,
            "phenomenon_label": _PHENOMENON_LABELS.get(phen_id, phen_id),
            "color": worst[1],
            "color_label": _COLOR_LABELS.get(worst[1], worst[1]),
            "active_from": entries[0][0].isoformat(),
            "worst_at": worst[0].isoformat(),
            "header": worst[2],
            "description": worst[3],
        })
    results.sort(key=lambda r: -_COLOR_SEVERITY.get(r["color"], 0))
    return results


def fetch_utrecht_warnings(api_key):
    return parse_utrecht_warnings(fetch_warnings_xml(api_key))
