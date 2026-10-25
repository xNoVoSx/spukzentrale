// Prüft den Chunk-Dekoder der Sound-Firmware (siehe chunked_test.sh).
// Aufruf: chunked_vergleich <chunked-datei> <original>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "../src/chunked.h"

static std::vector<uint8_t> lies(const char* pfad) {
  std::vector<uint8_t> d;
  FILE* f = fopen(pfad, "rb");
  if (!f) exit(2);
  int c;
  while ((c = fgetc(f)) != EOF) d.push_back((uint8_t)c);
  fclose(f);
  return d;
}

int main(int argc, char** argv) {
  if (argc != 3) return 2;
  auto ein = lies(argv[1]);
  auto soll = lies(argv[2]);
  srand(42);
  for (int runde = 0; runde < 50; runde++) {
    spuk::ChunkDekoder d;
    d.start(true);
    std::vector<uint8_t> aus;
    std::vector<uint8_t> puffer;
    size_t pos = 0;
    while (pos < ein.size() && !d.ende()) {
      size_t n = 1 + rand() % (runde < 25 ? 1460 : 7);  // große und winzige Häppchen
      if (pos + n > ein.size()) n = ein.size() - pos;
      puffer.assign(ein.begin() + pos, ein.begin() + pos + n);
      size_t k = d.verarbeite(puffer.data(), n, puffer.data());  // in-place wie in der Firmware
      aus.insert(aus.end(), puffer.begin(), puffer.begin() + k);
      pos += n;
      if (d.fehler()) {
        printf("FEHLER: Runde %d, Dekoder meldet Fehler bei Byte %zu\n", runde, pos);
        return 1;
      }
    }
    if (!d.ende() || aus != soll) {
      printf("FEHLER: Runde %d: %zu Bytes statt %zu, Ende erkannt: %d\n", runde, aus.size(), soll.size(), d.ende());
      return 1;
    }
  }
  // kaputte Eingabe muss erkannt werden
  spuk::ChunkDekoder d;
  d.start(true);
  uint8_t kaputt[] = "zz\r\nabc";
  d.verarbeite(kaputt, sizeof(kaputt) - 1, kaputt);
  if (!d.fehler()) {
    printf("FEHLER: ungültige Chunk-Länge nicht erkannt\n");
    return 1;
  }
  printf("OK: %zu Bytes in 50 Runden identisch, Fehlererkennung funktioniert\n", soll.size());
  return 0;
}
