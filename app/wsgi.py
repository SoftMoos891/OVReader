"""WSGI-entrypoint voor productie (gunicorn/waitress).

Start GEEN collector-scheduler -- draai app/collector_service.py als apart
proces/service daarvoor. Zo kan de webserver met meerdere workers draaien
zonder dat de realtime feeds dubbel bevraagd worden.

Gebruik: gunicorn --preload -w 1 -b 127.0.0.1:5151 app.wsgi:app
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
# per worker.
gc.freeze()
