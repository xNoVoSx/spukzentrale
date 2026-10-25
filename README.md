# 🎃 Spukzentrale

Halloween-Effektbeleuchtung mit Sound für ein Baugerüst-Netz. LED-Streifen unten an der Hauswand
strahlen auf ein dichtes weißes Gerüstnetz, Pappfiguren dazwischen werfen Schatten. Ein
unheimlicher Soundtrack läuft dauerhaft, zufällig kommen Donner, Hexengelächter und Schreie
dazu – jeweils mit passendem Licht, das genau auf dem Ton sitzt.

```
Handy ──► SERVER (Docker)  Weboberfläche, Szenen, Zufallsereignisse, Zeitplan,
            │               Ambiente-Stream, Home Assistant, MQTT
            │ WebSocket (JSON)                    │ HTTP (MP3-Stream, Effekt-Dateien)
            ▼                                     ▼
        ESP#1 LICHT                           ESP#2 SOUND
        5 PWM-Zonen, Effekte 100×/s           MP3-Stream + Effekte aus dem Flash
        läuft auch ohne Server weiter         Mixer → I2S-DAC → AUX der Box
            ▲            UART (Kabel)             │
            └─────────────────────────────────────┘
              Licht-Aktionen + Lautstärke-Hüllkurve, synchron zum Ton
```

## Inhalt

- [Funktionen](#funktionen)
- [Teileliste](#teileliste)
- [Verkabelung](#verkabelung)
- [Montage am Gerüst](#montage-am-gerüst)
- [Firmware flashen](#firmware-flashen)
- [Server in Portainer einrichten](#server-in-portainer-einrichten)
- [Bedienung](#bedienung)
- [Home Assistant](#home-assistant)
- [Fehlersuche](#fehlersuche)
- [Entwicklung](#entwicklung)

## Funktionen

- **Ambiente:** Playlist als Endlos-Stream mit Überblendung – keine Lücken beim Wiederholen.
- **Effekt-Sounds** (Donner, Schrei, Lachen, Herz, Wind, Kette, Sonstiges) liegen im Flash des
  Sound-ESP und starten sofort. Jeder Sound hat eine Licht-Aktion, z. B. *Blitze zum Sound*:
  jeder Knall im Donner wird ein Blitz.
- **Szenen** auf einer Zeitleiste: Gewitterfront, Schreck, Geisterwanderung, Herzschlag,
  Hexenauftritt – und eigene.
- **Zufallsereignisse** nach Regeln (z. B. „Donner alle 1–4 Minuten“), mit Mindestabstand.
- **Gruselstufen** 1 (Kinder) bis 3 (Schreck), auf Wunsch automatisch nach Uhrzeit.
- **Stimmungen:** 14 Licht-Effekte pro Zone (Kerze, Feuer, Geist wandert, kaputte Röhre …),
  als Favoriten speicherbar.
- **Zeitplan** ab Sonnenuntergang oder Uhrzeit, **Ruhezeit** (leiser oder nur Licht).
- **Klingel-Auslöser:** Erkennt die Türklingel (z. B. Reolink) eine Person, startet eine Szene.
- **Home Assistant** über MQTT-Discovery und REST-API.
- **Live-Vorschau** des Netzes in der Oberfläche – auch tagsüber einstellbar.
- Licht-ESP läuft mit dem letzten Zustand weiter, wenn der Server weg ist.

## Teileliste

| Teil | Anzahl | Hinweis |
|---|---|---|
| ESP32-Board (ESP32-WROOM-32, 4 MB Flash, z. B. diymore NodeMCU) | 2 | einer Licht, einer Sound |
| MOSFET-Modul mit Optokoppler (LR7843, PWM-Eingang) | 5 | je Zone eins |
| 12-V-LED-Streifen, einfarbig (z. B. COB) | 10 m | in 5 Zonen à ca. 1,6 m geschnitten |
| Netzteil 12 V, 200 W | 1 | Reserve: 10 m COB ≈ 80–150 W |
| I2S-DAC PCM5102A (Modul mit 3,5-mm-Buchse) | 1 | Ausgang an den AUX der Box |
| Lautsprecher/Box mit AUX-Eingang | 1 | |
| USB-Netzteile für die ESPs | 2 | oder Step-Down 12 → 5 V |
| Flachsicherung 15 A + Halter | 1 | direkt hinter dem Netzteil |
| Flachsicherung 3 A + Halter | 5 | optional, je Zone |
| Litze 2 × 1,5 mm² | ca. 25 m | Zuleitungen zu den Zonen |
| Dupont-Kabel, Klinkenkabel | | |
| Gehäuse IP65 mit Kabelverschraubungen | 1 | für Elektronik und MOSFETs |

## Verkabelung

```
 230 V ──► NETZTEIL 12 V / 200 W
             V+ ──[Sicherung 15 A]──► DC+ von MOSFET 1, 2, 3, 4, 5   (alle Module parallel)
             V− ────────────────────► DC− von MOSFET 1, 2, 3, 4, 5

             ┌────────┐┌────────┐┌────────┐┌────────┐┌────────┐
             │MOSFET 1││MOSFET 2││MOSFET 3││MOSFET 4││MOSFET 5│
             └────────┘└────────┘└────────┘└────────┘└────────┘
              OUT+ OUT−   …   (2 × 1,5 mm², optional 3-A-Sicherung in OUT+)
                │   │
                ▼   ▼
          LED-Zone 1 (+ / −)   …   LED-Zone 5

   Signalseite der Module:      PWM GND   PWM GND   PWM GND   PWM GND   PWM GND
                                 │   │     │   │     │   │     │   │     │   │
 USB ──► ┌───────────────┐       │   │     │   │     │   │     │   │     │   │
         │  ESP#1 LICHT  │ GPIO18┘   │     │   │     │   │     │   │     │   │
         │               │ GPIO19────┼─────┘   │     │   │     │   │     │   │
         │               │ GPIO21────┼─────────┼─────┘   │     │   │     │   │
         │               │ GPIO22────┼─────────┼─────────┼─────┘   │     │   │
         │               │ GPIO23────┼─────────┼─────────┼─────────┼─────┘   │
         │               │ GND ──────┴─────────┴─────────┴─────────┴─────────┘
         │               │
         │   GPIO17 (TX) ├──────────────► GPIO16 (RX) ┐
         │   GPIO16 (RX) ├◄────────────── GPIO17 (TX) │
         │           GND ├─────────────── GND         │
         └───────────────┘                ┌───────────┴───┐ ◄── USB
                                          │  ESP#2 SOUND  │
                                          │          3V3 ─┼──► VIN  ┌──────────┐
                                          │          GND ─┼──► GND  │ PCM5102A │
                                          │       GPIO26 ─┼──► BCK  │          ├── 3,5 mm ──► AUX Box
                                          │       GPIO25 ─┼──► LCK  │          │
                                          │       GPIO22 ─┼──► DIN  │          │
                                          └───────────────┘   SCK ─► GND
```

- **Gemeinsame Masse:** Die beiden ESPs müssen mit einer GND-Leitung verbunden sein, sonst
  funktioniert die UART-Verbindung nicht. Die 12-V-Seite ist durch die Optokoppler getrennt
  und muss nicht mit der ESP-Masse verbunden werden.
- **PCM5102A-Lötbrücken** (Rückseite): FLT, DEMP, FMT auf **L**, XSMT auf **H**. Bei vielen
  Modulen ist das ab Werk so. SCK an GND (der DAC erzeugt den Takt dann selbst).
- **Pins meiden:** GPIO 0, 2, 12, 15 (Boot) und 34–39 (nur Eingang).
- Eine 6. Zone ist möglich (z. B. GPIO 25 am Licht-ESP), dann `ZONEN_PINS` in `config.h` und
  die Zonenzahl in den Einstellungen anpassen.
- Brummt es über AUX (Masseschleife), hilft ein Entstörfilter für Klinke.

## Montage am Gerüst

- Die LED-Zonen hängen **unten an der Wand** und strahlen schräg nach oben **auf das Netz**.
  Zonen gleichmäßig über die Breite verteilen; Zone 1 links, Zone 5 rechts (von der Straße
  aus gesehen) – dann laufen Welle und Lauflicht in die richtige Richtung.
- **Pappfiguren** 10–20 cm hinter dem Netz aufhängen – je näher am Netz, desto schärfer der Schatten.
- Den Steuerkasten möglichst **in die Mitte** setzen, dann bleiben die Zuleitungen kurz
  (≤ 5 m bei 1,5 mm² → kaum Spannungsabfall).
- Nicht wetterfeste Streifen (IP20) vor Regen schützen (Alu-Profil mit Abdeckung oder
  Silikonschlauch). Netzteil ins Trockene.
- **Farbe:** Einfarbige Streifen lassen sich mit Farbfolie (Bühnen-Lichtfilter) einfärben.
  In den Einstellungen bekommt jede Zone dann eine Farbe für die Live-Vorschau.

## Firmware flashen

Voraussetzung: [PlatformIO](https://platformio.org/install/cli) (z. B. `pipx install platformio`).

```bash
git clone https://github.com/xNoVoSx/spukzentrale.git
cd spukzentrale/firmware

# Konfiguration anlegen und anpassen (WLAN, IP des Servers)
cp licht/include/config.example.h licht/include/config.h
cp sound/include/config.example.h sound/include/config.h
nano licht/include/config.h
nano sound/include/config.h

# Licht-ESP per USB anschließen und flashen
pio run -d licht -t upload
pio device monitor -d licht        # zeigt IP-Adresse und Status

# Sound-ESP per USB anschließen und flashen
pio run -d sound -t upload
pio device monitor -d sound
```

Updates für den Licht-ESP gehen später auch per WLAN:

```bash
SPUK_LICHT_IP=<IP des Licht-ESP> SPUK_OTA_PASSWORT=<OTA_PASSWORT aus config.h> pio run -d licht -e licht_ota -t upload
```

(In der fish-Shell: `env SPUK_LICHT_IP=… SPUK_OTA_PASSWORT=… pio run …`)

Die Effekt-Sounds lädt der Sound-ESP selbst vom Server – dafür ist kein extra Upload nötig.

Die Treiber für den USB-Seriell-Chip CH340 sind unter Linux im Kernel enthalten.

## Server in Portainer einrichten

Das Docker-Image baut GitHub Actions bei jedem Push und legt es unter
`ghcr.io/xnovosx/spukzentrale` ab.

1. **Ordner auf dem Docker-Host anlegen**, z. B.
   ```bash
   sudo mkdir -p /docker/spukzentrale
   ```
   Für die Medien kann auch eine NAS-Freigabe genutzt werden (muss auf dem Docker-Host
   eingebunden und beschreibbar sein).
2. **Portainer → Stacks → Add stack → Repository**
   - Repository URL: `https://github.com/xNoVoSx/spukzentrale`
   - Repository reference: `refs/heads/main`
   - Compose path: `docker-compose.yml`
   - optional *GitOps updates* mit *Re-pull image* aktivieren
3. **Environment variables** (alle optional):

   | Variable | Bedeutung | Standard |
   |---|---|---|
   | `SPUK_CONFIG_PFAD` | Einstellungen, Szenen, Playlist | `/docker/spukzentrale` |
   | `SPUK_MEDIEN_PFAD` | Musik und Effekt-Sounds | `/docker/spukzentrale/medien` |
   | `SPUK_PORT` | Port der Weboberfläche | `8095` |
   | `SPUK_TAG` | Image-Version (`latest` oder z. B. `1.0.0`) | `latest` |
   | `TZ` | Zeitzone | `Europe/Berlin` |

4. **Deploy the stack**, dann `http://<IP des Docker-Hosts>:8095` im Handy öffnen. Tipp: „Zum
   Startbildschirm hinzufügen“ – dann startet die Oberfläche wie eine App.

Die Weboberfläche hat keine Anmeldung – sie gehört ins Heimnetz, nicht ins Internet.

## Bedienung

| Seite | Was |
|---|---|
| 🎃 **Show** | Live-Vorschau, Show an/aus, Ambiente, Zufallsereignisse, Gruselstufe, Helligkeit, Lautstärke, Sounds/Licht/Szenen auslösen, Stimmungen, Protokoll |
| 💡 **Licht** | Effekt, Tempo, Stärke und Grundlicht je Zone, „auf alle“, Zonen-Test, Stimmungen speichern |
| 🔊 **Sounds** | Ambiente-Playlist (Reihenfolge, Zufall, Überblendung), Effekt-Sounds hochladen, Licht je Sound einstellen, Speicherbelegung |
| 🎬 **Szenen** | Szenen-Editor (Zeitleiste) und Regeln für die Zufallsereignisse |
| ⚙️ **Einstellungen** | Zeitplan, Ruhezeit, Gruselstufe nach Uhrzeit, Klingel, Home Assistant, MQTT, Standort, Ton, Licht, Geräte |

**Ersteinrichtung**

1. *Einstellungen → Licht → Zonen-Test*: Prüfen, ob Zone 1–5 an der richtigen Stelle hängen.
2. *Sounds*: Ambiente-Musik und Effekte hochladen (jedes gängige Format). Kostenlose Sounds:
   [freesound.org](https://freesound.org) (Filter: CC0), [pixabay.com/sound-effects](https://pixabay.com/sound-effects/).
3. *Einstellungen → Ton → Ton-Licht-Abgleich*: draußen „Klick + Blitz“ drücken und die Latenz
   so einstellen, dass Klick und Blitz zusammenfallen (mit DAC meist 40–80 ms).
4. *Einstellungen → Standort* für den Sonnenuntergang setzen, *Zeitplan* aktivieren.

**Sicherheit:** Das Stroboskop ist standardmäßig auf 3 Hz begrenzt. Schnellere Blitzfolgen
können bei Menschen mit Fotosensibilität Anfälle auslösen.

## Home Assistant

### Klingel / Bewegung als Auslöser

1. In Home Assistant ein Langzeit-Zugriffstoken anlegen (*Profil → Sicherheit*).
2. *Einstellungen → Home Assistant*: Adresse (`http://<IP>:8123`) und Token eintragen.
3. *Einstellungen → Klingel / Bewegung*: „Sensoren laden“, den Personen-Sensor wählen
   (Reolink: `binary_sensor.<name>_person`), Szene, Sperrzeit und Gruselstufe einstellen.

Die Spukzentrale beobachtet den Sensor über die WebSocket-API – in Home Assistant ist keine
Automation nötig. Abschalten: Haken bei „Auslöser aktiv“ entfernen.

### MQTT

*Einstellungen → MQTT*: Broker eintragen und aktivieren. In Home Assistant erscheint
automatisch das Gerät **Spukzentrale** mit Schaltern (Show, Ambiente, Zufallsereignisse),
Helligkeit, Lautstärke, Gruselstufe, Stimmung, einem Knopf pro Szene und dem Gerätestatus.

### REST-API

Alle Funktionen sind per HTTP erreichbar, Dokumentation unter `http://<server>:8095/docs`.
Beispiele:

```yaml
# configuration.yaml
rest_command:
  spuk_schreck:
    url: http://<server>:8095/api/szenen/schreck/start
    method: POST
  spuk_ausloeser:   # wie die Klingel: mit Sperrzeit und Gruselstufe
    url: http://<server>:8095/api/ausloeser
    method: POST
```

## Fehlersuche

| Problem | Ursache / Lösung |
|---|---|
| Punkt „Licht“/„Sound“ oben ist rot | ESP nicht verbunden. Seriellen Monitor öffnen (`pio device monitor -d licht`): WLAN-Daten und `SERVER_HOST`/`SERVER_PORT` in `config.h` prüfen. |
| Keine Live-Vorschau | Licht-ESP offline (siehe oben). |
| „kein Signal vom Sound-ESP“ bei *Geräte* | UART-Kabel prüfen: TX ↔ RX gekreuzt? GND verbunden? |
| Licht kommt vor/nach dem Ton | *Einstellungen → Ton → Latenz* mit dem Ton-Licht-Abgleich einstellen. |
| Kein Ton | DAC-Verkabelung und Lötbrücken (XSMT auf H!) prüfen, AUX-Eingang an der Box wählen, *Geräte*: läuft der Stream, wie voll ist der Puffer? |
| Ton stottert, „Aussetzer“ zählen hoch | WLAN-Empfang am Sound-ESP zu schwach (RSSI unter −75 dBm) – näher an den Access Point oder externe Antenne. |
| Zone flackert dunkel / leuchtet nicht ganz aus | Optokoppler-Modul mag die PWM nicht: `PWM_FREQUENZ` in `config.h` senken (z. B. 500). Zuleitung zu dünn? |
| Ganze Zone falsch herum (an = aus) | `PWM_INVERTIERT 1` in `config.h`. |
| Effekt-Upload: „Speicher voll“ | Der Sound-ESP hat ca. 2,2 MB ≈ 3 Minuten Effekte. Lange Sounds kürzen oder löschen. |
| Klingel löst nicht aus | *Einstellungen → Klingel*: Status „verbunden“? Sperrzeit, Gruselstufe und „Nur wenn die Show läuft“ beachten. Server-Log in Portainer ansehen. |
| Server-Log | Portainer → Container `spukzentrale` → Logs. Mehr Details: Umgebungsvariable `SPUK_LOGLEVEL=DEBUG`. |

## Entwicklung

```bash
# Server lokal starten (braucht ffmpeg)
cd server
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
SPUK_CONFIG=./data/config SPUK_MEDIEN=./data/medien uvicorn app.main:app --port 8095 --reload

# Simulierte ESPs verbinden (Licht + Sound, ohne Hardware)
python werkzeuge/sim_geraete.py --server http://localhost:8095

# Tests
python -m pytest -q
bash ../firmware/licht/test/effekte_test.sh
bash ../firmware/sound/test/adpcm_test.sh
bash ../firmware/sound/test/chunked_test.sh
```

### Aufbau

| Pfad | Inhalt |
|---|---|
| `firmware/common/protokoll.h` | UART-Protokoll zwischen den ESPs (`A5 5A typ len payload xor`) |
| `firmware/licht/` | Licht-ESP: Effekt-Engine (`effekte.cpp`), WLAN, WebSocket, PWM, OTA |
| `firmware/sound/` | Sound-ESP: Audio-Pipeline (`audio.cpp`), ADPCM-Dekoder, HTTP-Chunk-Dekoder |
| `server/app/` | FastAPI-Server: `zentrale.py` (Zustand), `ambiente.py` (Stream), `ablauf.py` (Szenen), `ereignisse.py`, `zeitplan.py`, `homeassistant.py`, `mqtt.py`, `licht.py` (Licht-Ausgang) |
| `server/static/` | Weboberfläche (Vanilla-JS) |

### WebSocket-Protokoll (Server ↔ ESPs)

Die ESPs verbinden sich mit `/ws/geraet?rolle=licht|sound&fw=<version>` und schicken
`{"t":"hallo",…}`, danach alle 2 s `{"t":"status",…}`.

| Richtung | Nachricht |
|---|---|
| Server → Licht | `config` {strobo_max_hz, gamma, zone_max[]}, `zustand` {an, hell, musik_reaktiv, zonen[{e, tempo, staerke, basis}], temp?}, `aktion` {name, zonen[], verz_ms, dauer_ms, staerke, param}, `stopp`, `vorschau` {an}, `neustart` |
| Licht → Server | `hallo`, `status`, `werte` {v[]} (10×/s, solange die Oberfläche offen ist) |
| Server → Sound | `config` {master, musik_vol, fx_vol, duck, latenz_ms}, `musik` {aktion: play\|stop, pfad}, `fx` {id, vol, licht?}, `fx_stop`, `fx_sync` {basis, liste[{id, groesse}]}, `neustart` |
| Sound → Server | `hallo`, `status`, `fx_fehlt` {id}, `fx_sync_fertig` {geladen, geloescht, fehler, meldung} |

Hat ein Effekt ein `licht`-Feld, schickt der Sound-ESP die Aktion im selben Moment, in dem
der Ton startet, per UART an den Licht-ESP (verzögert um die Ausgabe-Latenz). Ohne
UART-Verbindung schickt der Server die Aktion selbst.

## Lizenz

GPL-3.0 (wegen der verwendeten Bibliothek [arduino-libhelix](https://github.com/pschatzmann/arduino-libhelix)).
Schrift „Creepster“: SIL Open Font License, siehe `server/static/schrift/OFL.txt`.
