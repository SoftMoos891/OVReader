"""Bouwt een compacte opzoektabel van AlertC-locatiecodes naar wegnummer en
plaatsnaam, voor de wegsituatie-meldingen (zie app/road_situations.py).

De NDW-wegsituatiefeed benoemt locaties niet met een leesbare wegnaam, maar
met een AlertC-locatiecode (<specificLocation>) die verwijst naar de VILD-
tabel. Die tabel staat als los bestand op hetzelfde open-dataportaal
(~40 MB zip, met daarin een dBase-bestand van ~3,5 MB). Dit script haalt
daar de vier relevante velden uit en schrijft ze weg als een compacte JSON
van ~470 KB.

Draai dit eenmalig (en opnieuw zodra NDW een nieuwe VILD-versie uitbrengt,
zie VILD_TABLE_VERSION hieronder -- road_situations.py waarschuwt in het
collector-log zodra de feed naar een andere versie verwijst dan deze).

Zonder dit bestand blijft alles gewoon werken: de wegsituatie-meldingen
tonen dan alleen geen wegnummer, precies zoals voorheen.
"""
import json
import struct
import zipfile
from io import BytesIO
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "vild_locations.json"

# De feed (actueel_beeld.xml) verwijst via alertCLocationTableNumber/Version
# naar welke VILD-tabel geldt; op dit moment 6.13 versie A.
VILD_TABLE_VERSION = "6.13.A"
VILD_ZIP_URL = f"https://opendata.ndw.nu/VILD{VILD_TABLE_VERSION}.zip"
VILD_DBF_NAME = f"VILD{VILD_TABLE_VERSION}.dbf"

# LOC_NR is de AlertC-code uit de feed; de rest levert de leesbare aanduiding.
WANTED_FIELDS = {"LOC_NR", "ROADNUMBER", "FIRST_NAME", "SECND_NAME"}


def log(msg):
    print(f"[build_vild_index] {msg}", flush=True)


def _read_dbf(data):
    """Minimale dBase-lezer: genoeg voor dit ene bestand, en scheelt een
    externe dependency (dbfread/pandas) voor een formaat dat uit een vaste
    header + records met vaste veldbreedtes bestaat."""
    record_count, header_len, record_len = struct.unpack("<I H H", data[4:12])

    fields = []
    pos = 32
    while data[pos] != 0x0D:  # 0x0D sluit de veldbeschrijvingen af
        raw = data[pos:pos + 32]
        name = raw[:11].split(b"\x00")[0].decode("latin-1")
        fields.append((name, raw[16]))  # byte 16 = veldlengte
        pos += 32

    for i in range(record_count):
        start = header_len + i * record_len
        record = data[start:start + record_len]
        offset, row = 1, {}  # byte 0 is de verwijder-vlag
        for name, length in fields:
            if name in WANTED_FIELDS:
                row[name] = record[offset:offset + length].decode("latin-1").strip()
            offset += length
        yield row


def build_lookup(dbf_bytes):
    """code -> [wegnummer, naam1, naam2] (achterste elementen weggelaten als
    ze leeg zijn -- scheelt fors in bestandsgrootte over ~12.000 rijen)."""
    lookup = {}
    for row in _read_dbf(dbf_bytes):
        code = row.get("LOC_NR")
        if not code:
            continue
        road, first, second = row["ROADNUMBER"], row["FIRST_NAME"], row["SECND_NAME"]
        if not (road or first):
            continue  # zonder wegnummer én zonder plaatsnaam valt er niets te tonen
        if second:
            lookup[code] = [road, first, second]
        elif first:
            lookup[code] = [road, first]
        else:
            lookup[code] = [road]
    return lookup


def main():
    DATA_DIR.mkdir(exist_ok=True)
    log(f"Download VILD-tabel {VILD_TABLE_VERSION} (~40 MB)...")
    resp = requests.get(VILD_ZIP_URL, timeout=300)
    resp.raise_for_status()

    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        log(f"Uitpakken en parsen van {VILD_DBF_NAME}...")
        dbf_bytes = zf.read(VILD_DBF_NAME)

    lookup = build_lookup(dbf_bytes)
    OUT_PATH.write_text(json.dumps(lookup, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"{len(lookup):,} locaties weggeschreven naar {OUT_PATH.name} "
        f"({OUT_PATH.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
