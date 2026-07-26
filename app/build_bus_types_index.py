"""Bouwt een opzoektabel van voertuignummer naar bustype (bv. "VDL Citea
LF-122 Electric"), voor de voertuig-popup op de kaart.

Bron: bussen.ov-database.nl, een community-onderhouden overzicht van
Nederlandse OV-voertuigen. Geen officiele API, geen key nodig -- gewoon een
publiek toegankelijke pagina per concessie, met per bustype een sectie met
voertuignummers. Kan van opmaak veranderen zonder aankondiging (het is geen
contract), dus dit script faalt zichtbaar (log + lege output) i.p.v. de rest
van de app te breken als de structuur ooit wijzigt.

Draai dit net als build_static_index.py/build_vild_index.py incidenteel
opnieuw (bv. na een grote vlootwijziging) om nieuwe/vervangen voertuignummers
bij te werken -- de vloot verandert traag, dus dagelijks verversen heeft geen
zin en zou deze (niet-officiele, community) bron onnodig belasten."""
import json
import re
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "bus_types.json"

# concessiecode (zoals gebruikt in vehicle.label-achtige nummering op de
# site) -> concessie-slug in de URL. CXX is de historische Connexxion-code,
# nog steeds in gebruik voor Transdev Nederland Utrecht Binnen.
CONCESSIONS = {
    "KEO": "KEO:Utrecht_Buiten",
    "CXX": "CXX:Utrecht_Binnen",
}
BASE_URL = "https://bussen.ov-database.nl/concessie.php"
REQUEST_TIMEOUT = 20
_SECTION_RE = re.compile(r"<section><h5>(.*?)</h5>(.*?)</section>", re.S)


def log(msg):
    print(f"[build_bus_types_index] {msg}", flush=True)


def _parse_concessie_page(html, concessie_code):
    """code -> {vehicle_number: bus_type}. Elke pagina is opgebouwd als
    <section><h5>Bustype</h5><a href="bus.php?voertuig=KEO:1234">1234</a>...</section>,
    herhaald per bustype."""
    result = {}
    vehicle_re = re.compile(rf"voertuig={concessie_code}:(\d+)")
    for type_name, body in _SECTION_RE.findall(html):
        type_name = type_name.strip()
        for number in vehicle_re.findall(body):
            result[number] = type_name
    return result


def build_lookup():
    lookup = {}
    for concessie_code, slug in CONCESSIONS.items():
        log(f"Ophalen {slug}...")
        resp = requests.get(BASE_URL, params={"concessie": slug}, timeout=REQUEST_TIMEOUT,
                             headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        parsed = _parse_concessie_page(resp.text, concessie_code)
        if not parsed:
            log(f"WAARSCHUWING: geen voertuigen gevonden voor {slug} -- is de paginaopmaak gewijzigd?")
        log(f"{len(parsed)} voertuigen gevonden voor {slug}.")
        lookup.update(parsed)
    return lookup


def main():
    DATA_DIR.mkdir(exist_ok=True)
    lookup = build_lookup()
    OUT_PATH.write_text(json.dumps(lookup, ensure_ascii=False), encoding="utf-8")
    log(f"{len(lookup)} voertuignummers weggeschreven naar {OUT_PATH.name}")


if __name__ == "__main__":
    main()
