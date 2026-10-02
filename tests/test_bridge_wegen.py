"""Wegsituaties via de DVZ RSS-bridge (sinds 2 okt 2026)."""
import time
from unittest.mock import MagicMock, patch

import pytest

from app import bridge_wegen


def _antwoord(**data):
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


def test_verse_lijst_komt_door():
    lijst = [{"situation_id": "NDW06_x", "record_type": "Accident"}]
    with patch.object(bridge_wegen.requests, "get", return_value=_antwoord(situaties=lijst, tijd=time.time() * 1000)) as get:
        assert bridge_wegen.fetch_utrecht_road_situations() == lijst
    assert get.call_args[0][0] == "http://127.0.0.1:3001/v1/wegsituaties-ruw"


def test_lege_lijst_is_geen_fout():
    with patch.object(bridge_wegen.requests, "get", return_value=_antwoord(situaties=[], tijd=time.time() * 1000)):
        assert bridge_wegen.fetch_utrecht_road_situations() == []


def test_oud_beeld_is_een_fout():
    with patch.object(bridge_wegen.requests, "get", return_value=_antwoord(situaties=[], tijd=(time.time() - 20 * 60) * 1000)):
        with pytest.raises(RuntimeError, match="20 minuten oud"):
            bridge_wegen.fetch_utrecht_road_situations()


def test_nog_niets_in_de_bridge():
    with patch.object(bridge_wegen.requests, "get", return_value=_antwoord(situaties=None, tijd=None)):
        with pytest.raises(RuntimeError, match="nog geen"):
            bridge_wegen.fetch_utrecht_road_situations()
