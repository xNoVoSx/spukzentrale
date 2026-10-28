// PC-Test der Effekt-Engine (ohne Hardware). Aufruf: bash firmware/licht/test/effekte_test.sh
#include <cmath>
#include <cstdio>
#include <initializer_list>

#include "../src/effekte.h"

using namespace spuk;

static int fehler = 0;
#define PRUEFE(bed, ...)          \
  do {                            \
    if (!(bed)) {                 \
      printf("FEHLER: " __VA_ARGS__); \
      printf("\n");               \
      fehler++;                   \
    }                             \
  } while (0)

int main() {
  // 1. Jeder Effekt mit Extremwerten: Ausgabe immer endlich und in 0..1
  for (uint8_t e = 0; e < E_ANZAHL; e++) {
    for (int tempo : {0, 50, 100}) {
      Lichtmaschine m(5);
      for (int i = 0; i < 5; i++) {
        ZonenParam p;
        p.effekt = e;
        p.tempo = tempo;
        p.staerke = 100;
        p.basis = 10;
        m.setzeZone(i, p);
      }
      float out[MAX_ZONEN];
      float maxi = 0;
      for (uint32_t t = 1000; t < 61000; t += 10) {
        m.huelle((t / 37) % 255, (t / 53) % 255, t);
        m.berechne(t, out);
        for (int i = 0; i < 5; i++) {
          PRUEFE(std::isfinite(out[i]) && out[i] >= 0 && out[i] <= 1, "Effekt %s tempo %d: Wert %f", effektName(e),
                 tempo, out[i]);
          if (out[i] > maxi) maxi = out[i];
        }
      }
      if (e != E_AUS) PRUEFE(maxi > 0.05f, "Effekt %s tempo %d bleibt dunkel", effektName(e), tempo);
    }
  }

  // 2. Strobo hält die Sicherheitsgrenze ein (Standard 3 Hz)
  {
    Lichtmaschine m(1);
    ZonenParam p;
    p.effekt = E_STROBO;
    p.tempo = 100;
    p.staerke = 100;
    p.basis = 0;
    m.setzeZone(0, p);
    m.global().hell = 100;
    float out[MAX_ZONEN];
    int blitze = 0;
    bool vorher = false;
    for (uint32_t t = 0; t < 12000; t += 5) {
      m.berechne(t, out);
      bool an = out[0] > 0.5f;
      if (t > 2000 && an && !vorher) blitze++;
      vorher = an;
    }
    PRUEFE(blitze <= 3 * 10 + 1, "Strobo: %d Blitze in 10 s (Grenze 3 Hz)", blitze);
    PRUEFE(blitze >= 25, "Strobo: nur %d Blitze in 10 s", blitze);
  }

  // 3. Jede Aktion: wirkt nach verzMs, endet nach dauerMs, Werte gültig
  for (uint8_t a = 1; a < A_ANZAHL; a++) {
    Lichtmaschine m(5);
    for (int i = 0; i < 5; i++) {
      ZonenParam p;
      p.effekt = E_AN;
      p.staerke = 30;
      p.basis = 30;
      m.setzeZone(i, p);
    }
    float out[MAX_ZONEN];
    for (uint32_t t = 0; t < 2000; t += 10) m.berechne(t, out);
    float ruhe = out[2];
    m.aktion(a, 2000, 300, 1500, 0, 100, 0);
    bool veraendert = false;
    for (uint32_t t = 2000; t < 6000; t += 10) {
      if (a == A_ENV_BLITZ || a == A_FLACKERN || a == A_ENV_PULS) m.huelle(0, (t / 20) % 2 ? 250 : 120, t);
      m.berechne(t, out);
      for (int i = 0; i < 5; i++)
        PRUEFE(std::isfinite(out[i]) && out[i] >= 0 && out[i] <= 1, "Aktion %s: Wert %f", aktionName(a), out[i]);
      if (t < 2300) PRUEFE(std::fabs(out[2] - ruhe) < 0.01f, "Aktion %s wirkt vor verz_ms", aktionName(a));
      if (t >= 2300 && t < 3800 && std::fabs(out[2] - ruhe) > 0.05f) veraendert = true;
    }
    PRUEFE(veraendert, "Aktion %s zeigt keine Wirkung", aktionName(a));
    PRUEFE(std::fabs(out[2] - ruhe) < 0.02f, "Aktion %s endet nicht (%f statt %f)", aktionName(a), out[2], ruhe);
  }

  // 4. Spektrum vom Pi: Bass links, Höhen rechts; Beat blitzt auf den Schlag und klingt ab
  {
    Lichtmaschine m(5);
    ZonenParam p;
    p.effekt = E_SPEKTRUM;
    p.staerke = 100;
    p.basis = 0;
    for (int i = 0; i < 5; i++) m.setzeZone(i, p);
    m.global().hell = 100;
    float out[MAX_ZONEN];
    for (uint32_t t = 0; t < 2000; t += 10) {
      m.spektrum(250, 60, 0, 0, t);
      m.berechne(t, out);
    }
    PRUEFE(out[0] > 0.8f && out[4] < 0.1f && out[2] > 0.1f && out[2] < 0.5f,
           "Spektrum: Bass links/Höhen rechts stimmt nicht (%f %f %f)", out[0], out[2], out[4]);

    p.effekt = E_BEAT;
    p.tempo = 50;
    for (int i = 0; i < 5; i++) m.setzeZone(i, p);
    for (uint32_t t = 2000; t < 3000; t += 10) {  // Überblendung abwarten, kein Schlag
      m.spektrum(100, 100, 100, 0, t);
      m.berechne(t, out);
    }
    PRUEFE(out[0] < 0.05f, "Beat: ohne Schlag nicht dunkel (%f)", out[0]);
    m.spektrum(200, 100, 100, 255, 3000);
    m.berechne(3000, out);
    float spitze = 0;
    for (int i = 0; i < 5; i++) spitze = out[i] > spitze ? out[i] : spitze;
    PRUEFE(spitze > 0.8f, "Beat: kein Blitz auf den Schlag (%f)", spitze);
    for (uint32_t t = 3010; t < 4000; t += 10) {
      m.spektrum(100, 100, 100, 0, t);
      m.berechne(t, out);
    }
    PRUEFE(out[0] < 0.05f && out[1] < 0.05f, "Beat: klingt nicht ab (%f)", out[0]);
  }

  // 5. Namen hin und zurück
  for (uint8_t e = 0; e < E_ANZAHL; e++) PRUEFE(effektVonName(effektName(e)) == e, "Effektname %d", e);
  for (uint8_t a = 0; a < A_ANZAHL; a++) PRUEFE(aktionVonName(aktionName(a)) == a, "Aktionsname %d", a);

  if (fehler) {
    printf("%d Fehler\n", fehler);
    return 1;
  }
  printf("OK: %d Effekte, %d Aktionen geprüft\n", (int)E_ANZAHL, (int)A_ANZAHL - 1);
  return 0;
}
