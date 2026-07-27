"""Actueel weer (De Bilt, provincie Utrecht) via KNMI.

Bron: KNMI Open Data API, dataset "10-minute-in-situ-meteorological-
observations" versie 1.0 -- elke 10 minuten een nieuw bestand met actuele
waarnemingen van alle ~58 KNMI-weerstations in Nederland. Vereist dezelfde
KNMI_API_KEY als de weerwaarschuwingen (zie knmi_warnings.py) -- als die key
is ingesteld werkt dit automatisch mee, geen aparte instelling nodig.

Het bestand is NetCDF4/HDF5 (binair), niet handmatig te parsen zoals bv. de
DBF/DATEX II-bestanden elders in dit project -- vandaar de dependency op
h5py (lichter dan de volledige netCDF4-package: kant-en-klare wheels, geen
los te installeren C-library nodig).

Station "06260" (De Bilt) is het KNMI-hoofdstation, middenin de provincie
Utrecht -- vast stationnummer, geen fuzzy naam-matching nodig."""
from datetime import datetime, timedelta, timezone
from io import BytesIO

import h5py
import requests

DATASET = "10-minute-in-situ-meteorological-observations"
VERSION = "1.0"
BASE_URL = f"https://api.dataplatform.knmi.nl/open-data/v1/datasets/{DATASET}/versions/{VERSION}"
REQUEST_TIMEOUT = 60
STATION_CODE = b"06260"  # De Bilt

_COMPASS_POINTS = [
    "N", "NNO", "NO", "ONO", "O", "OZO", "ZO", "ZZO",
    "Z", "ZZW", "ZW", "WZW", "W", "WNW", "NW", "NNW",
]
# Schaal van Beaufort (windsnelheid in m/s -> windkracht), standaard grenzen.
_BEAUFORT_THRESHOLDS = [0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7]


def _latest_filename(api_key):
    resp = requests.get(
        f"{BASE_URL}/files",
        params={"maxKeys": 5, "orderBy": "created", "sorting": "desc"},
        headers={"Authorization": api_key},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    files = resp.json()["files"]
    if not files:
        raise RuntimeError("Geen bestanden gevonden voor de KNMI 10-minuten-waarnemingen")
    return files[0]["filename"]


def fetch_observations_file(api_key):
    filename = _latest_filename(api_key)
    resp = requests.get(
        f"{BASE_URL}/files/{filename}/url",
        headers={"Authorization": api_key},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    download_url = resp.json()["temporaryDownloadUrl"]
    file_resp = requests.get(download_url, timeout=REQUEST_TIMEOUT)
    file_resp.raise_for_status()
    return file_resp.content


def _clean(value):
    """numpy-scalar -> gewone Python-float, afgerond; NaN (ontbrekende
    meting) -> None. 'value != value' is de klassieke NaN-test zonder
    math-import (NaN is het enige getal dat niet aan zichzelf gelijk is)."""
    value = float(value)
    return None if value != value else round(value, 1)


def _beaufort(speed_ms):
    if speed_ms is None:
        return None
    for scale, threshold in enumerate(_BEAUFORT_THRESHOLDS):
        if speed_ms < threshold:
            return scale
    return 12


def _compass_direction(degrees):
    if degrees is None:
        return None
    return _COMPASS_POINTS[round(degrees / 22.5) % 16]


def parse_de_bilt_observation(nc_bytes):
    with h5py.File(BytesIO(nc_bytes), "r") as f:
        stations = f["station"][:]
        idx = next((i for i, s in enumerate(stations) if s == STATION_CODE), None)
        if idx is None:
            raise RuntimeError("Station De Bilt (06260) niet gevonden in het KNMI-bestand")

        time_raw = float(f["time"][0])  # seconden sinds 1950-01-01 (CF-conventie)
        observed_at = datetime(1950, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=time_raw)

        temperature = _clean(f["ta"][idx][0])
        dew_point = _clean(f["td"][idx][0])
        humidity = _clean(f["rh"][idx][0])
        wind_speed_ms = _clean(f["ff"][idx][0])
        wind_gust_ms = _clean(f["fx"][idx][0])
        wind_direction = _clean(f["dd"][idx][0])
        pressure = _clean(f["pp"][idx][0])
        cloud_cover = _clean(f["n"][idx][0])

    return {
        "station": "De Bilt",
        "observed_at": observed_at.isoformat(),
        "temperature": temperature,
        "dew_point": dew_point,
        "humidity": humidity,
        "wind_speed_ms": wind_speed_ms,
        "wind_speed_bft": _beaufort(wind_speed_ms),
        "wind_gust_ms": wind_gust_ms,
        "wind_direction": wind_direction,
        "wind_direction_compass": _compass_direction(wind_direction),
        "pressure": pressure,
        "cloud_cover_okta": cloud_cover,
    }


def fetch_de_bilt_weather(api_key):
    return parse_de_bilt_observation(fetch_observations_file(api_key))
