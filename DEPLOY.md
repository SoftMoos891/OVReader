# Deployen naar je VPS (ovreader.dvznet.nl)

Ga ervan uit dat je VPS Linux draait (Ubuntu/Debian-commando's hieronder;
pas `apt` aan naar jouw distro indien anders) en dat je SSH-toegang hebt.

## 0. Vooraf

- DNS: zorg dat `ovreader.dvznet.nl` een A-record heeft dat naar het
  IP-adres van je VPS wijst.
- Poorten 80 en 443 moeten open staan in je firewall (bv. `ufw allow 80,443/tcp`).
- Dit document gaat ervan uit dat de VPS met **HestiaCP** beheerd wordt en
  het domein daar al is aangemaakt (webdomein + Let's Encrypt-certificaat),
  met nginx als reverse proxy naar de gunicorn-app op poort 5151 -- zie
  stap 6. Het eenmalig aanmaken van een nieuw webdomein in Hestia zelf valt
  buiten dit document; zie de HestiaCP-documentatie.

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

Er zijn drie aparte services: één die op de achtergrond data verzamelt, één
die het volledige (met Basic Auth afgeschermde) dashboard serveert, en één
die de publieke lite-versie serveert (alleen storingen + uitval, geen auth --
zie `app/lite_server.py`). Gescheiden services voorkomen dat de realtime
feeds dubbel bevraagd worden, en isoleren de publieke, ongeauthenticeerde
lite-pagina van je eigen dashboard (los proces, eigen geheugenlimiet).

```bash
sudo cp deploy/utrecht-bus-collector.service /etc/systemd/system/
sudo cp deploy/utrecht-bus-web.service /etc/systemd/system/
sudo cp deploy/utrecht-bus-lite.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now utrecht-bus-collector
sudo systemctl enable --now utrecht-bus-web
sudo systemctl enable --now utrecht-bus-lite
sudo systemctl status utrecht-bus-collector utrecht-bus-web utrecht-bus-lite
```

Logs bekijken: `sudo journalctl -u utrecht-bus-collector -f` (of `-web`/`-lite`).

## 6. Reverse proxy: nginx via HestiaCP

Deze server draait **nginx** (beheerd via HestiaCP), niet Caddy -- eerdere
versies van dit document gingen daar ten onrechte van uit. HTTPS/Let's
Encrypt en de basisroutering (`/` naar poort 5151, `/static/` als directe
disk-alias) zijn al geregeld via het HestiaCP-webdomein en staan in
`/etc/nginx/conf.d/domains/<domein>.conf(.ssl)`.

**Bewerk die gegenereerde bestanden nooit rechtstreeks** -- HestiaCP
overschrijft ze zodra je iets via het paneel wijzigt of het certificaat
vernieuwt. Voor eigen aanvullingen (zoals de `/lite`-route) is er een
include-hook die altijd blijft staan en elk bijpassend bestand automatisch
meeneemt:

```
/home/hestiaadmin/conf/web/<domein>/nginx.conf_*      (HTTP-serverblok)
/home/hestiaadmin/conf/web/<domein>/nginx.ssl.conf_*  (SSL-serverblok -- dit bedient het echte verkeer)
```

Om de publieke lite-service te ontsluiten:

```bash
sudo cp deploy/nginx-lite.conf /home/hestiaadmin/conf/web/ovreader.dvznet.nl/nginx.conf_lite
sudo cp deploy/nginx-lite.conf /home/hestiaadmin/conf/web/ovreader.dvznet.nl/nginx.ssl.conf_lite
sudo nginx -t && sudo systemctl reload nginx
```

`nginx -t` test de configuratie zonder 'm te laden -- controleer dat die
"successful" meldt voordat je `reload` draait. Zie ook `deploy/nginx-lite.conf`
voor de precieze proxy-instellingen.

(Draait jouw server toch los nginx of Caddy zonder HestiaCP? Dan is het
equivalent gewoon een `location /lite { proxy_pass http://127.0.0.1:5152; ... }`
-blok resp. `handle /lite* { reverse_proxy 127.0.0.1:5152 }` in je eigen
config, met dezelfde proxy-headers als in `deploy/nginx-lite.conf`.)

## 7. Testen

- https://ovreader.dvznet.nl/ moet om inloggegevens vragen (Basic Auth) en
  daarna het live dashboard tonen.
- https://ovreader.dvznet.nl/uitval moet (ook achter Basic Auth) het
  uitval-dashboard tonen.
- https://ovreader.dvznet.nl/lite moet zonder inloggegevens direct de
  publieke lite-versie (storingen + uitval) tonen.
- `sudo journalctl -u utrecht-bus-collector -f` moet elke 30s een regel
  "fetch klaar op ..." laten zien.

## Onderhoud

- Statische dienstregeling verversen (maandelijks, of na een grote wijziging):
  `sudo -u utrechtbus ./venv/bin/python -m app.build_static_index`
  gevolgd door
  `sudo systemctl restart utrecht-bus-collector utrecht-bus-web utrecht-bus-lite`
  (de lite-service leest ook `utrecht_routes.json`, dus wel meenemen).
- Code-updates: rsync/git pull opnieuw, dan
  `sudo systemctl restart utrecht-bus-collector utrecht-bus-web utrecht-bus-lite`.

## Back-ups van de historie

De collector schrijft elke nacht om 04:15 een back-up van de onvervangbare
historie-tabellen (uitval, gereden ritten, dagstatistieken -- niet de
gigantische ruwe `trip_delays`) naar `data/backups/history_YYYY-MM-DD.db.gz`;
de laatste 7 blijven staan. Dat beschermt tegen een kapotte database, maar
niet tegen verlies van de hele VPS -- haal het bestand daarom ook periodiek
op naar een andere machine. Voorbeeld voor een wekelijkse cronjob elders
(bv. een andere server). Zet de inloggegevens in een netrc-bestand (dan
staan ze niet in het script, de proceslijst of secret-scanner-triggers):

```
# /root/.ovreader-netrc, daarna: chmod 600 /root/.ovreader-netrc
machine ovreader.dvznet.nl login admin password VUL-HIER-IN
```

Het script zelf (niet rechtstreeks in de crontab -- cron interpreteert `%`
in `date +%F` als regeleinde); inplannen met bv.
`30 5 * * 0 /root/bus-backup.sh`:

```sh
#!/bin/sh
set -e  # stop (en laat de cron-taak falen) zodra de download mislukt
BACKUP_DIR=/root/ovreader-backups
mkdir -p "$BACKUP_DIR"
# -f: laat een foutantwoord (401/404) falen i.p.v. een kapot bestand achter
curl -fsS --netrc-file /root/.ovreader-netrc \
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
