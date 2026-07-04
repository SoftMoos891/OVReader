"""Standalone proces dat alleen de achtergrond-collector draait.

Gescheiden van de webserver zodat gunicorn met meerdere workers kan draaien
zonder dat de realtime feeds dubbel bevraagd worden (elke worker zou anders
zijn eigen scheduler starten). Draai dit als eigen systemd-service naast de
webserver-service.
"""
import time

from .collector import start_scheduler


def main():
    start_scheduler()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
