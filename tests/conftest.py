import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as db_module


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Wijst db.DB_PATH tijdelijk naar een lege testdatabase, zodat tests nooit
    de echte data/bus_monitor.db aanraken."""
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()
    return db_module


@pytest.fixture()
def client(temp_db, monkeypatch):
    from app import server

    # Testroutes bestaan niet in de echte (live) statische GTFS-index -- laat
    # is_relevant_route/is_bus_route alles doorlaten zodat tests niet
    # afhankelijk zijn van de actuele dienstregeling.
    monkeypatch.setattr(server._index, "is_relevant_route", lambda route_id: True)
    monkeypatch.setattr(server._index, "is_bus_route", lambda route_id: True)
    server.app.testing = True

    # server._response_cache is een module-brede TTL-cache (zie _cached() in
    # server.py) die overleeft tussen tests, terwijl temp_db elke test een
    # eigen lege database geeft -- zonder deze reset kan een test binnen de
    # TTL het gecachte antwoord van een vorige test terugkrijgen in plaats
    # van tegen zijn eigen data te draaien.
    server._response_cache.clear()

    return server.app.test_client()
