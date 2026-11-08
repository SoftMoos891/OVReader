"""Gunicorn-instellingen voor utrecht-bus-web en utrecht-bus-lite. Gunicorn
leest dit bestand vanzelf uit de werkmap (/opt/utrecht-bus-monitor).

Snel afsluiten: de webserver houdt de dienstregeling (utrecht_stop_times.json)
als grote Python-dict in het geheugen, en die staat na een tijdje grotendeels
in swap. Bij het afsluiten ruimde Python die dict netjes op, waarvoor alles
eerst uit swap terug moest: op 4 nov 2026 duurde dat langer dan de 90 s van
systemd, die het proces toen afschoot (en een mislukt-melding gaf). Na
"Shutting down" valt er niets meer op te ruimen, dus stoppen we meteen, met de
exitcode die gunicorn zelf zou geven."""
import os
import sys


def on_starting(server):
    oorspronkelijk = server.halt

    def halt(reason=None, exit_status=0):
        server.dvz_exitcode = exit_status
        return oorspronkelijk(reason, exit_status)

    server.halt = halt


def on_exit(server):
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(getattr(server, "dvz_exitcode", 0))
