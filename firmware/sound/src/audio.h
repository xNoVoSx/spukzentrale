// Spukzentrale – Audio-Pipeline des Sound-ESP
//
//   Server ──HTTP (MP3-Endlos-Stream)──► netzTask ──► MP3-Puffer ──► decoderTask (Helix)
//                                                                         │
//   LittleFS /fx/<id>.wav (IMA-ADPCM) ──► Stimmen (max. 4) ─┐              ▼ PCM-Puffer
//                                                           └──► audioTask: Mixer, Ducking,
//                                                                Limiter ──► I2S ──► PCM5102A
//                                                                    │
//                                       Hüllkurve + Licht-Aktionen ──┴──► UART ──► Licht-ESP
#pragma once
#include <HardwareSerial.h>
#include <stdint.h>

#include "protokoll.h"

namespace spuk {

struct AudioConfig {
  uint8_t master = 80;     // Gesamtlautstärke 0..100
  uint8_t musikVol = 70;   // Ambiente 0..100
  uint8_t fxVol = 100;     // Effekte 0..100
  uint8_t duck = 40;       // Ambiente wird bei Effekten um so viel % leiser
  uint16_t latenzMs = 60;  // Ausgabe-Latenz: um so viel wird das Licht verzögert
};

struct AudioStatus {
  bool musikSoll = false;   // Ambiente soll laufen
  bool musikVerbunden = false;
  bool musikSpielt = false; // es kommt tatsächlich Ton
  bool formatFehler = false;
  uint8_t puffer = 0;       // Füllstand MP3-Puffer in %
  uint8_t stimmen = 0;      // aktive Effekt-Stimmen
  uint32_t unterlaeufe = 0; // Aussetzer seit Start
};

constexpr int MAX_STIMMEN = 4;
constexpr const char* FX_ORDNER = "/fx";

// Startet I2S und alle Audio-Tasks. uart: Verbindung zum Licht-ESP (bereits geöffnet).
void audioStart(int bck, int lck, int din, HardwareSerial* uart);
void audioConfig(const AudioConfig& c);

// Ambiente-Stream (HTTP, MP3 44,1 kHz) starten bzw. sanft stoppen
void musikPlay(const char* host, uint16_t port, const char* pfad);
void musikStop();

// Effekt aus FX_ORDNER/<id>.wav starten. aktion (optional) geht zeitgleich mit dem
// ersten Ton an den Licht-ESP. vol 0..100.
void fxStart(const char* id, uint8_t vol, const LichtAktion* aktion);
void fxStopAlle();

// Während der Synchronisierung keine Dateien öffnen (Dateien werden ersetzt/gelöscht)
void fxSperren(bool gesperrt);

AudioStatus audioStatus();
void fxDateiPfad(const char* id, char* ziel, size_t laenge);

}  // namespace spuk
