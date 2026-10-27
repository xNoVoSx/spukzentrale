// PC-Test der Ambiente-Ausgabe (src/musik_ausgabe.h). Aufruf: bash firmware/sound/test/musik_ausgabe_test.sh
//
// Simuliert den Decoder (liefert 1152-Frame-MP3-Blöcke = 4608 Byte) und die Audio-Task (holt alle 10 ms 441 Frames).
// Ein Sinus (440 Hz) zeigt Knacken als großen Sprung zwischen zwei Samples.
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <deque>
#include <vector>

#include "../src/musik_ausgabe.h"

using namespace spuk;

constexpr int RATE = 44100, BLOCK = 441, MP3_FRAME = 1152;
constexpr size_t PUFFER = 16 * 1024;
static int fehler = 0;
#define PRUEFE(bed, ...)              \
  do {                                \
    if (!(bed)) {                     \
      printf("FEHLER: " __VA_ARGS__); \
      printf("\n");                   \
      fehler++;                       \
    }                                 \
  } while (0)

// Decoder wie auf dem ESP: dekodiert einen MP3-Frame (DEKODIER_MS Rechenzeit), schreibt ihn dann in den Puffer,
// sobald Platz ist – die neue Firmware in Stücken von höchstens STUECK Byte, die alte nur als ganzen Frame.
// Hänger: der Decoder bekommt ab hang_ab für hang_ms keine Rechenzeit (z. B. WLAN auf demselben Kern).
// Die Audio-Task holt alle 10 ms einen Block.
constexpr double DEKODIER_MS = 5.0;

struct Simulation {
  std::deque<uint8_t> puffer;
  long naechstesSample = 0;  // Quelle: laufender Sinus
  std::vector<uint8_t> fertig;  // dekodierter Frame, noch nicht (ganz) geschrieben
  double dekodiert_bis = 0;
  std::vector<int16_t> aus;     // linker Kanal

  void dekodiere() {
    fertig.clear();
    for (int i = 0; i < MP3_FRAME; i++) {
      int16_t s = (int16_t)(20000 * sin(2 * M_PI * 440.0 * naechstesSample++ / RATE));
      for (int k = 0; k < 2; k++) {
        fertig.push_back((uint8_t)(s & 0xFF));
        fertig.push_back((uint8_t)((s >> 8) & 0xFF));
      }
    }
  }

  // Decoder bis zum Zeitpunkt t (ms) laufen lassen
  void decoder(double t, size_t groesse, size_t stueck, double hang_ab, double hang_ms) {
    for (double u = t - 1.0; u < t; u += 0.25) {
      if (u >= hang_ab && u < hang_ab + hang_ms) continue;
      if (fertig.empty()) {
        if (dekodiert_bis == 0) dekodiert_bis = u + DEKODIER_MS;
        if (u >= dekodiert_bis) {
          dekodiere();
          dekodiert_bis = 0;
        }
        continue;
      }
      size_t n = std::min(stueck, fertig.size());
      if (puffer.size() + n <= groesse) {
        puffer.insert(puffer.end(), fertig.begin(), fertig.begin() + n);
        fertig.erase(fertig.begin(), fertig.begin() + n);
      }
    }
  }

  size_t lesen(uint8_t* d, size_t n) {
    size_t k = 0;
    for (; k < n && !puffer.empty(); k++) {
      d[k] = puffer.front();
      puffer.pop_front();
    }
    return k;
  }
};

// Neue Logik (MusikAusgabe, 16-KB-Puffer, Decoder schreibt in 10-ms-Stücken)
static void lauf_neu(Simulation& sim, MusikAusgabe& a, int bloecke, double hang_ab, double hang_ms) {
  int16_t blk[BLOCK * 2];
  for (int t = 1; t <= bloecke * 10; t++) {
    sim.decoder(t, PUFFER, BLOCK * 4, hang_ab, hang_ms);
    if (t % 10) continue;
    a.block(blk, BLOCK, sim.puffer.size(), true, [&](uint8_t* d, size_t n) { return sim.lesen(d, n); });
    for (int i = 0; i < BLOCK; i++) sim.aus.push_back(blk[2 * i]);
  }
}

// Alte Logik (1.1.0): 8-KB-Puffer, ganze Frames, was da ist ausgeben und den Rest mit Stille füllen
static void lauf_alt(Simulation& sim, int bloecke, double hang_ab, double hang_ms) {
  for (int t = 1; t <= bloecke * 10; t++) {
    sim.decoder(t, 8 * 1024, MP3_FRAME * 4, hang_ab, hang_ms);
    if (t % 10) continue;
    std::vector<uint8_t> raw(BLOCK * 4, 0);
    sim.lesen(raw.data(), raw.size());
    for (int i = 0; i < BLOCK; i++) sim.aus.push_back((int16_t)(raw[4 * i] | (raw[4 * i + 1] << 8)));
  }
}

static int max_sprung(const std::vector<int16_t>& s, size_t ab) {
  int m = 0;
  for (size_t i = ab + 1; i < s.size(); i++) m = std::max(m, std::abs(s[i] - s[i - 1]));
  return m;
}

int main() {
  // natürlicher größter Sprung eines 440-Hz-Sinus mit Amplitude 20000
  const int sinus_sprung = (int)ceil(20000 * 2 * M_PI * 440.0 / RATE) + 2;

  // 1. gleichmäßiger Decoder: kein Unterlauf, nach dem Einblenden bit-genau die Quelle
  {
    Simulation sim;
    MusikAusgabe a(8 * 1024, 220);
    lauf_neu(sim, a, 3000, 0, 0);
    PRUEFE(a.unterlaeufe == 0, "gleichmäßig: %u Unterläufe", a.unterlaeufe);
    size_t start = 0;                      // erstes Sample der Musik (Sinus beginnt bei 0, eingeblendet)
    while (start < sim.aus.size() && sim.aus[start] == 0) start++;
    start--;
    long quelle = 220;  // nach dem Einblenden
    bool gleich = true;
    for (size_t i = start + 220; i < sim.aus.size(); i++, quelle++) {
      int16_t soll = (int16_t)(20000 * sin(2 * M_PI * 440.0 * quelle / RATE));
      if (sim.aus[i] != soll) { gleich = false; break; }
    }
    PRUEFE(gleich, "gleichmäßig: Ausgabe weicht von der Quelle ab");
    PRUEFE(max_sprung(sim.aus, 0) <= sinus_sprung, "gleichmäßig: Knacken (Sprung %d)", max_sprung(sim.aus, 0));
  }

  // 2. Decoder hängt einmal 120 ms: genau ein Unterlauf, weich (kein Sprung über dem Sinus), danach weiter
  {
    Simulation sim;
    MusikAusgabe a(8 * 1024, 220);
    lauf_neu(sim, a, 3000, 10000, 120);
    PRUEFE(a.unterlaeufe == 1, "Hänger 120 ms: %u Unterläufe statt 1", a.unterlaeufe);
    PRUEFE(max_sprung(sim.aus, 0) <= sinus_sprung, "Hänger 120 ms: Knacken (Sprung %d > %d)", max_sprung(sim.aus, 0), sinus_sprung);
  }

  // 3. Hänger bis 60 ms schluckt der 16-KB-Puffer ganz (vorher reichten ~20 ms)
  {
    Simulation sim;
    MusikAusgabe a(8 * 1024, 220);
    lauf_neu(sim, a, 3000, 10000, 60);
    PRUEFE(a.unterlaeufe == 0, "Hänger 60 ms: %u Unterläufe (Puffer müsste reichen)", a.unterlaeufe);
  }

  // 4. Gegenprobe: alte Logik mit 8-KB-Puffer knackt beim selben 40-ms-Hänger
  {
    Simulation sim;
    lauf_alt(sim, 3000, 10000, 40);
    PRUEFE(max_sprung(sim.aus, 0) > 5 * sinus_sprung, "Gegenprobe: alte Logik knackt nicht – Test ist wertlos");
    printf("Gegenprobe alte Logik (8 KB, Hänger 40 ms): größter Sprung %d (Sinus: %d)\n", max_sprung(sim.aus, 0), sinus_sprung);
  }

  // 5. aus: nur Stille, Neustart puffert neu vor
  {
    MusikAusgabe a(8 * 1024, 220);
    int16_t blk[BLOCK * 2];
    size_t n = a.block(blk, BLOCK, 16000, false, [](uint8_t*, size_t) { return (size_t)0; });
    bool still = n == 0;
    for (int i = 0; i < BLOCK * 2; i++) still = still && blk[i] == 0;
    PRUEFE(still && !a.spielt(), "aus: keine Stille");
    n = a.block(blk, BLOCK, 100, true, [](uint8_t*, size_t) { return (size_t)0; });
    PRUEFE(n == 0 && !a.spielt() && a.unterlaeufe == 0, "Vorlauf: spielt schon vor dem Vorpuffern");
  }

  if (fehler) {
    printf("%d Fehler\n", fehler);
    return 1;
  }
  printf("OK: kein Unterlauf bei gleichmäßigem Decoder (bit-genau), Hänger 60 ms geschluckt, Hänger 120 ms = 1 weicher Aussetzer ohne Knacken\n");
  return 0;
}
