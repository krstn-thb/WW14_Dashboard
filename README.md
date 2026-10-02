# WW14 Dashboard

Vollbild-Dashboard für einen Raspberry Pi 5. Es bündelt Loxone-Klimadaten aus InfluxDB, Journal- und Konferenz-Deadlines, wichtige Kalendertermine, ausgewählte Aktienkurse und eine gemeinsame To-do-Liste.

Das Projekt ist bewusst schlank aufgebaut: Ein Python-Dienst liefert Daten und Oberfläche, Chromium zeigt sie im Vollbildmodus. Auf dem Raspberry Pi ist kein KI-Agent notwendig.

## Aktueller Funktionsumfang

- responsive Vollbild-Oberfläche für 16:9-Monitore, optimiert für Full HD und 4K
- selbstständige Aktualisierung; Standardintervall 60 Sekunden
- Loxone-/InfluxDB-Messwerte mit 24-Stunden-Verlauf
- Auswahl vorhandener InfluxDB-Messfelder direkt im Dashboard
- Deadlines und wichtige Termine direkt im Dashboard eintragen und löschen
- Wetter für Brandenburg an der Havel mit Tagesverlauf und animierter DWD-Regenvorschau für die nächsten zwei Stunden
- Aktienkurse mit Suche, Hinzufügen und Entfernen im Dashboard
- persistente To-do-Liste in SQLite
- Offline-Anzeige und Fehlerisolierung je Datenquelle
- automatischer Start als Benutzer-Service und Chromium-Vollbild auf Raspberry Pi OS

Ohne Zugangsdaten startet das Dashboard mit gekennzeichneten Beispieldaten. Dadurch kann die Oberfläche sofort geprüft werden.

## Lokal unter Windows starten

PowerShell im Projektordner öffnen und ausführen:

```powershell
.\scripts\start.ps1
```

Danach [http://localhost:8080](http://localhost:8080) öffnen. Beim ersten Start wird eine virtuelle Python-Umgebung eingerichtet; das kann einige Minuten dauern.

Alternativ manuell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Auf dem Raspberry Pi installieren

Auf Raspberry Pi OS 64 Bit:

```bash
git clone https://github.com/krstn-thb/WW14_Dashboard.git
cd WW14_Dashboard
bash scripts/install-pi.sh
```

Das Skript richtet eine eigene Python-Umgebung ein, startet den Dienst automatisch und legt den Chromium-Autostart für den Vollbildmodus an. Nach dem nächsten grafischen Login erscheint das Dashboard automatisch. Es ist zusätzlich im lokalen Netz unter `http://IP-DES-PI:8080` erreichbar.

Der Vollbildmodus lässt sich mit `F11` verlassen. `Alt` + `F4` schließt Chromium vollständig.

Status prüfen:

```bash
systemctl --user status ww14-dashboard
journalctl --user -u ww14-dashboard -f
```

Nach einem späteren Update:

```bash
git pull
.venv/bin/python -m pip install -r requirements.txt
systemctl --user restart ww14-dashboard
```

## Konfiguration

Die sichtbaren Inhalte stehen in `config/dashboard.yaml`. Zugangsdaten gehören in `.env` und werden nicht in Git gespeichert.

### InfluxDB aktivieren

1. In `.env` URL, Organisation, Bucket und Token eintragen und `INFLUXDB_ENABLED=true` setzen.
2. Den Dashboard-Dienst neu starten.
3. Im Dashboard bei **Raumklima** auf **+** klicken, **InfluxDB durchsuchen** wählen und die gewünschten Felder hinzufügen.

Die Auswahl wird lokal in `data/dashboard.db` gespeichert und verursacht deshalb keine Konflikte bei `git pull`. Beim ersten Start sind die Definitionen aus `config/dashboard.yaml` vorausgewählt und können im Auswahlfenster entfernt werden.

Für InfluxDB Cloud:

```env
INFLUXDB_ENABLED=true
INFLUXDB_URL=https://YOUR-INFLUXDB-CLOUD-HOST
INFLUXDB_ORG=YOUR_ORG
INFLUXDB_BUCKET=YOUR_BUCKET
INFLUXDB_TOKEN=HIER_DEN_LESETOKEN_EINTRAGEN
```

Den Token nur lokal auf dem Raspberry Pi eintragen und nicht committen. Beispiel für die Messwertzuordnung:

```yaml
influxdb:
  enabled: ${INFLUXDB_ENABLED:-false}

climate:
  range: "-24h"
  window: "15m"
  metrics:
    - id: temperature
      label: Seminarraum
      measurement: climate
      field: temperature
      tags: { room: WW14 }
      unit: "°C"
      decimals: 1
```

### Deadlines

Im Feld **Deadlines** auf **+** klicken und Titel, Fälligkeitszeit sowie Art eintragen. Im selben Fenster können vorhandene Einträge wieder gelöscht werden. Die Daten liegen dauerhaft in `data/dashboard.db`.

Beim ersten Start einer älteren Installation werden vorhandene Einträge aus `data/deadlines.json` einmalig übernommen. Danach wird diese Datei nicht mehr zur laufenden Pflege benötigt.

### Kalender

Im Feld **Wichtige Termine** auf **+** klicken. Titel und Beginn sind erforderlich; Ende und Ort sind optional. Vorhandene Termine lassen sich im selben Fenster löschen und werden dauerhaft in `data/dashboard.db` gespeichert.

Beim ersten Start einer älteren Installation werden vorhandene Einträge aus `data/events.json` einmalig übernommen.

### Aktien

Vonovia (`VNA.DE`) ist standardmäßig die erste Aktie. Über **+** im Aktienfeld kann nach einem Unternehmen oder Börsenkürzel gesucht werden. Ein Treffer wird per Klick hinzugefügt; ausgewählte Aktien lassen sich im selben Fenster wieder entfernen.

Die Yahoo-Schnittstelle ist nicht vertraglich garantiert. Für einen dauerhaften Produktivbetrieb sollte später ein offizieller Marktdatenanbieter mit API-Schlüssel ergänzt werden.

### Wetter und Regenradar

Die Vorhersage für Brandenburg an der Havel wird ohne API-Schlüssel von [Open-Meteo](https://open-meteo.com/) geladen. Sie enthält den Tagesverlauf in Drei-Stunden-Schritten. Ort, Koordinaten und Anzahl der Vorhersagetage stehen unter `weather` in `config/dashboard.yaml`.

Das Regenradar nutzt das amtliche RADVOR-Produkt des Deutschen Wetterdienstes und spielt die verfügbaren Vorhersagebilder automatisch ab. Der grüne Punkt markiert Brandenburg an der Havel; das Zeitfeld wechselt von **jetzt** bis ungefähr **+120 min**. Bei einem vorübergehenden DWD-Aussetzer bleibt die letzte erfolgreiche Animation bis zu sechs Stunden sichtbar; alle zwei Minuten wird ein neuer Abruf versucht. Damit die Karte auf dem Raspberry Pi erscheint, muss Chromium ausgehend auf `api.open-meteo.com`, `maps.dwd.de` und `tile.openstreetmap.org` zugreifen können.

## Docker-Alternative

```bash
cp .env.example .env
docker compose up -d --build
```

`compose.yaml` und das Docker-Image funktionieren auf x86-64 und ARM64.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

## Architektur

```text
Browser im Vollbildmodus
        │
        ▼
FastAPI-Dashboard ─── SQLite (To-do und lokale Auswahl)
        ├──────────── InfluxDB (Loxone)
        ├──────────── SQLite (Deadlines, Termine und To-dos)
        ├──────────── Open-Meteo (Wetter)
        └──────────── Marktdatenanbieter (Aktien)
```

Ein Agent auf dem Pi wäre nur nötig, wenn später unstrukturierte Inhalte automatisch gesucht, bewertet oder zusammengefasst werden sollen. Für Abfragen, Aktualisierung und Anzeige genügen deterministische Hintergrunddienste – sie sind zuverlässiger, sparsamer und leichter zu warten.
