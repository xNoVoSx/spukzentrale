// Spukzentrale – Ausgabe des Ambiente-PCM in 10-ms-Blöcken
//
// Der Decoder füllt einen Puffer, die Audio-Task holt alle 10 ms einen Block. Kommt der Decoder einmal nicht
// hinterher, darf das nicht als Folge kleiner Lücken hörbar werden (klingt wie Krachen). Deshalb:
//  - vor dem Start und nach jedem Unterlauf erst VORLAUF Bytes sammeln, dann in EINBLENDEN Frames einblenden
//  - bei einem Unterlauf das, was noch da ist, ausblenden statt hart abzuschneiden, und neu vorpuffern
//
// Frei von Arduino/FreeRTOS, damit es am PC getestet werden kann (test/musik_ausgabe_test).
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace spuk {

class MusikAusgabe {
 public:
  static constexpr size_t BYTES_PRO_FRAME = 4;  // Stereo, 16 Bit

  MusikAusgabe(size_t vorlaufBytes, size_t einblendFrames) : vorlauf_(vorlaufBytes), einblenden_(einblendFrames) {}

  // Einen Block holen. ziel: frames Stereo-Frames (wird immer ganz beschrieben, Stille wo nichts da ist).
  // verfuegbar: Füllstand des Puffers in Bytes. lesen(dst, maxBytes) -> gelesene Bytes.
  // Gibt die Zahl der Frames zurück, die echte Musik enthalten.
  template <typename Lesen>
  size_t block(int16_t* ziel, size_t frames, size_t verfuegbar, bool aktiv, Lesen lesen) {
    const size_t blockBytes = frames * BYTES_PRO_FRAME;
    size_t gelesen = 0;
    if (!aktiv) {
      bereit_ = false;
    } else if (!bereit_ && verfuegbar >= vorlauf_) {
      bereit_ = true;
      einblendRest_ = einblenden_;
    }
    if (aktiv && bereit_) {
      if (verfuegbar < minVerfuegbar) minVerfuegbar = verfuegbar;
      gelesen = lesen(reinterpret_cast<uint8_t*>(ziel), blockBytes);
      gelesen -= gelesen % BYTES_PRO_FRAME;
      if (gelesen < blockBytes) {
        // Unterlauf: Rest ausblenden, danach neu vorpuffern
        unterlaeufe++;
        if (gelesen > 0)
          teilbloecke++;
        else
          leerbloecke++;
        ausblenden(ziel, gelesen / BYTES_PRO_FRAME);
        bereit_ = false;
      }
      einblenden(ziel, gelesen / BYTES_PRO_FRAME);
    }
    memset(reinterpret_cast<uint8_t*>(ziel) + gelesen, 0, blockBytes - gelesen);
    return gelesen / BYTES_PRO_FRAME;
  }

  bool spielt() const { return bereit_; }

  // Zähler seit dem Start
  uint32_t unterlaeufe = 0;   // Unterläufe (je einer = ein kurzer, weich ausgeblendeter Aussetzer)
  uint32_t teilbloecke = 0;   // davon: es war noch ein Rest da
  uint32_t leerbloecke = 0;   // davon: Puffer war ganz leer
  size_t minVerfuegbar = SIZE_MAX;  // niedrigster Füllstand beim Abspielen

 private:
  void ausblenden(int16_t* p, size_t n) {
    for (size_t i = 0; i < n; i++) {
      int32_t g = (int32_t)((n - i) * 256 / (n + 1));
      p[2 * i] = (int16_t)(p[2 * i] * g / 256);
      p[2 * i + 1] = (int16_t)(p[2 * i + 1] * g / 256);
    }
  }

  void einblenden(int16_t* p, size_t n) {
    for (size_t i = 0; i < n && einblendRest_ > 0; i++, einblendRest_--) {
      int32_t g = (int32_t)((einblenden_ - einblendRest_ + 1) * 256 / (einblenden_ + 1));
      p[2 * i] = (int16_t)(p[2 * i] * g / 256);
      p[2 * i + 1] = (int16_t)(p[2 * i + 1] * g / 256);
    }
  }

  size_t vorlauf_;
  size_t einblenden_;
  size_t einblendRest_ = 0;
  bool bereit_ = false;
};

}  // namespace spuk
