# Changelog

Alle nennenswerten Änderungen. Versionierung nach [SemVer](https://semver.org/lang/de/);
Firmware und Server tragen dieselbe Versionsnummer.

## [1.3.1] – 2026-10-28

- **Licht-Effekt „Musik“ mit Pi neu:** Grundglühen aus der Energie aller Bänder (je Zone leicht verschieden), jeder
  erkannte Schlag läuft als **Welle von der Mitte nach außen** über das Netz, Höhen lassen einzelne Zonen funkeln.
  Tempo = Laufzeit/Abklingen der Welle. Ohne Pi bleibt das bisherige Verhalten.
- **Live-Vorschau flüssig:** Licht-ESP schickt über USB 25 statt 10 Werte/s, die Oberfläche blendet mit der Bildrate
  weich nach. Die USB-Brücke im Pi reicht Daten sofort weiter (vorher in 0,2-s-Päckchen).
- Verdrahtungsplan (`docs/schaltplan.svg`) für den Pi-Aufbau.

## [1.3.0] – 2026-10-28

### Neu: Raspberry Pi statt Sound-ESP ([`pi/`](pi/README.md))
- Dienst **spukpi** auf einem Pi 3 B+: meldet sich als „sound“ (Ton über PCM5102A an I2S) und als „licht“
  (USB-Brücke zum Licht-ESP). Der Server bleibt kompatibel, beide Rollen sind einzeln schaltbar.
- **15 s Stream-Vorrat** (der Server läuft für Zuhörer mit `puffer_s` entsprechend voraus) und **lückenloser
  Ersatz**: Reißt der Stream ab, blendet der Pi auf einen Mitschnitt der letzten Minuten über und zurück, sobald der
  Stream wieder genug Vorrat hat. Getestet: Server hart neu gestartet bzw. 2,5 min weg, 0 Aussetzer.
- **Notbetrieb:** Ohne Server läuft das Ambiente aus dem Mitschnitt weiter, nach 60 s kommen Zufallseffekte mit dem
  zuletzt eingestellten Licht.
- **Latenz gemessen** (Füllstand des ALSA-Puffers): Ton und Licht sind ohne Einmessen synchron. `latenz_ms` ist beim
  Pi nur noch das Extra, z. B. für Bluetooth.
- **Musik-Analyse** (FFT, 50 Hz): Pegel in Bass, Mitte und Höhen sowie Beat-Erkennung, per USB an den Licht-ESP.
- 8 Effekt-Stimmen, Effekte auf der SD-Karte. Pi-Gesundheit (Temperatur, Unterspannung, SD) in der Oberfläche.
- Licht-ESP **über den Pi flashen** (`python -m spukpi.flash`).

### Licht-Firmware 1.3.0
- **USB-Modus:** Spricht der Pi über USB, schaltet der ESP sein WLAN ab und läuft komplett über das Kabel
  (JSON-Zeilen + Rahmen). Schweigt der Pi 5 s lang, geht er zurück ins WLAN wie bisher.
- Neue Effekte **„Spektrum“** (Zonen folgen Bass → Höhen) und **„Beat-Blitz“** (Blitz auf jeden Schlag, Tempo =
  Abklingzeit). Ohne Pi folgen sie der normalen Hüllkurve.
- MAC kommt aus dem eFuse (stimmt auch bei abgeschaltetem WLAN).

### Server
- `/stream/ambiente?puffer_s=N`: bis 20 s Rückblick, Encoder-Vorlauf folgt dem größten Wunsch. Der angezeigte Titel
  wechselt erst, wenn er zu hören ist.
- Oberfläche: Geräteanzeige für Pi und USB-Licht (Quelle, Vorrat, überbrückte Lücken, gemessene Latenz,
  Temperatur, Unterspannung), Hinweis zur Latenz beim Pi.

## [1.2.3] – 2026-10-27 (nur Sound-Firmware)

### Behoben
- Seltene kurze Aussetzer und ein Watchdog-Neustart beim Effekt-Abgleich: Der Status fragte alle 2 s den
  freien Effekt-Speicher ab; LittleFS liest dafür das ganze Dateisystem (bis 6,8 s), währenddessen stockte
  der MP3-Decoder. Jetzt nur beim Start und nach dem Abgleich gemessen (gemessen: 5 Status-Hänger über 1 s
  = 5 Aussetzer in 15 min).

### Neu
- Jeder Aussetzer landet mit Stream-Füllstand im Fern-Log.

## [1.2.2] – 2026-10-27 (nur Server)

### Behoben
- „Ambiente an“, wenn es schon läuft (Hand, Zeitplan, Home Assistant), startete den Stream neu – kurze
  hörbare Lücke. Jetzt wird nur bei einer echten Änderung geschaltet.
- Versionshinweis unter „Geräte“ nur noch bei anderer Haupt-/Unterversion (Firmware 1.2.1 passt zu Server 1.2.2).

## [1.2.1] – 2026-10-27

### Geändert
- **Sound-ESP wieder ohne OTA** (Updates per USB) und mit der Flash-Aufteilung von 1.1.0 (2,25 MB für
  Effekte). Mit OTA hing der Sound-ESP nach abgebrochenen Updates, und die Effekt-Downloads scheiterten;
  das WLAN der diymore-Boards ist in der Senderichtung sehr schwach (UniFi: −93 dBm, „Signal Balance:
  Poor“). Effekt-Download wieder mit HTTPClient wie in 1.1.0, mit genauer Fehlermeldung.
- Licht-ESP: mDNS aus (OTA per IP bleibt).
- Beide: Hostname „spuk-licht“/„spuk-sound“ wird jetzt wirklich gesetzt (vorher „esp32-…“).

### Neu
- Fern-Log: Der Sound-ESP meldet Stream-Abbrüche, Datenpausen, Download-Fehler und Hänger der
  Hauptschleife (mit Abschnitt) ins Protokoll der Oberfläche.

## [1.2.0] – 2026-10-27

### Behoben
- **Sound-ESP: Aussetzer und Krachen im Ambiente.** Der Decoder durfte erst nachlegen, wenn nur noch
  ~20 ms Musik im Puffer waren, und wurde auf Kern 0 von WLAN/TCP verdrängt (gemessen: 41 Aussetzer/min
  bei vollem Stream-Puffer). Jetzt: Decoder auf Kern 1, PCM-Puffer 16 KB, Nachlegen in 10-ms-Stücken,
  bei Unterlauf weich aus- und wieder einblenden statt Knacken.
- **Licht-ESP: Licht friert ein / „kein Signal vom Licht-ESP“.** Bei schwachem WLAN hingen
  Sendevorgänge die Hauptschleife auf, samt Effekten und Lebenszeichen. Effekte, PWM und UART laufen jetzt
  in einer eigenen Task, unabhängig vom Netz; WLAN-Stromsparen aus.

### Neu
- Sound-ESP: Updates über WLAN (OTA). **Einmalig per USB flashen** – die Flash-Aufteilung ändert sich
  (Effekt-Speicher 1,25 MB statt 2,25 MB, wird danach automatisch vom Server neu befüllt).
  In `include/config.h` muss `#define OTA_PASSWORT "…"` stehen.
- Diagnose beider ESPs: Neustart-Grund (Absturz, Watchdog, Unterspannung …), WLAN-Trennungen mit Grund,
  längster Hänger der Hauptschleife; ungewöhnliche Neustarts landen im Protokoll.

## [1.1.0] – 2026-10-25

### Neu
- **Auslöser-Liste:** beliebig viele Sensoren aus Home Assistant, je mit eigener Szene oder
  Sound-Kategorie, Sperrzeit, Gruselstufe und Test-Knopf. Voreingestellt: „Klingel gedrückt“ →
  Schreck, „Person erkannt“ → Kettensäge. Die Einstellung aus 1.0 wird übernommen.
- Sound-Kategorien **Kettensäge** und **Stimme**, neue Szenen „Kettensäge“ und „Begrüßung“
  (werden bei bestehenden Installationen ergänzt, gelöschte Szenen kommen nicht zurück)
- **Aufbau & Montageplan:** Streifen, Abstände, Kasten-Position, Zonen-Zuordnung einstellbar;
  die Oberfläche berechnet Kabellängen, Strom, Spannungsabfall und Netzteil-Auslastung
- Live-Vorschau zeigt die senkrechten Streifen an ihren echten Positionen
- Verdrahtungsplan als Grafik (`docs/schaltplan.svg`) passend zu den LR7843-Modulen
  (`+`, `LOAD`, `−`)
- MQTT: ein Knopf pro Auslöser

### Geändert
- REST: `POST /api/ausloeser` → `POST /api/ausloeser/<id>` (`?test=true` ohne Sperrzeit)

## [1.0.0] – 2026-10-25

Erste Version für Halloween.

### Server
- Weboberfläche (mobil zuerst) mit Live-Vorschau des Netzes, Halloween-Optik
- Ambiente-Endlos-Stream: Playlist mit Überblendung, ohne Lücken beim Wiederholen
- Effekt-Sounds hochladen (jedes ffmpeg-Format) → IMA-ADPCM für den Sound-ESP, Stille am
  Anfang wird abgeschnitten, Vorhören im Browser
- Jeder Sound hat eine passende Licht-Aktion (Blitze folgen dem Donner, Schreck zum Schrei …)
- Szenen auf einer Zeitleiste (Sound, Licht, Stimmung), 5 eingebaute Szenen
- Zufallsereignisse mit Regeln je Kategorie/Szene, Mindestabstand, Gruselstufen 1–3
- Stimmungen (Grundbeleuchtung) speichern und abrufen
- Zeitplan (Uhrzeit oder Sonnenuntergang), Ruhezeit (leiser/stumm), Gruselstufe nach Uhrzeit
- Home Assistant: Klingel/Bewegungssensor löst eine Szene aus (WebSocket-API, mit Sperrzeit)
- MQTT mit Home-Assistant-Discovery, REST-API
- Licht-Ausgang als austauschbare Schicht (Vorbereitung für WLED)

### Firmware Licht-ESP
- 5–6 PWM-Zonen, Effekt-Engine mit 100 Bildern/s, 14 Effekte, 10 Aktionen
- Strobo-Sicherheitsgrenze (Standard 3 Hz), Gamma, Zonen-Maximum
- letzter Zustand im Flash (läuft ohne Server weiter), Updates per WLAN (OTA)
- Licht-Aktionen und Hüllkurve vom Sound-ESP per UART (synchron zum Ton)

### Firmware Sound-ESP
- Ausgabe über I2S-DAC PCM5102A an den AUX-Eingang der Box
- MP3-Stream (Helix) mit Vorpuffer, bis zu 4 Effekt-Stimmen aus LittleFS, Mixer mit
  Ducking und weichem Limiter
- Effekt-Dateien werden automatisch mit dem Server abgeglichen
