# KI-Stimme

Spricht Besucher passend zu dem an, was eine Kamera sieht („Hey du da, kleines Skelett – komm näher!“).
Läuft auf einem PC mit NVIDIA-GPU im LAN (getestet: RTX 3090, ~3–4 s vom Auslöser bis zur Stimme).

```
Home Assistant (Personensensor) ─► Server ─► Standbild (HA camera_proxy)
                                     ├──► llama-server + Gemma 3 12B (Bild → Satz)       Port 8091
                                     ├──► stimme.py + Chatterbox (Satz → Gruselstimme)   Port 8092
                                     └──► Pi lädt die WAV und spielt sie mit Licht
```

Das Standbild bleibt nur im Speicher des Servers. Fällt etwas aus (PC aus, zu langsam), spielt der Auslöser
einen Sound aus der Ersatz-Kategorie – die Show läuft ohne KI weiter wie vorher. Der Schalter „KI-Stimme“
(Startseite bzw. MQTT/Home Assistant) schaltet die KI ganz ab, die Kamera-Auslöser spielen dann nur Ersatz-Sounds.

## Einrichten (Arch/CachyOS)

1. Sprachmodell: `sudo pacman -S llama-cpp ggml-cuda` (CachyOS; sonst AUR `llama.cpp-cuda`).
2. Sprach-Dienst:
   ```
   uv venv --python 3.11 .venv-tts
   VIRTUAL_ENV=.venv-tts uv pip install chatterbox-tts "setuptools<81" fastapi uvicorn
   ```
   (`setuptools<81`: das Wasserzeichen-Paket von Chatterbox braucht noch `pkg_resources`.)
3. Stimmvorlagen: `<name>.wav`, 10–15 s saubere Sprache, in den Ordner `SPUK_VORLAGEN`. Welche Vorlage zu welcher
   Stimme gehört, steht in `STIMMEN` in `stimme.py` (`daemon` braucht keine Vorlage). Standard ist `alter_mann`
   (Vorlage: freesound.org, sabotovat, Sound 414496, CC BY 3.0); fehlt eine Stimme oder scheitert sie, spricht
   `maedchen` als Ersatz. Englische Vorlagen färben das Deutsch ein – dagegen hilft `cfg_weight` 0.
4. Dienste: `systemd/*.service` nach `~/.config/systemd/user/` kopieren, Pfade prüfen, dann
   `systemctl --user daemon-reload && systemctl --user enable --now spuk-llm spuk-stimme`.
5. Firewall: Ports 8091–8092 für das LAN öffnen (z. B. `sudo ufw allow from 192.168.1.0/24 to any port 8091:8092 proto tcp`).
6. Oberfläche → Reiter 🤖 KI → Einstellungen der KI: Adressen eintragen; unter Einstellungen → Auslöser einen
   Auslöser vom Typ „KI-Stimme spricht an“ mit Personensensor, Kamera (`camera.…`), Lage-Hinweis und Ersatz anlegen.

## KI-Zentrale (Reiter 🤖 KI)

Schalter, Erreichbarkeit der Dienste, die KI-Auslöser, die letzte Erkennung (Bild, was die KI sieht, was sie
gesagt hat, Zeiten), ein Live-Bild (Standbild alle 2 s, nur solange die Seite offen ist) und der Verlauf.
Das Modell antwortet als JSON mit Beschreibung und Satz.

Test ohne Server: `curl -X POST localhost:8092/sprechen -H 'Content-Type: application/json'
-d '{"text":"Komm näher.","stimme":"daemon"}' -o test.wav`

## Besuchs-Ablauf

Auslöser vom Typ KI bekommen eine Rolle: **Weg** (fern, lockt näher), **Haustür** (nah, fordert zum Klingeln auf);
der Klingel-Auslöser bekommt die Rolle **Klingel**. Der Server fasst die Erkennungen zu einem Besuch zusammen
(`server/app/besuch.py`): höchstens 2× locken (einmal sanft nachfassen, wenn sie stehen bleiben), einmal zum Klingeln
auffordern, nach dem Klingeln 3 min still, wer geht wird verabschiedet und nie zurückgelockt, danach Nachlauf.
Es spricht immer nur eine Stimme, während Szenen schweigt die KI, Zufallsereignisse pausieren während eines Besuchs.
„Pause 10 min“ im Reiter KI, wenn ihr selbst rausgeht.
