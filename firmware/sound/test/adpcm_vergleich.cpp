// Prüft den ADPCM-Dekoder der Sound-Firmware (siehe adpcm_test.sh).
// Aufruf: adpcm_vergleich <datei.wav> <referenz.s16le> [min_snr_db]
//   ohne min_snr_db: bit-genau, z. B. gegen die Referenz nach IMA-Spezifikation
//   mit min_snr_db: Abweichung muss so weit unter dem Signal liegen, z. B. gegen ffmpeg
//   (ältere ffmpeg-Versionen rechnen IMA mit einer anderen Rundung)
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "../src/adpcm.h"

int main(int argc, char** argv) {
  if (argc != 3 && argc != 4) return 2;
  const double minSnr = argc == 4 ? atof(argv[3]) : 0;
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
  // die Referenz darf bis zum Blockende gehen (ffmpeg dekodiert die Auffüllung mit),
  // wir dekodieren nur bis zur Länge aus dem fact-Chunk
  if (alle.size() != info.samples || ref.size() < alle.size() || ref.size() - alle.size() >= info.samplesProBlock) {
    printf("FEHLER: %zu Samples (fact: %u), ffmpeg liefert %zu\n", alle.size(), info.samples, ref.size());
    return 1;
  }
  if (minSnr == 0) {
    for (size_t i = 0; i < alle.size(); i++)
      if (alle[i] != ref[i]) {
        printf("FEHLER: Sample %zu: %d statt %d\n", i, alle[i], ref[i]);
        return 1;
      }
    printf("OK: %zu Samples, %u Hz, Block %u Bytes – bit-genau wie die Referenz\n", alle.size(), info.abtastrate,
           info.blockAlign);
    return 0;
  }
  double signal = 0, fehlerQ = 0;
  for (size_t i = 0; i < alle.size(); i++) {
    double d = (double)alle[i] - ref[i];
    signal += (double)ref[i] * ref[i];
    fehlerQ += d * d;
  }
  double snr = fehlerQ > 0 ? 10 * log10(signal / fehlerQ) : 999;
  if (snr < minSnr) {
    printf("FEHLER: Abstand zu ffmpeg nur %.1f dB (mindestens %.0f dB)\n", snr, minSnr);
    return 1;
  }
  printf("OK: passt zu ffmpeg (Abweichung %s)\n", fehlerQ > 0 ? (std::to_string((int)snr) + " dB unter dem Signal").c_str() : "keine");
  return 0;
}
