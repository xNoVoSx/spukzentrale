/* Spukzentrale – Effekt-Engine des Licht-ESP als JavaScript (ES-Modul)
 *
 * 1:1-Portierung von firmware/licht/src/effekte.{h,cpp} (und den Aktionen aus firmware/common/protokoll.h) für das
 * virtuelle Licht-Gerät im Browser. Ziel: Bild für Bild dieselben Zonenwerte wie die C++-Engine.
 *
 * Rechenregeln, damit das Ergebnis gleich bleibt:
 *  - uint32 (Zeit, Zufall, Hash) mit `>>> 0` und Math.imul.
 *  - float32: jede einzelne Operation wird mit Math.fround gerundet (f(a * b), f(a + b) …). Für +, −, ×, ÷ ist
 *    „in double rechnen, dann auf float runden“ beweisbar identisch mit echter float-Arithmetik.
 *  - expf/powf/sinf/cosf: f(Math.exp(x)) usw. Die C-Bibliotheken runden in seltenen Grenzfällen um 1 ULP anders;
 *    der Abgleich (firmware/licht/test/vergleich_js.sh) prüft das gegen die C++-Engine am PC.
 *
 * Kein Build-Schritt, keine Abhängigkeiten – läuft im Browser und in Node.
 */

const f = Math.fround;

export const MAX_ZONEN = 6;
const MAX_OVERLAYS = 6;
const HUELLE_HISTORIE = 64;
const MAX_WELLEN = 4;

// Effekte (Enum-Reihenfolge wie effekte.h)
export const EFFEKTE = Object.freeze([
  "aus", "an", "flackern", "feuer", "atmen", "herzschlag", "strobo", "gewitter", "lauflicht", "wandern",
  "pingpong", "neon", "zufall", "musik", "spektrum", "beat",
]);
const [E_AUS, E_AN, E_FLACKERN, E_FEUER, E_ATMEN, E_HERZSCHLAG, E_STROBO, E_GEWITTER, E_LAUFLICHT, E_WANDERN,
  E_PINGPONG, E_NEON, E_ZUFALL, E_MUSIK, E_SPEKTRUM, E_BEAT] = EFFEKTE.keys();
const E_ANZAHL = EFFEKTE.length;

// Aktionen (Enum-Reihenfolge wie protokoll.h, "keine" = 0)
export const AKTIONEN = Object.freeze([
  "keine", "blitz", "env_blitz", "schreck", "blackout", "welle", "alle_an", "puls", "test", "flackern", "env_puls",
]);
const [A_KEINE, A_BLITZ, A_ENV_BLITZ, A_SCHRECK, A_BLACKOUT, A_WELLE, A_ALLE_AN, A_PULS, A_TEST, A_FLACKERN,
  A_ENV_PULS] = AKTIONEN.keys();
const A_ANZAHL = AKTIONEN.length;

// Flags der Hüllkurve (protokoll.h)
export const FLAG_MUSIK_AKTIV = 1;
export const FLAG_FX_AKTIV = 2;
export const FLAG_SERVER = 4;

export function effektName(e) { return e >= 0 && e < E_ANZAHL ? EFFEKTE[e] : "flackern"; }
/** unbekannt -> flackern (wie C++) */
export function effektVonName(name) {
  const i = EFFEKTE.indexOf(name);
  return i < 0 ? E_FLACKERN : i;
}
export function aktionName(a) { return a >= 0 && a < A_ANZAHL ? AKTIONEN[a] : "keine"; }
/** unbekannt -> keine (0) */
export function aktionVonName(name) {
  const i = AKTIONEN.indexOf(name);
  return i < 0 ? A_KEINE : i;
}

// ---------------------------------------------------------------------------------------------------------------
// float32-Konstanten (C++: ...f-Literale)
const PI_F = f(3.14159265);
const PI_2 = f(2 * PI_F);
const PI_3 = f(PI_F * 3);
const F = (x) => f(x);
const K = {
  c0_01: F(0.01), c0_05: F(0.05), c0_30: F(0.30), c0_25: F(0.25), c0_72: F(0.72), c0_28: F(0.28),
  c0_75: F(0.75), c0_85: F(0.85), c0_15: F(0.15), c0_1: F(0.1), c0_035: F(0.035), c0_22: F(0.22), c0_65: F(0.65),
  c0_8: F(0.8), c0_2: F(0.2), c1_2: F(1.2), c0_55: F(0.55), c1_6: F(1.6), c0_96: F(0.96), c0_04: F(0.04),
  c0_45: F(0.45), c0_3: F(0.3), c0_7: F(0.7), c1_8: F(1.8), c1_4: F(1.4), c0_6: F(0.6), c0_4: F(0.4),
  c0_35: F(0.35), c0_12: F(0.12), c0_9985: F(0.9985), c1_35: F(1.35), c0_08: F(0.08), c0_82: F(0.82),
  c0_18: F(0.18), c3_99: F(3.99), c0_33: F(0.33), c2_99: F(2.99), c4294967000: F(4294967000),
  c1_1: F(1.1), c0_9: F(0.9),
};
const WANDERN_NENNER = f(f(2 * K.c0_55) * K.c0_55);  // 2.0f * 0.55f * 0.55f (vom Compiler in float gefaltet)

const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);
const fexp = (x) => f(Math.exp(x));
const fpow = (x, y) => f(Math.pow(x, y));
const fcos = (x) => f(Math.cos(x));
const fsin = (x) => f(Math.sin(x));
const u32 = (x) => Math.trunc(x) >>> 0;  // (uint32_t)float wie g++ auf x86-64 (2^32 -> 0)
const u8 = (x) => (x | 0) & 0xff;        // Zuweisung an uint8_t

// Deterministisches Rauschen aus Ganzzahlen
function mische(x) {
  x >>>= 0;
  x = (x ^ (x >>> 16)) >>> 0;
  x = Math.imul(x, 0x7feb352d) >>> 0;
  x = (x ^ (x >>> 15)) >>> 0;
  x = Math.imul(x, 0x846ca68b) >>> 0;
  x = (x ^ (x >>> 16)) >>> 0;
  return x;
}
function hashRausch(a, b, c) {
  const x = (Math.imul(a >>> 0, 0x9e3779b1) ^ mische((b + 0x85ebca6b) >>> 0) ^ Math.imul(c >>> 0, 0xc2b2ae35)) >>> 0;
  return f((mische(x) & 0xffffff) / 16777215);
}
function sanftRausch(seed, t, periodeMs) {
  t >>>= 0;
  const k = Math.floor(t / periodeMs);
  let fr = f(f(t - k * periodeMs) / periodeMs);
  fr = f(f(fr * fr) * f(3 - f(2 * fr)));
  return f(f(hashRausch(seed, k, 3) * f(1 - fr)) + f(hashRausch(seed, (k + 1) >>> 0, 3) * fr));
}

function tempoFaktor(tempo) {
  // 0 -> 0,25x  |  50 -> 1x  |  100 -> 4x
  return f(K.c0_25 * fpow(16, f((tempo > 100 ? 100 : tempo) / 100)));
}

// Blitzfolge aus einem Startwert: 1–4 Blitze mit Nachglühen
function blitzMuster(seed, seit, dauer) {
  seit >>>= 0;
  if (seit >= dauer) return 0;
  let r = (seed | 1) >>> 0;
  const naechste = () => {
    r = (r ^ (r << 13)) >>> 0;
    r = (r ^ (r >>> 17)) >>> 0;
    r = (r ^ (r << 5)) >>> 0;
    return f((r & 0xffff) / 65535);
  };
  const anzahl = 1 + Math.trunc(f(naechste() * K.c3_99));
  let pos = 0;
  for (let k = 0; k < anzahl; k++) {
    const an = (30 + u32(f(naechste() * 90))) >>> 0;
    const pause = (50 + u32(f(naechste() * 170))) >>> 0;
    const staerke = f((k === 0 ? K.c0_75 : K.c0_55) + f(naechste() * K.c0_45));
    if (seit < pos) return 0;
    if (seit < pos + an) return clamp01(staerke);
    if (seit < pos + an + pause) {
      const s = f(seit - pos - an);
      return f(f(staerke * K.c0_18) * fexp(f(-s / 60)));
    }
    pos += an + pause;
  }
  return 0;
}

function neueZone() {
  return {
    p: { effekt: E_FLACKERN, tempo: 50, staerke: 80, basis: 5 },
    wert: 0, zufallZiel: 0, zufallWert: 0, naechsterWechsel: 0,
    neonPhase: 0, neonBis: 0,
    vonWert: 0, blendeStart: 0, blende: false,
  };
}
const neuesOverlay = () => ({ typ: A_KEINE, start: 0, dauer: 0, maske: 0, staerke: 1, param: 0, seed: 0 });

export class Lichtmaschine {
  constructor(zonen) {
    zonen = Math.trunc(zonen);
    this._n = zonen < 1 ? 1 : zonen > MAX_ZONEN ? MAX_ZONEN : zonen;
    this._z = Array.from({ length: MAX_ZONEN }, neueZone);
    /** wie GlobalParam: an, hell (0..100), musikReaktiv (0..100), zoneMax[6] (0..100), stroboMaxHz */
    this.global = { an: true, hell: 80, musikReaktiv: 0, zoneMax: [100, 100, 100, 100, 100, 100], stroboMaxHz: 3.0 };
    this._o = Array.from({ length: MAX_OVERLAYS }, neuesOverlay);
    this._sturm = { naechsterEinschlag: 0, start: 0, maske: 0, blitze: 0, seed: 1 };
    this._rng = 0x12345678;
    this._letzteZeit = 0;
    this._masterIst = 0;
    // Audio
    this._musikNorm = 0; this._fxNorm = 0;
    this._musikMax = 40; this._musikSchnitt = 0; this._beat = 0;
    this._hist = new Float32Array(HUELLE_HISTORIE);
    this._histPos = 0;
    this._letzteHuelle = 0;
    this._band = [0, 0, 0];
    this._schlag = 0;
    this._schlaege = 0;
    this._letztesSpektrum = 0;
    this._wellen = Array.from({ length: MAX_WELLEN }, () => ({ start: 0, staerke: 0 }));
    this._wellePos = 0;
    this._energie = 0;
  }

  get zonen() { return this._n; }
  get musikNorm() { return this._musikNorm; }
  get fxNorm() { return this._fxNorm; }

  /** Kopie der Zonen-Parameter {effekt (Zahl), tempo, staerke, basis} */
  zone(i) { return { ...this._z[i].p }; }

  /**
   * Parameter setzen; Effektwechsel werden weich überblendet (600 ms). Fehlende Felder behalten den aktuellen Wert
   * (wie wendeZustandAn in main.cpp: zone(i) holen, Felder überschreiben, setzeZone). effekt: Zahl oder Name.
   */
  setzeZone(i, p) {
    if (i < 0 || i >= this._n) return;
    const z = this._z[i];
    const alt = z.p;
    let effekt = p.effekt ?? alt.effekt;
    if (typeof effekt === "string") effekt = effektVonName(effekt);
    const neu = {
      effekt: u8(effekt),
      tempo: u8(p.tempo ?? alt.tempo),
      staerke: u8(p.staerke ?? alt.staerke),
      basis: u8(p.basis ?? alt.basis),
    };
    if (alt.effekt !== neu.effekt) {
      z.vonWert = z.wert;
      z.blendeStart = this._letzteZeit;
      z.blende = true;
      z.naechsterWechsel = 0;
      z.neonPhase = 0;
      z.neonBis = 0;
    }
    z.p = neu;
  }

  _zufall() {
    let r = this._rng;
    r = (r ^ (r << 13)) >>> 0;
    r = (r ^ (r >>> 17)) >>> 0;
    r = (r ^ (r << 5)) >>> 0;
    this._rng = r;
    return f((r & 0xffffff) / 16777215);
  }

  /**
   * Einmal-Aktion. typ: Zahl (oder Name), zonenMaske: Bit i = Zone i (0 = alle), verzMs: Startverzögerung,
   * dauerMs: Dauer (0 -> 800), staerke: 0..100, param: aktionsabhängig (schreck: Dunkelzeit in ms).
   */
  aktion(typ, jetztMs, verzMs, dauerMs, zonenMaske, staerke, param = 0) {
    if (typeof typ === "string") typ = aktionVonName(typ);
    typ = u8(typ);
    staerke = u8(staerke);
    jetztMs >>>= 0; verzMs >>>= 0; dauerMs >>>= 0; zonenMaske >>>= 0; param >>>= 0;
    if (typ === A_KEINE || typ >= A_ANZAHL) return;
    if (dauerMs === 0) dauerMs = 800;
    let platz = -1, aeltester = 0xffffffff, aeltesterIdx = 0;
    for (let k = 0; k < MAX_OVERLAYS; k++) {
      if (this._o[k].typ === A_KEINE) { platz = k; break; }
      if (this._o[k].start < aeltester) { aeltester = this._o[k].start; aeltesterIdx = k; }
    }
    if (platz < 0) platz = aeltesterIdx;
    const o = this._o[platz];
    o.typ = typ;
    o.start = (jetztMs + verzMs) >>> 0;
    o.dauer = dauerMs;
    o.maske = zonenMaske ? zonenMaske : 0xffffffff;
    o.staerke = f((staerke > 100 ? 100 : staerke) / 100);
    o.param = param;
    o.seed = (this._rng ^ Math.imul(jetztMs, 2654435761)) >>> 0;
    this._zufall();
  }

  aktionenStoppen() {
    for (const o of this._o) o.typ = A_KEINE;
  }

  /** Hüllkurve vom Sound-Gerät (musik, fx: 0..255), ca. 100x/s */
  huelle(musik, fx, jetztMs) {
    musik = u8(musik); fx = u8(fx);
    this._letzteHuelle = jetztMs >>> 0;
    const roh = musik;
    this._musikMax = f(this._musikMax * K.c0_9985);
    if (roh > this._musikMax) this._musikMax = roh;
    if (this._musikMax < 25) this._musikMax = 25;
    const norm = clamp01(f(roh / this._musikMax));
    this._musikNorm = f(f(this._musikNorm * K.c0_4) + f(norm * K.c0_6));
    if (this._musikNorm > f(f(this._musikSchnitt * K.c1_35) + K.c0_08) && this._beat < K.c0_25) this._beat = 1;
    this._musikSchnitt = f(f(this._musikSchnitt * K.c0_96) + f(this._musikNorm * K.c0_04));
    this._fxNorm = f(fx / 255);
    this._hist[this._histPos] = this._musikNorm;
    this._histPos = (this._histPos + 1) % HUELLE_HISTORIE;
  }

  /** Spektrum vom Pi (0..255 je Band, beat > 0 = Schlag) */
  spektrum(bass, mitte, hoehen, beat, jetztMs) {
    jetztMs >>>= 0;
    beat = u8(beat);
    this._letztesSpektrum = jetztMs;
    const roh = [u8(bass), u8(mitte), u8(hoehen)];
    for (let k = 0; k < 3; k++) {
      const v = f(roh[k] / 255);
      this._band[k] = v > this._band[k] ? v : f(f(this._band[k] * K.c0_82) + f(v * K.c0_18));
    }
    if (beat) {
      const st = f(K.c0_45 + f(K.c0_55 * f(beat / 255)));
      if (st > this._schlag) this._schlag = st;
      this._schlaege = (this._schlaege + 1) >>> 0;
      this._wellen[this._wellePos] = { start: jetztMs, staerke: st };
      this._wellePos = (this._wellePos + 1) % MAX_WELLEN;
    }
  }

  _hatSpektrum(t) {
    return this._letztesSpektrum !== 0 && ((t - this._letztesSpektrum) >>> 0) < 500;
  }

  _basisWert(i, z, t, dt) {
    const a = f(z.p.staerke / 100);
    const b = f(z.p.basis / 100);
    const s = tempoFaktor(z.p.tempo);
    const ts = f(f(t) / 1000);
    const n = this._n;
    const lin = (x) => f(b + f(f(a - b) * x));  // b + (a - b) * x

    switch (z.p.effekt) {
      case E_AUS:
        return 0;

      case E_AN:
        return a;

      case E_FLACKERN: {
        if (((t - z.naechsterWechsel) | 0) >= 0) {
          const r = this._zufall();
          z.zufallZiel = r < K.c0_05 ? f(K.c0_30 + f(K.c0_25 * this._zufall()))
            : f(K.c0_72 + f(K.c0_28 * this._zufall()));
          z.naechsterWechsel = (t + u32(f(f(45 + f(110 * this._zufall())) / s))) >>> 0;
        }
        z.zufallWert = f(z.zufallWert + f(f(z.zufallZiel - z.zufallWert) * f(1 - fexp(f(f(-dt * 16) * s)))));
        return lin(z.zufallWert);
      }

      case E_FEUER: {
        if (((t - z.naechsterWechsel) | 0) >= 0) {
          z.zufallZiel = f(K.c0_25 + f(K.c0_75 * this._zufall()));
          z.naechsterWechsel = (t + u32(f(f(20 + f(70 * this._zufall())) / s))) >>> 0;
        }
        z.zufallWert = f(z.zufallWert + f(f(z.zufallZiel - z.zufallWert) * f(1 - fexp(f(f(-dt * 28) * s)))));
        const fein = f(K.c0_85 + f(K.c0_15 * hashRausch(i, Math.floor(t / 25), 7)));
        return f(b + f(f(f(a - b) * z.zufallWert) * fein));
      }

      case E_ATMEN: {
        const c = f(0.5 - f(0.5 * fcos(f(f(f(PI_2 * ts) * K.c0_1) * s))));
        return f(b + f(f(f(a - b) * c) * c));
      }

      case E_HERZSCHLAG: {
        const periode = f(K.c1_1 / s);
        const ph = f((ts % periode) / periode);
        const puls = (x, c) => {
          const d = f(f(x - c) / K.c0_035);
          return fexp(f(-d * d));
        };
        const v = f(puls(ph, K.c0_05) + f(K.c0_65 * puls(ph, K.c0_22)));
        return lin(clamp01(v));
      }

      case E_STROBO: {
        const smh = f(this.global.stroboMaxHz);
        const maxHz = smh < 0.5 ? f(0.5) : smh;
        let hz = f(0.5 + f(f(maxHz - 0.5) * f(z.p.tempo / 100)));
        if (hz > maxHz) hz = maxHz;
        const periode = u32(f(1000 / hz));
        return t % periode < 45 ? a : b;
      }

      case E_GEWITTER: {
        const grund = f(b * f(K.c0_8 + f(K.c0_2 * hashRausch(i, Math.floor(t / 90), 3))));
        let blitz = 0;
        if (this._sturm.maske & (1 << i))
          blitz = blitzMuster((this._sturm.seed + (i & 1) * 31) >>> 0, (t - this._sturm.start) >>> 0, 1200);
        const v = f(a * blitz);
        return v > grund ? v : grund;
      }

      case E_LAUFLICHT: {
        const pos = f(f(f(ts * s) * K.c1_2) % n);
        let d = Math.abs(f(i - pos));
        if (d > f(n / 2)) d = f(n - d);
        const v = clamp01(f(1 - d));
        return lin(fpow(v, 1.5));
      }

      case E_WANDERN: {
        const x = f((n - 1) * f(0.5 - f(0.5 * fcos(f(f(f(PI_F * ts) * s) * K.c0_25)))));
        const d = f(i - x);
        const v = fexp(f(f(-d * d) / WANDERN_NENNER));
        return lin(v);
      }

      case E_PINGPONG: {
        if (n < 2) return lin(f(0.5 - f(0.5 * fcos(f(f(PI_2 * ts) * s)))));
        const laenge = f(2 * (n - 1));
        const tri = f(f(f(ts * s) * K.c1_6) % laenge);
        const x = tri <= n - 1 ? tri : f(laenge - tri);
        const v = clamp01(f(1 - Math.abs(f(i - x))));
        return lin(v);
      }

      case E_NEON: {
        if (((t - z.neonBis) | 0) >= 0) {
          switch (z.neonPhase) {
            case 0:
              z.neonPhase = 1;
              z.neonBis = (t + u32(f(f(300 + f(1200 * this._zufall())) / s))) >>> 0;
              break;
            case 1:
              z.neonPhase = this._zufall() < 0.5 ? 2 : 0;
              z.neonBis = (t + u32(z.neonPhase === 2 ? f(f(400 + f(2100 * this._zufall())) / s)
                : f(f(2500 + f(9000 * this._zufall())) / s))) >>> 0;
              break;
            case 2:
              z.neonPhase = 3;
              z.neonBis = (t + u32(f(f(300 + f(600 * this._zufall())) / s))) >>> 0;
              break;
            default:
              z.neonPhase = 0;
              z.neonBis = (t + u32(f(f(3000 + f(12000 * this._zufall())) / s))) >>> 0;
              break;
          }
        }
        switch (z.neonPhase) {
          case 0:
            return f(a * f(K.c0_96 + f(K.c0_04 * hashRausch(i, Math.floor(t / 40), 11))));
          case 1:
            return hashRausch(i, Math.floor(t / 35), 13) > K.c0_45 ? a : f(b * K.c0_3);
          case 2:
            return f(b * K.c0_3);
          default:
            return hashRausch(i, Math.floor(t / 60), 17) > K.c0_7 ? f(a * K.c0_8) : f(b * K.c0_3);
        }
      }

      case E_ZUFALL: {
        if (((t - z.naechsterWechsel) | 0) >= 0) {
          z.zufallZiel = this._zufall();
          z.naechsterWechsel = (t + u32(f(f(1000 + f(3000 * this._zufall())) / s))) >>> 0;
        }
        z.zufallWert = f(z.zufallWert + f(f(z.zufallZiel - z.zufallWert) * f(1 - fexp(f(f(-dt * K.c1_8) * s)))));
        return lin(z.zufallWert);
      }

      case E_SPEKTRUM: {
        let v;
        const band = this._band;
        if (this._hatSpektrum(t)) {
          const pos = n > 1 ? f(i / (n - 1)) : 0.5;
          v = pos < 0.5 ? f(band[0] + f(f(f(band[1] - band[0]) * pos) * 2))
            : f(band[1] + f(f(f(band[2] - band[1]) * f(pos - 0.5)) * 2));
          v = clamp01(f(v + f(this._schlag * K.c0_25)));
        } else {
          v = clamp01(f(this._musikNorm + f(this._beat * K.c0_3)));
        }
        return lin(v);
      }

      case E_BEAT: {
        const h = this._hatSpektrum(t) ? this._schlag : this._beat;
        const dabei = hashRausch(this._schlaege, i, 77) > K.c0_33 || n === 1;
        return lin(dabei ? h : f(h * K.c0_15));
      }

      case E_MUSIK: {
        if (this._hatSpektrum(t)) {
          const pos = n > 1 ? f(i / (n - 1)) : 0.5;
          const abstand = f(Math.abs(f(pos - 0.5)) * 2);
          const laufMs = f(140 / s), abklingMs = f(260 / s);
          let welle = 0;
          for (const w of this._wellen) {
            if (w.staerke <= 0) continue;
            const seit = f(f((t - w.start) | 0) - f(abstand * laufMs));
            if (seit < 0 || seit > f(abklingMs * 6)) continue;
            const anstieg = seit < 25 ? f(seit / 25) : 1;
            const v = f(f(w.staerke * anstieg) * fexp(f(-seit / abklingMs)));
            if (v > welle) welle = v;
          }
          const grund = f(fpow(this._energie, K.c1_4) * f(K.c0_7 + f(K.c0_3 * sanftRausch(i * 131 + 17, t, 900))));
          const r = sanftRausch(i * 977 + 5, t, 90);
          const b2 = this._band[2];
          const funkeln = f(f(f(b2 * b2) * clamp01(f(f(r - K.c0_6) / K.c0_4))) * K.c0_8);
          const v = clamp01(f(f(f(grund * K.c0_8) + f(welle * K.c0_85)) + funkeln));
          return lin(v);
        }
        let verz = i * (1 + Math.trunc(z.p.tempo / 20));
        if (verz >= HUELLE_HISTORIE) verz = HUELLE_HISTORIE - 1;
        const idx = (this._histPos - 1 - verz + HUELLE_HISTORIE * 2) % HUELLE_HISTORIE;
        const v = clamp01(f(this._hist[idx] + f(this._beat * K.c0_35)));
        return lin(v);
      }
    }
    return 0;
  }

  /** Berechnet alle Zonen; Ergebnis linear 0..1 (nach Master und zoneMax, vor Gamma). */
  berechne(jetztMs, ausgabe = new Float32Array(this._n)) {
    const t = jetztMs >>> 0;
    const g = this.global;
    let dt = this._letzteZeit ? f(f((t - this._letzteZeit) >>> 0) / 1000) : K.c0_01;
    if (dt < 0 || dt > 0.5) dt = K.c0_01;
    this._letzteZeit = t;

    if (((t - this._letzteHuelle) >>> 0) > 500) {
      this._musikNorm = f(this._musikNorm * fexp(f(-dt * 4)));
      this._fxNorm = f(this._fxNorm * fexp(f(-dt * 8)));
    }
    this._beat = f(this._beat * fexp(f(-dt * 7)));
    let schlagTempo = 1;
    for (let i = 0; i < this._n; i++)
      if (this._z[i].p.effekt === E_BEAT) {
        schlagTempo = tempoFaktor(this._z[i].p.tempo);
        break;
      }
    this._schlag = f(this._schlag * fexp(f(f(-dt * 6) * schlagTempo)));
    if (!this._hatSpektrum(t))
      for (let k = 0; k < 3; k++) this._band[k] = f(this._band[k] * fexp(f(-dt * 4)));
    {
      const bd = this._band;
      const e = f(f(f(K.c0_45 * bd[0]) + f(K.c0_35 * bd[1])) + f(K.c0_2 * bd[2]));
      this._energie = f(this._energie + f(f(e - this._energie) * f(1 - fexp(f(-dt * 5)))));
    }

    // Gewitter: gemeinsamer Einschlag über benachbarte Zonen
    let gewitterZonen = 0;
    let sturmTempo = 1;
    for (let i = 0; i < this._n; i++)
      if (this._z[i].p.effekt === E_GEWITTER) {
        gewitterZonen |= 1 << i;
        sturmTempo = tempoFaktor(this._z[i].p.tempo);
      }
    const sturm = this._sturm;
    if (gewitterZonen && ((t - sturm.naechsterEinschlag) | 0) >= 0) {
      const kandidaten = [];
      for (let i = 0; i < this._n; i++) if (gewitterZonen & (1 << i)) kandidaten.push(i);
      const k = kandidaten.length;
      const mitte = kandidaten[Math.trunc(f(this._zufall() * k)) % k];
      const breite = 1 + Math.trunc(f(this._zufall() * K.c2_99));
      let m = 0;
      const von = mitte - Math.trunc(breite / 2);
      for (let i = von; i < von + breite; i++) if (i >= 0 && i < this._n) m |= 1 << i;
      sturm.maske = (m & gewitterZonen) >>> 0;
      sturm.start = t;
      sturm.seed = (u32(f(this._zufall() * K.c4294967000)) | 1) >>> 0;
      sturm.naechsterEinschlag = (t + u32(f(f(2500 + f(11000 * this._zufall())) / sturmTempo))) >>> 0;
    }

    // Master weich nachführen
    const masterSoll = g.an ? f(u8(g.hell) / 100) : 0;
    this._masterIst = f(this._masterIst + f(f(masterSoll - this._masterIst) * f(1 - fexp(f(-dt * 6)))));

    const mr = f(u8(g.musikReaktiv) / 100);

    for (let i = 0; i < this._n; i++) {
      const z = this._z[i];
      let v = this._basisWert(i, z, t, dt);
      let test = false;
      if (z.blende) {
        const k = f(f((t - z.blendeStart) >>> 0) / 600);
        if (k >= 1 || k < 0) z.blende = false;
        else v = f(z.vonWert + f(f(v - z.vonWert) * k));
      }
      z.wert = v;

      if (mr > 0)
        v = f(v * f(f(1 - mr) + f(mr * clamp01(
          f(f(K.c0_15 + f(this._musikNorm * K.c0_85)) + f(this._beat * K.c0_3))))));

      // Einmal-Aktionen
      for (let k = 0; k < MAX_OVERLAYS; k++) {
        const o = this._o[k];
        if (o.typ === A_KEINE) continue;
        if (((t - o.start) | 0) < 0) continue;
        const seit = (t - o.start) >>> 0;
        if (seit >= o.dauer) continue;
        const drin = ((o.maske >>> i) & 1) !== 0;
        const fs = f(seit), fd = f(o.dauer);

        switch (o.typ) {
          case A_TEST:
            v = drin ? o.staerke : 0;
            test = true;
            break;
          case A_FLACKERN:
            if (drin) {
              const r = hashRausch((o.seed + i * 7) >>> 0, Math.floor(t / 45), 21);
              const env = f(K.c0_45 + f(K.c0_55 * clamp01(f(this._fxNorm * K.c1_6))));
              const d08 = f(fd * K.c0_8);
              const ausklang = fs > d08 ? f(1 - f(f(fs - d08) / f(fd * K.c0_2))) : 1;
              const bw = f(f(f(o.staerke * env) * (r > K.c0_35 ? f(K.c0_55 + f(K.c0_45 * r)) : K.c0_08))
                * clamp01(ausklang));
              const mitte = f(v * f(1 - f(clamp01(ausklang) * K.c0_7)));
              v = bw > mitte ? bw : mitte;
            }
            break;
          case A_BLITZ:
            if (drin) {
              const bw = f(o.staerke * blitzMuster((o.seed + (i & 1) * 17) >>> 0, seit, o.dauer));
              if (bw > v) v = bw;
            }
            break;
          case A_ENV_BLITZ:
            if (drin) {
              const x = clamp01(f(f(this._fxNorm - K.c0_12) / K.c0_55));
              const knistern = f(K.c0_55 + f(K.c0_45 * hashRausch((o.seed + i) >>> 0, Math.floor(t / 30), 5)));
              const bw = f(f(o.staerke * fpow(x, K.c1_6)) * knistern);
              if (bw > v) v = bw;
            }
            break;
          case A_SCHRECK:
            if (drin) {
              let dunkel = o.param ? o.param : 1200;
              if (dunkel >= o.dauer) dunkel = Math.floor(o.dauer / 2);
              if (seit < dunkel) {
                v = 0;
              } else {
                const s2 = f(seit - dunkel);
                const rest = f(o.dauer - dunkel);
                const flacker = f(K.c0_6 + f(K.c0_4 * hashRausch((o.seed + i) >>> 0, Math.floor(t / 40), 9)));
                const knall = f(f(o.staerke * fexp(f(-s2 / f(rest * K.c0_35)))) * flacker);
                const zurueck = f(v * f(s2 / rest));
                v = knall > zurueck ? knall : zurueck;
              }
            }
            break;
          case A_ENV_PULS:
            if (drin) {
              const bw = f(o.staerke * fpow(clamp01(f(this._fxNorm * K.c1_4)), f(1.3)));
              if (bw > v) v = bw;
            }
            break;
          case A_BLACKOUT:
            if (drin) {
              const ausblend = o.dauer > 800 ? 400 : Math.floor(o.dauer / 2);
              if (seit > o.dauer - ausblend) v = f(v * f(f(seit - (o.dauer - ausblend)) / ausblend));
              else v = 0;
            }
            break;
          case A_WELLE:
            if (drin) {
              const x = f(f(f(f(this._n + 1) * fs) / fd) - 1);
              const d = f(i - x);
              const bw = f(o.staerke * fexp(f(f(-d * d) / 0.5)));
              if (bw > v) v = bw;
            }
            break;
          case A_ALLE_AN:
            if (drin) {
              const d07 = f(fd * K.c0_7);
              const k2 = fs < d07 ? 1 : f(1 - f(f(fs - d07) / f(fd * K.c0_3)));
              const bw = f(o.staerke * clamp01(k2));
              if (bw > v) v = bw;
            }
            break;
          case A_PULS:
            if (drin) {
              const sn = fsin(f(f(PI_3 * fs) / fd));
              const bw = f(f(o.staerke * sn) * sn);
              if (bw > v) v = bw;
            }
            break;
        }
      }

      const zm = f(u8(g.zoneMax[i] ?? 100) / 100);
      ausgabe[i] = clamp01(f(f(v * (test ? 1 : this._masterIst)) * zm));
    }

    // abgelaufene Aktionen aufräumen
    for (const o of this._o)
      if (o.typ !== A_KEINE && ((t - o.start) | 0) >= 0 && ((t - o.start) >>> 0) >= o.dauer) o.typ = A_KEINE;
    return ausgabe;
  }
}

/**
 * Werte für die Live-Vorschau wie main.cpp (sendeWerte): Ausgabe der Engine (nach Master und zoneMax, OHNE Gamma)
 * in ganzen Prozent, gerundet: (int)(wert * 100 + 0.5f). Ergebnis = Feld "v" der Nachricht {"t":"werte","v":[…]}.
 */
export function werteFuerVorschau(ausgabe) {
  return Array.from(ausgabe, (w) => Math.trunc(f(f(f(w) * 100) + 0.5)));
}
