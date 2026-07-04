"""WSGI-entrypoint voor productie (gunicorn/waitress).

Start GEEN collector-scheduler -- draai app/collector_service.py als apart
proces/service daarvoor. Zo kan de webserver met meerdere workers draaien
zonder dat de realtime feeds dubbel bevraagd worden.

Gebruik: gunicorn -w 2 -b 127.0.0.1:5151 app.wsgi:app
"""
from .server import create_app

app = create_app()
