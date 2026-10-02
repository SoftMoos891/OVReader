"""Weer, KNMI-waarschuwingen en luchtkwaliteit via de DVZ RSS-bridge (sinds 2 okt 2026)."""
import time
from unittest.mock import MagicMock, patch

import pytest

from app import bridge_weer


def _antwoord(**data):
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


def test_verse_data_komt_door():
    nu = time.time() * 1000
    data = {"weer": {"temperature": 10.2}, "weerTijd": nu - 60_000,
            "waarschuwingen": [{"phenomenon_id": "VV", "color": "YELLOW"}], "waarschTijd": nu - 60_000,
            "lucht": {"lki": 3, "concentrations": {}}, "luchtTijd": nu - 60_000, "fouten": {}}
    with patch.object(bridge_weer.requests, "get", return_value=_antwoord(**data)) as get:
        assert bridge_weer.fetch_de_bilt_weather()["temperature"] == 10.2
        assert bridge_weer.fetch_utrecht_warnings()[0]["phenomenon_id"] == "VV"
        assert bridge_weer.fetch_air_quality()["lki"] == 3
    assert get.call_args[0][0].startswith("http://127.0.0.1:3001/")


def test_lege_waarschuwingenlijst_is_geen_fout():
    with patch.object(bridge_weer.requests, "get",
                      return_value=_antwoord(waarschuwingen=[], waarschTijd=time.time() * 1000)):
        assert bridge_weer.fetch_utrecht_warnings() == []


def test_verouderde_data_is_een_fout():
    oud = (time.time() - 45 * 60) * 1000
    with patch.object(bridge_weer.requests, "get", return_value=_antwoord(weer={"temperature": 9}, weerTijd=oud)):
        with pytest.raises(RuntimeError, match="45 minuten oud"):
            bridge_weer.fetch_de_bilt_weather()


def test_luchtkwaliteit_mag_langer_oud_zijn():
    oud = (time.time() - 60 * 60) * 1000
    with patch.object(bridge_weer.requests, "get", return_value=_antwoord(lucht={"lki": 4}, luchtTijd=oud)):
        assert bridge_weer.fetch_air_quality()["lki"] == 4


def test_nog_niets_in_de_bridge_noemt_de_fout():
    with patch.object(bridge_weer.requests, "get",
                      return_value=_antwoord(waarschuwingen=None, waarschTijd=None, fouten={"waarschuwingen": "HTTPError: 429"})):
        with pytest.raises(RuntimeError, match="429"):
            bridge_weer.fetch_utrecht_warnings()


def test_bridge_onbereikbaar_geeft_een_fout():
    with patch.object(bridge_weer.requests, "get", side_effect=bridge_weer.requests.ConnectionError("weg")):
        with pytest.raises(bridge_weer.requests.ConnectionError):
            bridge_weer.fetch_de_bilt_weather()
