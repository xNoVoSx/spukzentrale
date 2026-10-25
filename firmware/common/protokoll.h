// Spukzentrale – gemeinsames UART-Protokoll zwischen Sound-ESP (#2) und Licht-ESP (#1)
//
// Rahmen: 0xA5 0x5A <typ> <len> <payload[len]> <pruefsumme>
// Prüfsumme = XOR über typ, len und alle Payload-Bytes.
//
// Der Sound-ESP schickt ca. 100x pro Sekunde die Lautstärke-Hüllkurve des
// gerade gehörten Mixes. Sie ist bereits um die Ausgabe-Latenz verzögert,
// d. h. das Licht reagiert genau dann, wenn der Ton aus der Box kommt.
// Startet der Sound-ESP einen Effekt mit Licht-Aktion, schickt er die Aktion
// ebenfalls über diese Leitung – so sitzt der Blitz exakt auf dem Donner.
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>

namespace spuk {

constexpr uint32_t UART_BAUD = 115200;
constexpr uint8_t SYNC1 = 0xA5;
constexpr uint8_t SYNC2 = 0x5A;
constexpr uint8_t MAX_PAYLOAD = 32;

enum MsgTyp : uint8_t {
  MSG_HUELLE = 1,      // Sound -> Licht, Payload: Huelle
  MSG_HERZSCHLAG = 2,  // beide Richtungen, Payload: leer
  MSG_AKTION = 3,      // Sound -> Licht, Payload: LichtAktion
};

// Einmal-Aktionen des Licht-ESP. Die Namen sind Teil des WebSocket-Protokolls
// (Feld "name") – Reihenfolge nur am Ende erweitern.
enum Aktion : uint8_t {
  A_KEINE = 0,
  A_BLITZ,      // kurze Blitzserie
  A_ENV_BLITZ,  // Blitze folgen der Effekt-Lautstärke (Donner!)
  A_SCHRECK,    // Dunkel, dann voll an mit Flackern
  A_BLACKOUT,   // alles aus
  A_WELLE,      // Lichtwelle von links nach rechts
  A_ALLE_AN,    // alle Zonen hell
  A_PULS,       // dreimal pulsieren
  A_TEST,       // nur gewählte Zonen an (Zuordnung prüfen)
  A_FLACKERN,   // unruhiges Flackern, z. B. zu Gelächter
  A_ENV_PULS,   // Helligkeit folgt weich der Effekt-Lautstärke (Herzschlag)
  A_ANZAHL
};

inline const char* aktionName(uint8_t a) {
  static const char* const NAMEN[A_ANZAHL] = {"keine", "blitz",   "env_blitz", "schreck", "blackout",
                                              "welle", "alle_an", "puls",      "test",    "flackern",
                                              "env_puls"};
  return a < A_ANZAHL ? NAMEN[a] : "keine";
}

// unbekannt -> A_KEINE
inline uint8_t aktionVonName(const char* name) {
  if (!name) return A_KEINE;
  for (uint8_t i = 0; i < A_ANZAHL; i++)
    if (strcmp(name, aktionName(i)) == 0) return i;
  return A_KEINE;
}

enum HuelleFlags : uint8_t {
  FLAG_MUSIK_AKTIV = 1 << 0,   // Ambiente-Stream läuft
  FLAG_FX_AKTIV = 1 << 1,      // mindestens ein Effekt-Sound spielt
  FLAG_SERVER = 1 << 2,        // Sound-ESP ist mit dem Server verbunden
};

struct __attribute__((packed)) Huelle {
  uint8_t musik;  // Pegel der Musik 0..255 (RMS)
  uint8_t fx;     // Pegel der Soundeffekte 0..255 (Spitze)
  uint8_t flags;  // HuelleFlags
};

struct __attribute__((packed)) LichtAktion {
  uint8_t typ;      // Aktion
  uint8_t maske;    // Bit i = Zone i, 0 = alle
  uint8_t staerke;  // 0..100
  uint8_t reserve;
  uint16_t dauerMs;
  uint16_t param;
  uint16_t verzMs;  // Startverzögerung ab Empfang
};
static_assert(sizeof(LichtAktion) <= MAX_PAYLOAD, "LichtAktion zu groß");

// Baut einen Rahmen in out (mind. len + 5 Bytes), gibt die Gesamtlänge zurück.
inline size_t baueRahmen(uint8_t typ, const void* payload, uint8_t len, uint8_t* out) {
  out[0] = SYNC1;
  out[1] = SYNC2;
  out[2] = typ;
  out[3] = len;
  uint8_t chk = typ ^ len;
  const uint8_t* p = static_cast<const uint8_t*>(payload);
  for (uint8_t i = 0; i < len; i++) {
    out[4 + i] = p[i];
    chk ^= p[i];
  }
  out[4 + len] = chk;
  return len + 5;
}

// Zustandsautomat zum Empfangen. feed() gibt true zurück, sobald ein
// vollständiger, gültiger Rahmen in typ/len/payload bereitliegt.
class RahmenLeser {
 public:
  uint8_t typ = 0;
  uint8_t len = 0;
  uint8_t payload[MAX_PAYLOAD] = {0};

  bool feed(uint8_t b) {
    switch (zustand_) {
      case 0:
        zustand_ = (b == SYNC1) ? 1 : 0;
        return false;
      case 1:
        zustand_ = (b == SYNC2) ? 2 : (b == SYNC1 ? 1 : 0);
        return false;
      case 2:
        typ = b;
        chk_ = b;
        zustand_ = 3;
        return false;
      case 3:
        len = b;
        chk_ ^= b;
        pos_ = 0;
        if (len > MAX_PAYLOAD) {
          zustand_ = 0;
          return false;
        }
        zustand_ = (len == 0) ? 5 : 4;
        return false;
      case 4:
        payload[pos_++] = b;
        chk_ ^= b;
        if (pos_ >= len) zustand_ = 5;
        return false;
      case 5:
        zustand_ = 0;
        return b == chk_;
    }
    zustand_ = 0;
    return false;
  }

 private:
  uint8_t zustand_ = 0;
  uint8_t chk_ = 0;
  uint8_t pos_ = 0;
};

}  // namespace spuk
