// Abgleich C++-Engine <-> JavaScript-Port (server/static/lichtmaschine.js).
// Liest ein Drehbuch (Befehlszeilen, erzeugt von vergleich_js.mjs) und gibt bei jedem "B" die Zonenwerte aus.
// Aufruf: bash firmware/licht/test/vergleich_js.sh
//
// Befehle:  N zonen | Z i effekt tempo staerke basis | G an hell musikReaktiv zm0..zm5 stroboMaxHz
//           A typ jetzt verz dauer maske staerke param | S | H musik fx jetzt | P bass mitte hoehen beat jetzt | B jetzt
// Ausgabe je B: jetzt w0 .. w(n-1) musikNorm fxNorm (float-Bits als Hex, verlustfrei)
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>

#include "../src/effekte.h"

using namespace spuk;

static uint32_t bits(float f) {  // float verlustfrei ausgeben
  uint32_t u;
  memcpy(&u, &f, sizeof(u));
  return u;
}

int main(int argc, char** argv) {
  if (argc < 2) {
    fprintf(stderr, "Aufruf: %s drehbuch.txt\n", argv[0]);
    return 2;
  }
  FILE* in = fopen(argv[1], "r");
  if (!in) {
    perror(argv[1]);
    return 2;
  }
  std::unique_ptr<Lichtmaschine> m;
  char zeile[512];
  float out[MAX_ZONEN];
  while (fgets(zeile, sizeof(zeile), in)) {
    char* p = zeile;
    char cmd = *p++;
    auto zahl = [&p]() { return strtoul(p, &p, 10); };
    switch (cmd) {
      case 'N':
        m.reset(new Lichtmaschine((int)zahl()));
        break;
      case 'Z': {
        int i = (int)zahl();
        ZonenParam zp;
        zp.effekt = (uint8_t)zahl();
        zp.tempo = (uint8_t)zahl();
        zp.staerke = (uint8_t)zahl();
        zp.basis = (uint8_t)zahl();
        m->setzeZone(i, zp);
        break;
      }
      case 'G': {
        GlobalParam& g = m->global();
        g.an = zahl() != 0;
        g.hell = (uint8_t)zahl();
        g.musikReaktiv = (uint8_t)zahl();
        for (int i = 0; i < MAX_ZONEN; i++) g.zoneMax[i] = (uint8_t)zahl();
        g.stroboMaxHz = strtof(p, &p);
        break;
      }
      case 'A': {
        uint32_t typ = zahl(), jetzt = zahl(), verz = zahl(), dauer = zahl(), maske = zahl(), st = zahl(),
                 param = zahl();
        m->aktion((uint8_t)typ, jetzt, verz, dauer, maske, (uint8_t)st, param);
        break;
      }
      case 'S':
        m->aktionenStoppen();
        break;
      case 'H': {
        uint32_t mu = zahl(), fx = zahl(), jetzt = zahl();
        m->huelle((uint8_t)mu, (uint8_t)fx, jetzt);
        break;
      }
      case 'P': {
        uint32_t b = zahl(), mi = zahl(), h = zahl(), beat = zahl(), jetzt = zahl();
        m->spektrum((uint8_t)b, (uint8_t)mi, (uint8_t)h, (uint8_t)beat, jetzt);
        break;
      }
      case 'B': {
        uint32_t jetzt = zahl();
        m->berechne(jetzt, out);
        printf("%u", jetzt);
        for (int i = 0; i < m->zonen(); i++) printf(" %08x", bits(out[i]));
        printf(" %08x %08x\n", bits(m->musikNorm()), bits(m->fxNorm()));
        break;
      }
      default:
        break;
    }
  }
  fclose(in);
  return 0;
}
