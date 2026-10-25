// Spukzentrale – Dekoder für HTTP "Transfer-Encoding: chunked"
//
// Der Server (uvicorn) schickt den endlosen Ambiente-Stream in Chunks:
//   <Länge hex>[;Erweiterung]\r\n <Daten> \r\n ... 0\r\n\r\n
// Frei von Arduino-Abhängigkeiten, damit er am PC getestet werden kann (test/chunked_test).
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace spuk {

class ChunkDekoder {
 public:
  // chunked = false: Daten werden unverändert durchgereicht
  void start(bool chunked) {
    chunked_ = chunked;
    z_ = GROESSE;
    rest_ = 0;
    ziffern_ = 0;
    fehler_ = false;
    ende_ = false;
  }

  bool fehler() const { return fehler_; }
  bool ende() const { return ende_; }

  // Verarbeitet n Bytes aus ein und schreibt die Nutzdaten nach aus (darf == ein sein).
  // Gibt die Anzahl der Nutzbytes zurück.
  size_t verarbeite(const uint8_t* ein, size_t n, uint8_t* aus) {
    if (!chunked_) {
      if (aus != ein)
        for (size_t i = 0; i < n; i++) aus[i] = ein[i];
      return n;
    }
    size_t k = 0;
    for (size_t i = 0; i < n && !fehler_ && !ende_; i++) {
      uint8_t b = ein[i];
      switch (z_) {
        case GROESSE: {
          int w = hex(b);
          if (w >= 0) {
            if (++ziffern_ > 7) fehler_ = true;  // > 256 MB pro Chunk ist sicher Unsinn
            rest_ = (rest_ << 4) | (uint32_t)w;
          } else if (b == ';' || b == ' ' || b == '\t') {
            z_ = ERWEITERUNG;
          } else if (b == '\r') {
            // auf \n warten
          } else if (b == '\n') {
            if (ziffern_ == 0) {
              fehler_ = true;
            } else if (rest_ == 0) {
              ende_ = true;
            } else {
              z_ = DATEN;
            }
          } else {
            fehler_ = true;
          }
          break;
        }
        case ERWEITERUNG:
          if (b == '\n') {
            if (rest_ == 0) {
              ende_ = true;
            } else {
              z_ = DATEN;
            }
          }
          break;
        case DATEN: {
          // möglichst viel am Stück kopieren
          size_t m = n - i;
          if (m > rest_) m = rest_;
          for (size_t j = 0; j < m; j++) aus[k++] = ein[i + j];
          i += m - 1;
          rest_ -= (uint32_t)m;
          if (rest_ == 0) z_ = NACH_DATEN;
          break;
        }
        case NACH_DATEN:
          if (b == '\n') {
            z_ = GROESSE;
            ziffern_ = 0;
          } else if (b != '\r') {
            fehler_ = true;
          }
          break;
      }
    }
    return k;
  }

 private:
  enum Zustand : uint8_t { GROESSE, ERWEITERUNG, DATEN, NACH_DATEN };

  static int hex(uint8_t b) {
    if (b >= '0' && b <= '9') return b - '0';
    if (b >= 'a' && b <= 'f') return b - 'a' + 10;
    if (b >= 'A' && b <= 'F') return b - 'A' + 10;
    return -1;
  }

  bool chunked_ = false;
  Zustand z_ = GROESSE;
  uint32_t rest_ = 0;
  uint8_t ziffern_ = 0;
  bool fehler_ = false;
  bool ende_ = false;
};

}  // namespace spuk
