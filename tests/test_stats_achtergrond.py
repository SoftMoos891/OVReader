"""/api/stats op de live-pagina: na het verlopen krijgt de bezoeker meteen de
vorige uitkomst en wordt de nieuwe op de achtergrond berekend."""
import threading
import time

from app import server


def test_verlopen_waarde_direct_terug_en_op_achtergrond_ververst(monkeypatch):
    server._response_cache.clear()
    sleutel = ("proef",)
    server._response_cache[sleutel] = (time.time() - 10_000, "oud")
    klaar = threading.Event()

    def bereken():
        klaar.wait(5)
        return "nieuw"

    t0 = time.time()
    assert server._cached(sleutel, 60, bereken, achtergrond=True) == "oud"
    assert time.time() - t0 < 1, "de bezoeker wacht niet op de berekening"
    # Tweede verzoek terwijl de berekening loopt: geen tweede thread.
    assert server._cached(sleutel, 60, bereken, achtergrond=True) == "oud"
    assert sleutel in server._bezig
    klaar.set()
    for _ in range(50):
        if sleutel not in server._bezig:
            break
        time.sleep(0.05)
    assert server._cached(sleutel, 60, bereken, achtergrond=True) == "nieuw"


def test_zonder_eerdere_waarde_wordt_gewoon_gerekend():
    server._response_cache.clear()
    assert server._cached(("leeg",), 60, lambda: 42, achtergrond=True) == 42


def test_achtergrondfout_laat_oude_waarde_staan():
    server._response_cache.clear()
    sleutel = ("fout",)
    server._response_cache[sleutel] = (time.time() - 10_000, "oud")

    def kapot():
        raise RuntimeError("stuk")

    assert server._cached(sleutel, 60, kapot, achtergrond=True) == "oud"
    for _ in range(50):
        if sleutel not in server._bezig:
            break
        time.sleep(0.05)
    assert server._response_cache[sleutel][1] == "oud"
