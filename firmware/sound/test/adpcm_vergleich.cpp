// Prüft den ADPCM-Dekoder der Sound-Firmware gegen ffmpeg (siehe adpcm_test.sh).
// Aufruf: adpcm_vergleich <datei.wav> <referenz.s16le>
#include <cstdio>
#include <vector>

#include "../src/adpcm.h"

int main(int argc, char** argv) {
  if (argc != 3) return 2;
  FILE* f = fopen(argv[1], "rb");
  FILE* r = fopen(argv[2], "rb");
  if (!f || !r) return 2;
  std::vector<uint8_t> datei;
  int c;
  while ((c = fgetc(f)) != EOF) datei.push_back((uint8_t)c);
  std::vector<int16_t> ref;
  int16_t s;
  while (fread(&s, 2, 1, r) == 1) ref.push_back(s);

  auto lesen = [&](uint32_t off, uint8_t* ziel, uint32_t n) -> uint32_t {
    uint32_t k = 0;
    for (; k < n && off + k < datei.size(); k++) ziel[k] = datei[off + k];
    return k;
  };
  spuk::WavInfo info;
  if (!spuk::leseWavKopf(lesen, (uint32_t)datei.size(), info)) {
    printf("FEHLER: WAV-Kopf nicht erkannt\n");
    return 1;
  }
  std::vector<int16_t> pcm(info.samplesProBlock), alle;
  for (uint32_t p = info.datenStart; p + info.blockAlign <= info.datenStart + info.datenLaenge; p += info.blockAlign) {
    size_t n = spuk::dekodiereImaBlock(&datei[p], info.blockAlign, pcm.data());
    alle.insert(alle.end(), pcm.begin(), pcm.begin() + n);
  }
  if (alle.size() > info.samples) alle.resize(info.samples);
  // ffmpeg dekodiert bis zum Blockende (inkl. Auffüllung), wir nur bis zur Länge aus dem fact-Chunk
  if (alle.size() != info.samples || ref.size() < alle.size() || ref.size() - alle.size() >= info.samplesProBlock) {
    printf("FEHLER: %zu Samples (fact: %u), ffmpeg liefert %zu\n", alle.size(), info.samples, ref.size());
    return 1;
  }
  for (size_t i = 0; i < alle.size(); i++)
    if (alle[i] != ref[i]) {
      printf("FEHLER: Sample %zu: %d statt %d\n", i, alle[i], ref[i]);
      return 1;
    }
  printf("OK: %zu Samples, %u Hz, Block %u Bytes – identisch mit ffmpeg\n", alle.size(), info.abtastrate,
         info.blockAlign);
  return 0;
}
