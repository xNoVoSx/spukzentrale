// Spukzentrale – Effekt-Engine (siehe effekte.h)
#include "effekte.h"

#include <math.h>
#include <string.h>

namespace spuk {

namespace {
constexpr float PI_F = 3.14159265f;

const char* const EFFEKT_NAMEN[E_ANZAHL] = {
    "aus",    "an",       "flackern", "feuer",   "atmen",
    "herzschlag", "strobo", "gewitter", "lauflicht", "wandern",
    "pingpong", "neon",   "zufall",   "musik",   "spektrum",
    "beat"};


inline float clamp01(float x) { return x < 0 ? 0 : (x > 1 ? 1 : x); }

// Deterministisches Rauschen aus Ganzzahlen (für reproduzierbare Muster)
inline uint32_t mische(uint32_t x) {
  x ^= x >> 16;
  x *= 0x7feb352dU;
  x ^= x >> 15;
  x *= 0x846ca68bU;
  x ^= x >> 16;
  return x;
}
inline float hashRausch(uint32_t a, uint32_t b, uint32_t c) {
  return (mische(a * 0x9E3779B1U ^ mische(b + 0x85EBCA6BU) ^ (c * 0xC2B2AE35U)) & 0xFFFFFF) /
         16777215.0f;
}
}  // namespace

const char* effektName(uint8_t e) { return e < E_ANZAHL ? EFFEKT_NAMEN[e] : "flackern"; }
uint8_t effektVonName(const char* name) {
  if (!name) return E_FLACKERN;
  for (uint8_t i = 0; i < E_ANZAHL; i++)
    if (strcmp(name, EFFEKT_NAMEN[i]) == 0) return i;
  return E_FLACKERN;
}

Lichtmaschine::Lichtmaschine(int zonen) : n_(zonen < 1 ? 1 : (zonen > MAX_ZONEN ? MAX_ZONEN : zonen)) {}

float Lichtmaschine::zufall() {
  rng_ ^= rng_ << 13;
  rng_ ^= rng_ >> 17;
  rng_ ^= rng_ << 5;
  return (rng_ & 0xFFFFFF) / 16777215.0f;
}

float Lichtmaschine::tempoFaktor(uint8_t tempo) {
  // 0 -> 0,25x  |  50 -> 1x  |  100 -> 4x
  return 0.25f * powf(16.0f, (tempo > 100 ? 100 : tempo) / 100.0f);
}

void Lichtmaschine::setzeZone(int i, const ZonenParam& p) {
  if (i < 0 || i >= n_) return;
  ZonenZustand& z = z_[i];
  if (z.p.effekt != p.effekt) {
    z.vonWert = z.wert;
    z.blendeStart = letzteZeit_;
    z.blende = true;
    z.naechsterWechsel = 0;
    z.neonPhase = 0;
    z.neonBis = 0;
  }
  z.p = p;
}

void Lichtmaschine::aktion(uint8_t typ, uint32_t jetztMs, uint32_t verzMs, uint32_t dauerMs,
                           uint32_t zonenMaske, uint8_t staerke, uint32_t param) {
  if (typ == A_KEINE || typ >= A_ANZAHL) return;
  if (dauerMs == 0) dauerMs = 800;
  // Freien Platz suchen, sonst den ältesten ersetzen
  int platz = -1;
  uint32_t aeltester = 0xFFFFFFFF;
  int aeltesterIdx = 0;
  for (int k = 0; k < MAX_OVERLAYS; k++) {
    if (o_[k].typ == A_KEINE) {
      platz = k;
      break;
    }
    if (o_[k].start < aeltester) {
      aeltester = o_[k].start;
      aeltesterIdx = k;
    }
  }
  if (platz < 0) platz = aeltesterIdx;
  Overlay& o = o_[platz];
  o.typ = typ;
  o.start = jetztMs + verzMs;
  o.dauer = dauerMs;
  o.maske = zonenMaske ? zonenMaske : 0xFFFFFFFF;
  o.staerke = (staerke > 100 ? 100 : staerke) / 100.0f;
  o.param = param;
  o.seed = rng_ ^ (jetztMs * 2654435761U);
  zufall();
}

void Lichtmaschine::aktionenStoppen() {
  for (auto& o : o_) o.typ = A_KEINE;
}

void Lichtmaschine::huelle(uint8_t musik, uint8_t fx, uint32_t jetztMs) {
  letzteHuelle_ = jetztMs;
  // Automatische Verstärkung für Musik: Spitze merken, langsam abklingen lassen
  float roh = (float)musik;
  musikMax_ = musikMax_ * 0.9985f;
  if (roh > musikMax_) musikMax_ = roh;
  if (musikMax_ < 25.0f) musikMax_ = 25.0f;
  float norm = clamp01(roh / musikMax_);
  // leichte Glättung
  musikNorm_ = musikNorm_ * 0.4f + norm * 0.6f;
  // einfache Beat-Erkennung: deutlicher Anstieg über den gleitenden Schnitt
  if (musikNorm_ > musikSchnitt_ * 1.35f + 0.08f && beat_ < 0.25f) beat_ = 1.0f;
  musikSchnitt_ = musikSchnitt_ * 0.96f + musikNorm_ * 0.04f;

  fxNorm_ = fx / 255.0f;

  hist_[histPos_] = musikNorm_;
  histPos_ = (histPos_ + 1) % HUELLE_HISTORIE;
}

void Lichtmaschine::spektrum(uint8_t bass, uint8_t mitte, uint8_t hoehen, uint8_t beat, uint32_t jetztMs) {
  letztesSpektrum_ = jetztMs;
  const uint8_t roh[3] = {bass, mitte, hoehen};
  for (int k = 0; k < 3; k++) {
    float v = roh[k] / 255.0f;
    // schnell hoch, etwas langsamer runter (sonst zappelt es)
    band_[k] = v > band_[k] ? v : band_[k] * 0.82f + v * 0.18f;
  }
  if (beat) {
    float st = 0.45f + 0.55f * (beat / 255.0f);
    if (st > schlag_) schlag_ = st;
    schlaege_++;
  }
}

// Blitzfolge aus einem Startwert: 1–4 Blitze mit Nachglühen
float Lichtmaschine::blitzMuster(uint32_t seed, uint32_t seit, uint32_t dauer) const {
  if (seit >= dauer) return 0;
  uint32_t r = seed | 1;
  auto naechste = [&r]() {
    r ^= r << 13;
    r ^= r >> 17;
    r ^= r << 5;
    return (r & 0xFFFF) / 65535.0f;
  };
  int anzahl = 1 + (int)(naechste() * 3.99f);
  uint32_t pos = 0;
  for (int k = 0; k < anzahl; k++) {
    uint32_t an = 30 + (uint32_t)(naechste() * 90);
    uint32_t pause = 50 + (uint32_t)(naechste() * 170);
    float staerke = (k == 0 ? 0.75f : 0.55f) + naechste() * 0.45f;
    if (seit < pos) return 0;
    if (seit < pos + an) return clamp01(staerke);
    if (seit < pos + an + pause) {
      float s = (float)(seit - pos - an);
      return staerke * 0.18f * expf(-s / 60.0f);
    }
    pos += an + pause;
  }
  return 0;
}

float Lichtmaschine::basisWert(int i, ZonenZustand& z, uint32_t t, float dt) {
  const float a = z.p.staerke / 100.0f;
  const float b = z.p.basis / 100.0f;
  const float s = tempoFaktor(z.p.tempo);
  const float ts = t / 1000.0f;
  const float n = (float)n_;

  switch (z.p.effekt) {
    case E_AUS:
      return 0;

    case E_AN:
      return a;

    case E_FLACKERN: {  // Kerze: ruhig mit gelegentlichen Einbrüchen
      if ((int32_t)(t - z.naechsterWechsel) >= 0) {
        float r = zufall();
        z.zufallZiel = (r < 0.05f) ? 0.30f + 0.25f * zufall() : 0.72f + 0.28f * zufall();
        z.naechsterWechsel = t + (uint32_t)((45.0f + 110.0f * zufall()) / s);
      }
      z.zufallWert += (z.zufallZiel - z.zufallWert) * (1.0f - expf(-dt * 16.0f * s));
      return b + (a - b) * z.zufallWert;
    }

    case E_FEUER: {  // unruhiger, zwei Ebenen
      if ((int32_t)(t - z.naechsterWechsel) >= 0) {
        z.zufallZiel = 0.25f + 0.75f * zufall();
        z.naechsterWechsel = t + (uint32_t)((20.0f + 70.0f * zufall()) / s);
      }
      z.zufallWert += (z.zufallZiel - z.zufallWert) * (1.0f - expf(-dt * 28.0f * s));
      float fein = 0.85f + 0.15f * hashRausch(i, t / 25, 7);
      return b + (a - b) * z.zufallWert * fein;
    }

    case E_ATMEN: {
      float c = 0.5f - 0.5f * cosf(2.0f * PI_F * ts * 0.1f * s);
      return b + (a - b) * c * c;
    }

    case E_HERZSCHLAG: {
      float periode = 1.1f / s;
      float ph = fmodf(ts, periode) / periode;
      auto puls = [](float x, float c) {
        float d = (x - c) / 0.035f;
        return expf(-d * d);
      };
      float v = puls(ph, 0.05f) + 0.65f * puls(ph, 0.22f);
      return b + (a - b) * clamp01(v);
    }

    case E_STROBO: {
      float maxHz = g_.stroboMaxHz < 0.5f ? 0.5f : g_.stroboMaxHz;
      float hz = 0.5f + (maxHz - 0.5f) * (z.p.tempo / 100.0f);
      if (hz > maxHz) hz = maxHz;
      uint32_t periode = (uint32_t)(1000.0f / hz);
      return (t % periode) < 45 ? a : b;
    }

    case E_GEWITTER: {
      // dunkles Grundflackern + gemeinsame Blitzeinschläge (siehe berechne)
      float grund = b * (0.8f + 0.2f * hashRausch(i, t / 90, 3));
      float blitz = 0;
      if (sturm_.maske & (1u << i)) blitz = blitzMuster(sturm_.seed + (i & 1) * 31, t - sturm_.start, 1200);
      float v = a * blitz;
      return v > grund ? v : grund;
    }

    case E_LAUFLICHT: {
      float pos = fmodf(ts * s * 1.2f, n);
      float d = fabsf((float)i - pos);
      if (d > n / 2) d = n - d;
      float v = clamp01(1.0f - d);
      return b + (a - b) * powf(v, 1.5f);
    }

    case E_WANDERN: {  // weicher Lichtfleck wandert hin und her (Geister!)
      float x = (n - 1) * (0.5f - 0.5f * cosf(PI_F * ts * s * 0.25f));
      float d = (float)i - x;
      float v = expf(-d * d / (2.0f * 0.55f * 0.55f));
      return b + (a - b) * v;
    }

    case E_PINGPONG: {
      if (n_ < 2) return b + (a - b) * (0.5f - 0.5f * cosf(2 * PI_F * ts * s));
      float laenge = 2.0f * (n - 1);
      float tri = fmodf(ts * s * 1.6f, laenge);
      float x = tri <= (n - 1) ? tri : laenge - tri;
      float v = clamp01(1.0f - fabsf((float)i - x));
      return b + (a - b) * v;
    }

    case E_NEON: {  // defekte Leuchtstoffröhre
      // Phasen: 0 an, 1 Störung (flackert), 2 aus, 3 Zündversuch
      if ((int32_t)(t - z.neonBis) >= 0) {
        switch (z.neonPhase) {
          case 0:
            z.neonPhase = 1;
            z.neonBis = t + (uint32_t)((300 + 1200 * zufall()) / s);
            break;
          case 1:
            z.neonPhase = zufall() < 0.5f ? 2 : 0;
            z.neonBis = t + (uint32_t)(z.neonPhase == 2 ? (400 + 2100 * zufall()) / s
                                                         : (2500 + 9000 * zufall()) / s);
            break;
          case 2:
            z.neonPhase = 3;
            z.neonBis = t + (uint32_t)((300 + 600 * zufall()) / s);
            break;
          default:
            z.neonPhase = 0;
            z.neonBis = t + (uint32_t)((3000 + 12000 * zufall()) / s);
            break;
        }
      }
      switch (z.neonPhase) {
        case 0:
          return a * (0.96f + 0.04f * hashRausch(i, t / 40, 11));
        case 1:
          return hashRausch(i, t / 35, 13) > 0.45f ? a : b * 0.3f;
        case 2:
          return b * 0.3f;
        default:
          return hashRausch(i, t / 60, 17) > 0.7f ? a * 0.8f : b * 0.3f;
      }
    }

    case E_ZUFALL: {  // langsames, zufälliges Auf- und Abblenden
      if ((int32_t)(t - z.naechsterWechsel) >= 0) {
        z.zufallZiel = zufall();
        z.naechsterWechsel = t + (uint32_t)((1000.0f + 3000.0f * zufall()) / s);
      }
      z.zufallWert += (z.zufallZiel - z.zufallWert) * (1.0f - expf(-dt * 1.8f * s));
      return b + (a - b) * z.zufallWert;
    }

    case E_SPEKTRUM: {
      // Zonen über die Bänder verteilt: links Bass, Mitte Mitten, rechts Höhen (ohne Pi: alle = Musikpegel)
      float v;
      if (hatSpektrum(t)) {
        float pos = n > 1 ? i / (n - 1) : 0.5f;
        v = pos < 0.5f ? band_[0] + (band_[1] - band_[0]) * pos * 2 : band_[1] + (band_[2] - band_[1]) * (pos - 0.5f) * 2;
        v = clamp01(v + schlag_ * 0.25f);
      } else {
        v = clamp01(musikNorm_ + beat_ * 0.3f);
      }
      return b + (a - b) * v;
    }

    case E_BEAT: {
      // Schlag-Blitz: jede Zone ist bei etwa 2 von 3 Schlägen dabei (wechselndes Muster), Tempo = Abklingen
      float h = hatSpektrum(t) ? schlag_ : beat_;
      bool dabei = hashRausch(schlaege_, i, 77) > 0.33f || n == 1;
      return b + (a - b) * (dabei ? h : h * 0.15f);
    }

    case E_MUSIK: {
      // jede Zone bekommt die Hüllkurve leicht verzögert -> Welle über das Netz
      int verz = i * (1 + z.p.tempo / 20);
      if (verz >= HUELLE_HISTORIE) verz = HUELLE_HISTORIE - 1;
      int idx = (histPos_ - 1 - verz + HUELLE_HISTORIE * 2) % HUELLE_HISTORIE;
      float v = clamp01(hist_[idx] + beat_ * 0.35f);
      return b + (a - b) * v;
    }
  }
  return 0;
}

void Lichtmaschine::berechne(uint32_t t, float* ausgabe) {
  float dt = letzteZeit_ ? (t - letzteZeit_) / 1000.0f : 0.01f;
  if (dt < 0 || dt > 0.5f) dt = 0.01f;
  letzteZeit_ = t;

  // Ohne Hüllkurve (Sound-ESP aus) alles langsam auf 0
  if (t - letzteHuelle_ > 500) {
    musikNorm_ *= expf(-dt * 4);
    fxNorm_ *= expf(-dt * 8);
  }
  beat_ *= expf(-dt * 7.0f);
  // Abklingen der Pi-Schläge: Tempo der ersten Beat-Zone bestimmt die Zeit (langsam ~0,5 s, schnell ~0,1 s)
  float schlagTempo = 1;
  for (int i = 0; i < n_; i++)
    if (z_[i].p.effekt == E_BEAT) {
      schlagTempo = tempoFaktor(z_[i].p.tempo);
      break;
    }
  schlag_ *= expf(-dt * 6.0f * schlagTempo);
  if (!hatSpektrum(t))
    for (float& b : band_) b *= expf(-dt * 4);

  // Gewitter: gemeinsamer Einschlag über benachbarte Zonen
  uint32_t gewitterZonen = 0;
  float sturmTempo = 1;
  for (int i = 0; i < n_; i++)
    if (z_[i].p.effekt == E_GEWITTER) {
      gewitterZonen |= 1u << i;
      sturmTempo = tempoFaktor(z_[i].p.tempo);
    }
  if (gewitterZonen && (int32_t)(t - sturm_.naechsterEinschlag) >= 0) {
    // Mitte zufällig unter den Gewitter-Zonen wählen, Breite 1..3
    int kandidaten[MAX_ZONEN];
    int k = 0;
    for (int i = 0; i < n_; i++)
      if (gewitterZonen & (1u << i)) kandidaten[k++] = i;
    int mitte = kandidaten[(int)(zufall() * k) % k];
    int breite = 1 + (int)(zufall() * 2.99f);
    uint32_t m = 0;
    for (int i = mitte - breite / 2; i < mitte - breite / 2 + breite; i++)
      if (i >= 0 && i < n_) m |= 1u << i;
    sturm_.maske = m & gewitterZonen;
    sturm_.start = t;
    sturm_.seed = (uint32_t)(zufall() * 4294967000.0f) | 1;
    sturm_.naechsterEinschlag = t + (uint32_t)((2500.0f + 11000.0f * zufall()) / sturmTempo);
  }

  // Master weich nachführen
  float masterSoll = g_.an ? g_.hell / 100.0f : 0.0f;
  masterIst_ += (masterSoll - masterIst_) * (1.0f - expf(-dt * 6.0f));

  const float mr = g_.musikReaktiv / 100.0f;

  for (int i = 0; i < n_; i++) {
    ZonenZustand& z = z_[i];
    float v = basisWert(i, z, t, dt);
    bool test = false;  // Zonen-Test läuft unabhängig von An/Aus und Master
    if (z.blende) {
      float k = (t - z.blendeStart) / 600.0f;
      if (k >= 1.0f || k < 0)
        z.blende = false;
      else
        v = z.vonWert + (v - z.vonWert) * k;
    }
    z.wert = v;

    if (mr > 0) v *= (1.0f - mr) + mr * clamp01(0.15f + musikNorm_ * 0.85f + beat_ * 0.3f);

    // Einmal-Aktionen
    for (int k = 0; k < MAX_OVERLAYS; k++) {
      Overlay& o = o_[k];
      if (o.typ == A_KEINE) continue;
      if ((int32_t)(t - o.start) < 0) continue;
      uint32_t seit = t - o.start;
      if (seit >= o.dauer) continue;  // wird unten aufgeräumt
      bool drin = (o.maske >> i) & 1u;

      switch (o.typ) {
        case A_TEST:
          v = drin ? o.staerke : 0;
          test = true;
          break;
        case A_FLACKERN:
          if (drin) {
            // schnelles, unruhiges Flackern, folgt zusätzlich der Effekt-Lautstärke
            float r = hashRausch(o.seed + i * 7, t / 45, 21);
            float env = 0.45f + 0.55f * clamp01(fxNorm_ * 1.6f);
            float ausklang = seit > o.dauer * 0.8f ? 1.0f - (seit - o.dauer * 0.8f) / (o.dauer * 0.2f) : 1.0f;
            float b = o.staerke * env * (r > 0.35f ? 0.55f + 0.45f * r : 0.08f) * clamp01(ausklang);
            float mitte = v * (1.0f - clamp01(ausklang) * 0.7f);
            v = b > mitte ? b : mitte;
          }
          break;
        case A_BLITZ:
          if (drin) {
            float b = o.staerke * blitzMuster(o.seed + (i & 1) * 17, seit, o.dauer);
            if (b > v) v = b;
          }
          break;
        case A_ENV_BLITZ:
          if (drin) {
            float x = clamp01((fxNorm_ - 0.12f) / 0.55f);
            float knistern = 0.55f + 0.45f * hashRausch(o.seed + i, t / 30, 5);
            float b = o.staerke * powf(x, 1.6f) * knistern;
            if (b > v) v = b;
          }
          break;
        case A_SCHRECK:
          if (drin) {
            uint32_t dunkel = o.param ? o.param : 1200;
            if (dunkel >= o.dauer) dunkel = o.dauer / 2;
            if (seit < dunkel) {
              v = 0;
            } else {
              float s2 = (float)(seit - dunkel);
              float rest = (float)(o.dauer - dunkel);
              float flacker = 0.6f + 0.4f * hashRausch(o.seed + i, t / 40, 9);
              float knall = o.staerke * expf(-s2 / (rest * 0.35f)) * flacker;
              float zurueck = v * (s2 / rest);
              v = knall > zurueck ? knall : zurueck;
            }
          }
          break;
        case A_ENV_PULS:
          if (drin) {
            float b = o.staerke * powf(clamp01(fxNorm_ * 1.4f), 1.3f);
            if (b > v) v = b;
          }
          break;
        case A_BLACKOUT:
          if (drin) {
            uint32_t ausblend = o.dauer > 800 ? 400 : o.dauer / 2;
            if (seit > o.dauer - ausblend)
              v *= (float)(seit - (o.dauer - ausblend)) / ausblend;
            else
              v = 0;
          }
          break;
        case A_WELLE:
          if (drin) {
            float x = (n_ + 1.0f) * seit / (float)o.dauer - 1.0f;
            float d = (float)i - x;
            float b = o.staerke * expf(-d * d / (2.0f * 0.5f * 0.5f));
            if (b > v) v = b;
          }
          break;
        case A_ALLE_AN:
          if (drin) {
            float k2 = seit < o.dauer * 0.7f ? 1.0f : 1.0f - (seit - o.dauer * 0.7f) / (o.dauer * 0.3f);
            float b = o.staerke * clamp01(k2);
            if (b > v) v = b;
          }
          break;
        case A_PULS:
          if (drin) {
            float sn = sinf(PI_F * 3.0f * seit / (float)o.dauer);
            float b = o.staerke * sn * sn;
            if (b > v) v = b;
          }
          break;
      }
    }

    float zm = g_.zoneMax[i] / 100.0f;
    ausgabe[i] = clamp01(v * (test ? 1.0f : masterIst_) * zm);
  }

  // abgelaufene Aktionen aufräumen
  for (auto& o : o_)
    if (o.typ != A_KEINE && (int32_t)(t - o.start) >= 0 && t - o.start >= o.dauer) o.typ = A_KEINE;
}

}  // namespace spuk
