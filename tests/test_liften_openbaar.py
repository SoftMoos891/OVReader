def test_liften_openbaar_rest_niet(client, monkeypatch):
    from app import server

    monkeypatch.setattr(server, "AUTH_PASSWORD", "geheim")
    r = client.get("/liften")
    assert r.status_code == 200
    assert b"Log uit" not in r.data
    assert client.get("/api/liften").status_code == 200
    assert client.get("/").status_code == 302
    assert client.get("/api/health").status_code == 401
