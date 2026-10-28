# spukpi – die Spukzentrale auf dem Raspberry Pi

Ab 1.3 kann ein **Raspberry Pi 3 B+** den Sound-ESP ersetzen. Er spielt Ambiente und Effekte über den PCM5102A
und steuert den Licht-ESP per **USB** statt über WLAN. Der Server bleibt derselbe: Der Pi meldet sich dort wie
bisher als „sound“ und „licht“.

```
Server ──WLAN──► Pi ──I2S──► PCM5102A ──AUX──► Box
                  └──USB──► Licht-ESP ──PWM──► MOSFETs ──► LED-Zonen
```

## Warum der Pi

| | Sound-ESP | Pi |
|---|---|---|
| WLAN | diymore-Board sendet schwach (−93 dBm, 22–33 % Wiederholungen) | −60 dBm, 5 GHz, 0 % Wiederholungen |
| Stream-Vorrat | ~2 s | **15 s** (einstellbar), der Server läuft dafür voraus |
| Stream reißt ab | Aussetzer | **Überblendung auf den Mitschnitt** der letzten Minuten, zurück, sobald der Stream wieder da ist |
| Server weg | Stille | **Notbetrieb:** Ambiente aus dem Mitschnitt, nach 60 s Zufallseffekte mit dem zuletzt eingestellten Licht |
| Effekte | 4 Stimmen, Flash ~2 MB | 8 Stimmen, SD-Karte (GB) |
| Ton-Licht-Abgleich | Latenz von Hand einstellen | **gemessen** (Füllstand des ALSA-Puffers), nur ein Extra für Bluetooth einstellen |
| Licht zur Musik | Lautstärke-Hüllkurve | dazu **Spektrum** (Bass/Mitte/Höhen) und **Beat-Erkennung** für die Effekte „Spektrum“ und „Beat-Blitz“ |
| Licht-ESP | eigenes WLAN, OTA | WLAN aus, alles über USB; **flashen über den Pi** |
| Diagnose | Puffer, Aussetzer | dazu Quelle (Stream/Ersatz), Vorrat in s, Temperatur, Unterspannung, SD frei |

## Verdrahtung PCM5102A → Pi

Gesamtplan mit der GPIO-Leiste so, wie der Pi im Kasten liegt: [`docs/schaltplan.svg`](../docs/schaltplan.svg).
GND geht am kürzesten an **Pin 39** (direkt neben 35 und 40); Pin 6 ist gleichwertig.

| PCM5102A | Pi 3 B+ (Stiftleiste) |
|---|---|
| VIN | Pin 2 (5 V) |
| GND | Pin 39 (GND) – oder Pin 6 |
| BCK | Pin 12 (GPIO 18) |
| LCK | Pin 35 (GPIO 19) |
| DIN | Pin 40 (GPIO 21) |
| SCK | an GND (bzw. die Lötbrücke neben „SCK“ schließen) – der PCM5102A erzeugt den Takt dann selbst |

Lötbrücken auf der Rückseite: **H1L FLT = L, H2L DEMP = L, H3L XSMT = H** (sonst bleibt er stumm), **H4L FMT = L**.

Der Licht-ESP hängt mit einem **Datenkabel** am USB des Pi (CH340 → `/dev/ttyUSB0`). Er bekommt darüber auch seinen
Strom. Die UART-Kabel zum alten Sound-ESP braucht es nicht mehr.

## Einrichten

Raspberry Pi OS Lite 64 bit (Trixie), SSH an, WLAN eingerichtet. Dann auf dem Pi:

```bash
git clone https://github.com/xNoVoSx/spukzentrale.git
bash spukzentrale/pi/system/installieren.sh
sudo nano /etc/spukpi/config.toml     # server_host eintragen, sound/licht einschalten
sudo reboot                           # nur beim ersten Mal (I2S-Overlay)
```

Das Skript installiert die Pakete und legt `/opt/spukpi` an (mit `esptool` von PyPI, Debians Paket hat keinen
Flasher-Stub). Außerdem trägt es das Overlay `hifiberry-dac` ein und richtet den Dienst `spukpi` ein. Ein Update
ist dasselbe: `git pull`, dann das Skript erneut aufrufen.

Alle Einstellungen stehen in [`system/config.example.toml`](system/config.example.toml).

## Umschalten vom Sound-ESP auf den Pi

1. Den Sound-ESP vom Strom nehmen. Er und der Pi dürfen sich nicht gleichzeitig als „sound“ melden.
2. In `/etc/spukpi/config.toml` `sound = true` und `licht = true` setzen, dann `sudo systemctl restart spukpi`.
3. In der Oberfläche unter **Einstellungen → Geräte** erscheinen „Sound (Raspberry Pi)“ und
   „Licht-ESP (USB am Pi)“.
4. Unter **Ton** die Latenz auf **0 ms** stellen. Der Pi misst seine eigene Verzögerung. Nur für eine
   Bluetooth-Box trägt man dort das Extra ein und gleicht mit „Klick + Blitz“ ab.

Zurück geht es mit `sound = false` und `licht = false`. Der Licht-ESP schaltet nach 5 s ohne Pi selbst wieder ins
WLAN, mit Firmware 1.3 und derselben `config.h`.

## Licht-ESP flashen (über den Pi)

```bash
# am PC: Firmware bauen und rüberkopieren
cd firmware/licht && pio run
scp .pio/build/licht/firmware.bin spuk-pi:
# auf dem Pi
sudo PYTHONPATH=/opt/spukpi/src /opt/spukpi/venv/bin/python -m spukpi.flash ~/firmware.bin
```

Der Dienst gibt den USB-Port dafür kurz frei. Der gespeicherte Lichtzustand bleibt erhalten.

## Bluetooth zur Box (Option)

```bash
sudo apt install bluez-alsa-utils
bluetoothctl   # scan on / pair AA:BB:… / trust AA:BB:… / connect AA:BB:…
```

In der `config.toml` dann `audio_geraet = "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp"` eintragen und unter
**Ton** die Latenz der Box mit „Klick + Blitz“ einstellen (typisch 150–250 ms). AUX ist zuverlässiger, Bluetooth ist
nur für den Notfall gedacht.

## Fehlersuche

```bash
journalctl -u spukpi -f                     # Log (auch das Log des Licht-ESP)
systemctl status spukpi
aplay -l                                    # muss „sndrpihifiberry“ zeigen
vcgencmd get_throttled                      # 0x0 = Strom ok; 0x50000 = Unterspannung seit dem Start
```

- **Kein Ton, `alsa: false` in der Oberfläche:** Overlay fehlt (`/boot/firmware/config.txt`) oder es gab noch keinen
  Neustart.
- **Ton da, aber leise oder verzerrt:** Lötbrücken prüfen (XSMT = H), SCK an GND.
- **Licht-ESP „kein Signal vom Pi“:** USB-Kabel ist ein reines Ladekabel → `lsusb` zeigt dann keinen CH340.
- **Unterspannung:** Der Pi 3 B+ braucht 5,1 V / 2,5 A am Micro-USB. Ein dünnes Kabel reicht nicht, der Licht-ESP
  hängt zusätzlich mit dran.

## Aufbau des Programms

| Datei | Aufgabe |
|---|---|
| `spukpi/__main__.py` | Dienst: Server-Rollen, Brücke, Notbetrieb, Status |
| `spukpi/mischer.py` | 10-ms-Takt: Ambiente + Effekte → ALSA, Ducking, Limiter, Licht-Rahmen um die gemessene Latenz verzögert |
| `spukpi/ambiente.py` | Stream mit Vorrat, Mitschnitt auf der SD, Überblendung auf den Ersatz |
| `spukpi/analyse.py` | Bänder + Beat-Erkennung (FFT, 50 Hz) |
| `spukpi/effekte.py` | Effekt-Abgleich mit dem Server, dekodierte Klänge (.npy), Merkliste fürs Notprogramm |
| `spukpi/licht.py` | USB-Brücke zum Licht-ESP (115200 Baud, mehr verliert der Pi 3 Pakete des CH340) |
| `spukpi/protokoll.py` | Rahmen/JSON-Zeilen wie `firmware/common/protokoll.h` |
| `spukpi/flash.py` | Licht-ESP flashen |

Tests ohne Hardware: `cd pi && python -m pytest -q`.
