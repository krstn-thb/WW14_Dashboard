# WW14 Dashboard

Vollbild-Dashboard für einen Raspberry Pi 5. Es bündelt Loxone-Klimadaten aus InfluxDB, Journal- und Konferenz-Deadlines, wichtige Kalendertermine, ausgewählte Aktienkurse und eine gemeinsame To-do-Liste.

Das Projekt ist bewusst schlank aufgebaut: Ein Python-Dienst liefert Daten und Oberfläche, Chromium zeigt sie im Vollbildmodus. Auf dem Raspberry Pi ist kein KI-Agent notwendig.

## Aktueller Funktionsumfang

- responsive Vollbild-Oberfläche für 16:9-Monitore, optimiert für Full HD und 4K
- selbstständige Aktualisierung; Standardintervall 60 Sekunden
- Loxone-/InfluxDB-Messwerte mit 24-Stunden-Verlauf
- Auswahl vorhandener InfluxDB-Messfelder direkt im Dashboard
- lokale Deadlines sowie optionale JSON-Feeds
- lokale Termine sowie optionale iCal-/ICS-Kalender
- Wettervorhersage für Brandenburg an der Havel
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

Beispiel:

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

Direkt in `data/deadlines.json` pflegen. Das Feld `due` ist ein ISO-Datum mit Uhrzeit und Zeitzone:

```json
{
  "title": "Paper für Konferenz XYZ",
  "due": "2027-02-12T23:59:00+01:00",
  "kind": "Konferenz",
  "url": "https://example.org/cfp"
}
```

Unter `deadlines.json_feeds` können zusätzlich URLs eingetragen werden, die dasselbe JSON-Format liefern.

### Kalender

Lokale Termine stehen in `data/events.json`. Für einen veröffentlichten iCal-Kalender in `config/dashboard.yaml` ergänzen:

```yaml
events:
  ical:
    - name: Hochschulkalender
      url: "https://example.org/calendar.ics"
```

Private Kalender-URLs sollten später ebenfalls über Umgebungsvariablen eingebunden werden, damit sie nicht im öffentlichen Repository landen.

### Aktien

Vonovia (`VNA.DE`) ist standardmäßig die erste Aktie. Über **+** im Aktienfeld kann nach einem Unternehmen oder Börsenkürzel gesucht werden. Ein Treffer wird per Klick hinzugefügt; ausgewählte Aktien lassen sich im selben Fenster wieder entfernen.

Die Yahoo-Schnittstelle ist nicht vertraglich garantiert. Für einen dauerhaften Produktivbetrieb sollte später ein offizieller Marktdatenanbieter mit API-Schlüssel ergänzt werden.

### Wetter

Die Vorhersage für Brandenburg an der Havel wird ohne API-Schlüssel von [Open-Meteo](https://open-meteo.com/) geladen. Ort, Koordinaten und Anzahl der Vorhersagetage stehen unter `weather` in `config/dashboard.yaml`.

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
        ├──────────── JSON / iCal (Deadlines und Termine)
        ├──────────── Open-Meteo (Wetter)
        └──────────── Marktdatenanbieter (Aktien)
```

Ein Agent auf dem Pi wäre nur nötig, wenn später unstrukturierte Inhalte automatisch gesucht, bewertet oder zusammengefasst werden sollen. Für Abfragen, Aktualisierung und Anzeige genügen deterministische Hintergrunddienste – sie sind zuverlässiger, sparsamer und leichter zu warten.
