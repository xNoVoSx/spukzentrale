// Spukzentrale – Effekt-Engine für den Licht-ESP
//
// Die Engine ist bewusst frei von Arduino-Abhängigkeiten: Sie bekommt die Zeit
// und die Audio-Hüllkurve von außen und liefert pro Zone einen Helligkeitswert
// 0..1 (linear, vor Gamma). Dadurch lässt sie sich auch am PC testen.
#pragma once
#include <stdint.h>

#include "protokoll.h"

namespace spuk {

constexpr int MAX_ZONEN = 6;
constexpr int MAX_OVERLAYS = 6;
constexpr int HUELLE_HISTORIE = 64;  // 0,64 s bei 100 Hz

enum Effekt : uint8_t {
  E_AUS = 0,
  E_AN,
  E_FLACKERN,
  E_FEUER,
  E_ATMEN,
  E_HERZSCHLAG,
  E_STROBO,
  E_GEWITTER,
  E_LAUFLICHT,
  E_WANDERN,
  E_PINGPONG,
  E_NEON,
  E_ZUFALL,
  E_MUSIK,
  E_ANZAHL
};

// Aktionen (A_BLITZ, ...) sind in protokoll.h definiert, weil der Sound-ESP sie
// ebenfalls per UART auslöst.


const char* effektName(uint8_t e);
uint8_t effektVonName(const char* name);  // unbekannt -> E_FLACKERN

struct ZonenParam {
  uint8_t effekt = E_FLACKERN;
  uint8_t tempo = 50;    // 0..100
  uint8_t staerke = 80;  // 0..100 (Spitzenhelligkeit)
  uint8_t basis = 5;     // 0..100 (Grundhelligkeit)
};

struct GlobalParam {
  bool an = true;
  uint8_t hell = 80;          // Master 0..100
  uint8_t musikReaktiv = 0;   // 0..100: alle Zonen pulsieren zur Musik
  uint8_t zoneMax[MAX_ZONEN] = {100, 100, 100, 100, 100, 100};
  float stroboMaxHz = 3.0f;   // Sicherheitsgrenze für Stroboskop
};

class Lichtmaschine {
 public:
  explicit Lichtmaschine(int zonen);

  int zonen() const { return n_; }

  // Parameter setzen. Effektwechsel werden weich überblendet.
  void setzeZone(int i, const ZonenParam& p);
  const ZonenParam& zone(int i) const { return z_[i].p; }
  GlobalParam& global() { return g_; }

  // Einmal-Aktion. zonenMaske: Bit i = Zone i. verzMs: Startverzögerung
  // (Bluetooth-Latenz), dauerMs: Dauer, staerke: 0..100, param: aktionsabhängig.
  void aktion(uint8_t typ, uint32_t jetztMs, uint32_t verzMs, uint32_t dauerMs,
              uint32_t zonenMaske, uint8_t staerke, uint32_t param = 0);
  void aktionenStoppen();

  // Hüllkurve vom Sound-ESP (0..255). Ruft man ca. 100x/s auf.
  void huelle(uint8_t musik, uint8_t fx, uint32_t jetztMs);

  // Berechnet alle Zonen; ausgabe[i] linear 0..1.
  void berechne(uint32_t jetztMs, float* ausgabe);

  // Für Status/Debug
  float musikNorm() const { return musikNorm_; }
  float fxNorm() const { return fxNorm_; }

 private:
  struct ZonenZustand {
    ZonenParam p;
    float wert = 0;          // letzter Basiswert
    float zufallZiel = 0;    // für Flackern/Zufall
    float zufallWert = 0;
    uint32_t naechsterWechsel = 0;
    // Neon-Zustandsautomat
    uint8_t neonPhase = 0;
    uint32_t neonBis = 0;
    // Überblendung
    float vonWert = 0;
    uint32_t blendeStart = 0;
    bool blende = false;
  };

  struct Overlay {
    uint8_t typ = A_KEINE;
    uint32_t start = 0;
    uint32_t dauer = 0;
    uint32_t maske = 0;
    float staerke = 1;
    uint32_t param = 0;
    uint32_t seed = 0;
  };

  struct Sturm {  // gemeinsamer Zustand des Gewitter-Effekts
    uint32_t naechsterEinschlag = 0;
    uint32_t start = 0;
    uint32_t maske = 0;
    uint8_t blitze = 0;
    uint32_t seed = 1;
  };

  float zufall();  // 0..1
  float basisWert(int i, ZonenZustand& z, uint32_t t, float dt);
  float blitzMuster(uint32_t seed, uint32_t seit, uint32_t dauer) const;
  static float tempoFaktor(uint8_t tempo);

  int n_;
  ZonenZustand z_[MAX_ZONEN];
  GlobalParam g_;
  Overlay o_[MAX_OVERLAYS];
  Sturm sturm_;
  uint32_t rng_ = 0x12345678;
  uint32_t letzteZeit_ = 0;
  float masterIst_ = 0;

  // Audio
  float musikNorm_ = 0, fxNorm_ = 0;
  float musikMax_ = 40.0f;  // automatische Verstärkung
  float musikSchnitt_ = 0;
  float beat_ = 0;
  float hist_[HUELLE_HISTORIE] = {0};
  uint8_t histPos_ = 0;
  uint32_t letzteHuelle_ = 0;
};

}  // namespace spuk
