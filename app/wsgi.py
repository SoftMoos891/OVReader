"""WSGI-entrypoint voor productie (gunicorn/waitress).

Start GEEN collector-scheduler -- draai app/collector_service.py als apart
proces/service daarvoor. Zo kan de webserver met meerdere workers draaien
zonder dat de realtime feeds dubbel bevraagd worden.

Gebruik: gunicorn --preload -w 2 -b 127.0.0.1:5151 app.wsgi:app

Aantal workers: richt op het aantal vCores van de VPS (2 op de huidige
ovreader.dvznet.nl-server), zodat gelijktijdige requests over de beschikbare
cores verdeeld worden i.p.v. achter elkaar op 1 core te wachten -- een sync
gunicorn-worker is single-threaded en verwerkt requests strikt na elkaar.
Zonder --preload + gc.freeze() (zie hieronder) verdubbelt elke extra worker
het geheugengebruik met de volledige dienstregeling; verhoog dit getal dus
niet zonder die twee ook aan te laten staan.
"""
import gc

from .server import create_app

app = create_app()

# Met --preload wordt deze module een keer in het master-proces geimporteerd
# voordat er geforkt wordt. gc.freeze() verplaatst alle nu levende objecten
# (o.a. de complete dienstregeling, tientallen MB's Python dicts/lists) naar
# een generatie die de garbage collector nooit meer scant. Zonder dit raakt
# elke worker binnen enkele minuten na de fork alle geheugenpagina's aan
# doordat cyclische GC-runs refcounts bijwerken, wat copy-on-write-sharing
# met het master-proces teniet doet en het geheugengebruik laat verdubbelen
# per worker. Ga je het aantal workers verder verhogen dan het aantal
# vCores: controleer eerst met `free -m`/`htop` dat de COW-sharing hier ook
# echt standhoudt onder productielast (zie git-geschiedenis van dit bestand
# voor de eerdere OOM-crashloop met 2 workers zonder deze freeze).
gc.freeze()
