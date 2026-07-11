# Deployen naar je VPS (ovreader.dvznet.nl)

Ga ervan uit dat je VPS Linux draait (Ubuntu/Debian-commando's hieronder;
pas `apt` aan naar jouw distro indien anders) en dat je SSH-toegang hebt.

## 0. Vooraf

- DNS: zorg dat `ovreader.dvznet.nl` een A-record heeft dat naar het
  IP-adres van je VPS wijst. Caddy (stap 6) regelt HTTPS automatisch zodra
  dit klopt.
- Poorten 80 en 443 moeten open staan in je firewall (bv. `ufw allow 80,443/tcp`).

## 1. Code naar de server krijgen

Vanaf je eigen pc (dit project staat lokaal in
`C:\Users\danny\projects\utrecht-bus-monitor`), via `rsync` over SSH.
Sluit de venv en grote/gegenereerde databestanden uit -- die worden op de
server opnieuw aangemaakt:

```powershell
# Vanuit Git Bash / WSL op je Windows-pc
rsync -avz --exclude venv --exclude data/gtfs-nl.zip --exclude '*.db*' \
  --exclude data/utrecht_routes.json --exclude data/utrecht_stops.json --exclude data/utrecht_trips.json \
  /c/Users/danny/projects/utrecht-bus-monitor/ gebruiker@jouw-vps-ip:/opt/utrecht-bus-monitor/
```

Geen rsync beschikbaar? Dan kan ook `scp -r` (kopieert alles inclusief venv,
dus trager) of: zet dit project op GitHub (git is al geïnitialiseerd) en doe
`git clone` op de server.

Voor latere updates herhaal je gewoon dezelfde rsync (of `git pull`).

## 2. Gebruiker en Python-omgeving op de server

```bash
ssh gebruiker@jouw-vps-ip
sudo useradd -r -s /bin/false utrechtbus || true
sudo chown -R utrechtbus:utrechtbus /opt/utrecht-bus-monitor

cd /opt/utrecht-bus-monitor
sudo apt update && sudo apt install -y python3-venv python3-pip
sudo -u utrechtbus python3 -m venv venv
sudo -u utrechtbus ./venv/bin/pip install -r requirements.txt
sudo -u utrechtbus ./venv/bin/pip install gunicorn
```

## 3. Statische data opbouwen (welke haltes/lijnen horen bij Utrecht)

Dit downloadt eenmalig de landelijke GTFS-feed (~240 MB) en filtert 'm op de
provinciegrens:

```bash
sudo -u utrechtbus ./venv/bin/python -m app.build_static_index
```

Duurt een paar minuten. Resultaat: `data/utrecht_routes.json`,
`utrecht_stops.json`, `utrecht_trips.json`.

## 4. Wachtwoord instellen (verplicht -- dit komt op het internet te staan)

```bash
sudo -u utrechtbus cp .env.example .env
sudo -u utrechtbus nano .env   # vul een echt wachtwoord in bij BUS_MONITOR_PASSWORD
sudo chmod 600 .env
```

Zonder dit bestand is het dashboard voor iedereen op internet toegankelijk
zonder login.

## 5. systemd-services installeren

Er zijn twee aparte services: één die op de achtergrond data verzamelt, één
die het dashboard serveert. Dat voorkomt dat de realtime feeds dubbel
bevraagd worden als de webserver met meerdere workers draait.

```bash
sudo cp deploy/utrecht-bus-collector.service /etc/systemd/system/
sudo cp deploy/utrecht-bus-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now utrecht-bus-collector
sudo systemctl enable --now utrecht-bus-web
sudo systemctl status utrecht-bus-collector utrecht-bus-web
```

Logs bekijken: `sudo journalctl -u utrecht-bus-collector -f` (of `-web`).

## 6. HTTPS via Caddy (reverse proxy)

Caddy regelt automatisch een geldig SSL-certificaat (Let's Encrypt) zodra
het domein naar dit IP wijst -- geen handmatige certbot-toestanden nodig.

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

De meegeleverde `deploy/Caddyfile` is al ingesteld op `ovreader.dvznet.nl`.

## 7. Testen

- https://ovreader.dvznet.nl/ moet om inloggegevens vragen (Basic Auth) en
  daarna het live dashboard tonen.
- Wil  voor het uitval-dashboard.
- `sudo journalctl -u utrecht-bus-collector -f` moet elke 30s een regel
  "fetch klaar op ..." laten zien.

## Onderhoud

- Statische dienstregeling verversen (maandelijks, of na een grote wijziging):
  `sudo -u utrechtbus ./venv/bin/python -m app.build_static_index`
  gevolgd door `sudo systemctl restart utrecht-bus-collector utrecht-bus-web`.
- Code-updates: rsync/git pull opnieuw, dan
  `sudo systemctl restart utrecht-bus-collector utrecht-bus-web`.

## Back-ups van de historie

De collector schrijft elke nacht om 04:15 een back-up van de onvervangbare
historie-tabellen (uitval, gereden ritten, dagstatistieken -- niet de
gigantische ruwe `trip_delays`) naar `data/backups/history_YYYY-MM-DD.db.gz`;
de laatste 7 blijven staan. Dat beschermt tegen een kapotte database, maar
niet tegen verlies van de hele VPS -- haal het bestand daarom ook periodiek
op naar een andere machine. Voorbeeld voor een wekelijkse cronjob elders
(bv. een andere server): zet dit in een scriptje (niet rechtstreeks in de
crontab -- cron interpreteert `%` in `date +%F` als regeleinde) en plan het
in met bv. `30 5 * * 0 /root/bus-backup.sh`:

```sh
#!/bin/sh
BACKUP_DIR=/root/ovreader-backups
mkdir -p "$BACKUP_DIR"
# -f: laat een foutantwoord (401/404) falen i.p.v. een kapot bestand achter
curl -fsS -u admin:JOUW-WACHTWOORD \
  -o "$BACKUP_DIR/bus-historie-$(date +%F).db.gz" \
  https://ovreader.dvznet.nl/api/backup/latest
# laatste 8 bewaren
ls -1t "$BACKUP_DIR"/bus-historie-*.db.gz | tail -n +9 | xargs -r rm
```

Terugzetten: `gunzip history_*.db.gz` geeft een gewone SQLite-database met
alleen de historie-tabellen. Bij een verse installatie kun je die data
terugkopieren met bv.:

```
sqlite3 data/bus_monitor.db "ATTACH 'history_2026-07-11.db' AS b;
  INSERT OR IGNORE INTO trip_cancellations SELECT * FROM b.trip_cancellations;
  INSERT OR IGNORE INTO trips_ran_daily SELECT * FROM b.trips_ran_daily;
  INSERT OR IGNORE INTO route_stats_daily SELECT * FROM b.route_stats_daily;
  INSERT OR IGNORE INTO route_stats_period_daily SELECT * FROM b.route_stats_period_daily;
  DETACH b;"
```

(Draai eerst een keer de app of `python -m app.db` zodat het schema bestaat.)
