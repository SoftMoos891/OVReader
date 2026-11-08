"""Achter nginx zet de tweestapsinlog (dvz-auth) de kop X-DVZ-2FA; de app
vertrouwt die alleen van de lokale proxy."""
from app import server


def _met_wachtwoord(monkeypatch):
    monkeypatch.setattr(server, "AUTH_PASSWORD", "geheim")


def test_kop_van_lokale_proxy_laat_door(client, monkeypatch):
    _met_wachtwoord(monkeypatch)
    kop = {"X-DVZ-2FA": "Telefoon Danny"}
    assert client.get("/", headers=kop).status_code == 200
    assert client.get("/api/health", headers=kop).status_code == 200
    # Inlogpagina is dan overbodig: meteen door naar waar je heen wilde.
    r = client.get("/login?next=/uitval", headers=kop)
    assert r.status_code == 302 and r.headers["Location"].endswith("/uitval")
    r = client.get("/logout", headers=kop)
    assert r.headers["Location"].endswith("/auth/apparaten.html")
    assert b"Log uit" in client.get("/liften", headers=kop).data


def test_zonder_kop_of_van_buiten_niet(client, monkeypatch):
    _met_wachtwoord(monkeypatch)
    assert client.get("/").status_code == 302
    buiten = {"REMOTE_ADDR": "203.0.113.5"}
    assert client.get("/", headers={"X-DVZ-2FA": "nep"}, environ_base=buiten).status_code == 302
    assert client.get("/api/health", headers={"X-DVZ-2FA": "nep"}, environ_base=buiten).status_code == 401
    assert client.get("/", headers={"X-DVZ-2FA": ""}).status_code == 302
