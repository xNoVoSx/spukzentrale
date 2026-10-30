// PC-Test: Der Browser spielt den Raspberry Pi samt Licht-ESP – zum Testen ohne Hardware.
//
// Meldet sich wie der Pi zweimal beim Server an (/ws/geraet?rolle=sound bzw. licht), spielt Ambiente, Effekte und
// KI-Stimme über die Lautsprecher des PCs und rechnet das Licht mit derselben Effekt-Engine wie die Firmware
// (lichtmaschine.js, Bild für Bild gegen die C++-Engine geprüft). Läuft der echte Pi, startet der Test nicht – und
// meldet sich der Pi während des Tests, räumt der Browser das Feld.
//
// Läuft nach app.js (Modul = defer) und benutzt dessen Bausteine (S, api, bereich, esc, toast …).

import { Lichtmaschine, EFFEKTE, AKTIONEN, effektVonName, aktionVonName, werteFuerVorschau } from "./lichtmaschine.js";

const FW = "pc-test-1";
const WS = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/geraet`;

// ---------------------------------------------------------------------------
// Musik-Analyse wie pi/spukpi/analyse.py: drei Bänder (Bass/Mitte/Höhen, selbst normiert) + Schlagerkennung
// ---------------------------------------------------------------------------
const BAENDER = [[40, 160], [160, 2000], [2000, 9000]];
const SCHLAG_BAND = [40, 250];
const SCHLAG_PAUSE_S = 0.22;

class Analyse {
  constructor(analyser) {
    this.a = analyser;
    this.bin = analyser.context.sampleRate / analyser.fftSize;
    this.db = new Float32Array(analyser.frequencyBinCount);
    this.zeit = new Float32Array(analyser.fftSize);
    this.spitze = [1e-3, 1e-3, 1e-3];
    this.vorher = null;
    this.fluss = [];
    this.seitSchlag = 1;
    this.schwelle = null;
    this.zaehler = 0;
    this.letzte = [0, 0, 0];
  }

  bins([lo, hi]) {
    return [Math.max(1, Math.floor(lo / this.bin)), Math.max(2, Math.floor(hi / this.bin))];
  }

  // RMS der Musik (für die Hüllkurve), -> 0..1
  rms() {
    this.a.getFloatTimeDomainData(this.zeit);
    let s = 0;
    for (const x of this.zeit) s += x * x;
    return Math.sqrt(s / this.zeit.length);
  }

  // alle 20 ms: [bass, mitte, hoehen, beat] je 0..255
  schritt(dtS, rms) {
    this.seitSchlag += dtS;
    if (rms < 1e-4) {
      this.vorher = null;
      this.spitze = this.spitze.map((s) => s * 0.999);
      this.letzte = [0, 0, 0];
      return [0, 0, 0, 0];
    }
    this.a.getFloatFrequencyData(this.db);
    // WebAudio liefert dB relativ zur Fenstergröße – auf die Skala von numpy (rfft ohne Normierung) bringen
    const n = this.a.fftSize;
    const mag = (i) => (Number.isFinite(this.db[i]) ? 10 ** (this.db[i] / 20) * n : 0);
    const werte = BAENDER.map((b) => {
      const [lo, hi] = this.bins(b);
      let p = 0;
      for (let i = lo; i < hi; i++) p += mag(i) ** 2;
      return Math.sqrt(p);
    });
    this.spitze = this.spitze.map((s, i) => Math.max(s * 0.997, werte[i]));
    const norm = werte.map((w, i) => Math.min(1, Math.max(0, w / Math.max(this.spitze[i], 1e-6))));
    const [lo, hi] = this.bins(SCHLAG_BAND);
    const tief = [];
    for (let i = lo; i < hi; i++) tief.push(Math.log1p(mag(i)));
    let beat = 0;
    if (this.vorher) {
      let fluss = 0;
      tief.forEach((v, i) => (fluss += Math.max(v - this.vorher[i], 0)));
      this.zaehler++;
      if (this.fluss.length >= 10 && (this.schwelle === null || this.zaehler % 5 === 0)) {
        const s = [...this.fluss].sort((a, b) => a - b);
        this.schwelle = s[Math.floor(s.length / 2)] * 1.6 + 0.05;
      }
      if (this.schwelle !== null && this.fluss.length >= 10 && fluss > this.schwelle && this.seitSchlag >= SCHLAG_PAUSE_S) {
        const staerke = Math.min(1, (fluss / this.schwelle - 1) * 1.5 + 0.3);
        beat = Math.max(1, Math.floor(staerke * 255));
        this.seitSchlag = 0;
      }
      this.fluss.push(fluss);
      if (this.fluss.length > 50) this.fluss.shift();
    }
    this.vorher = tief;
    this.letzte = norm.map((x) => Math.floor(x * 255));
    return [...this.letzte, beat];
  }
}

// ---------------------------------------------------------------------------
// Das virtuelle Gerät
// ---------------------------------------------------------------------------
class PcTest {
  constructor() {
    this.laeuft = false;
    this.meldung = "";
    this.ws = { sound: null, licht: null };
    this.cfg = { master: 80, musik_vol: 60, fx_vol: 100, duck: 40, latenz_ms: 0 };
    this.lichtCfg = { gamma: 2.2, zone_max: [100, 100, 100, 100, 100, 100], strobo_max_hz: 3 };
    this.zustand = null;          // letzter Licht-Zustand vom Server
    this.stimmen = [];            // laufende Klänge: {name, art, start, dauer, quelle}
    this.puffer = new Map();      // Effekt-ID -> AudioBuffer
    this.vorschau = false;
    this.werte = [];              // 0..100 je Zone, wie die Live-Vorschau des ESP
    this.linear = [];             // 0..1 je Zone (Engine, vor Helligkeit/Gamma)
    this.overlays = [];           // laufende Aktionen für die Anzeige: {name, bis, zonen}
    this.musikSpielt = false;
    this.spektrum = [0, 0, 0, 0];
    this.huelle = [0, 0];
  }

  // ----------------------------------------------------------------- Start/Stopp
  async starten() {
    const echt = ["sound", "licht"].find((r) => S.geraete?.[r]?.verbunden && S.geraete[r].hallo?.plattform !== "browser");
    if (echt) {
      toast("Der echte Pi ist verbunden – erst ausschalten, dann testen", true);
      return;
    }
    this.ctx = new AudioContext({ latencyHint: "interactive" });
    await this.ctx.resume();
    this.master = this.ctx.createGain();
    this.master.connect(this.ctx.destination);
    // Musik: <audio> mit dem Ambiente-Stream -> Analyse (vor der Lautstärke, wie am Pi) -> Lautstärke -> Master
    this.audio = new Audio();
    this.audio.crossOrigin = "anonymous";
    this.musikQuelle = this.ctx.createMediaElementSource(this.audio);
    this.musikAnalyser = this.ctx.createAnalyser();
    this.musikAnalyser.fftSize = 2048;
    this.musikAnalyser.smoothingTimeConstant = 0;
    this.musikGain = this.ctx.createGain();
    this.musikQuelle.connect(this.musikAnalyser);
    this.musikAnalyser.connect(this.musikGain);
    this.musikGain.connect(this.master);
    this.analyse = new Analyse(this.musikAnalyser);
    // Effekte/Stimme: eigener Bus mit Spitzenmesser für die Hüllkurve
    this.fxGain = this.ctx.createGain();
    this.fxAnalyser = this.ctx.createAnalyser();
    this.fxAnalyser.fftSize = 512;
    this.fxZeit = new Float32Array(512);
    this.fxGain.connect(this.fxAnalyser);
    this.fxGain.connect(this.master);
    this.audio.addEventListener("playing", () => (this.musikSpielt = true));
    this.audio.addEventListener("pause", () => (this.musikSpielt = false));
    this.audio.addEventListener("error", () => {
      this.musikSpielt = false;
      if (this.musikSoll && this.laeuft) setTimeout(() => this.musikStarten(), 2000);
    });

    this.maschine = new Lichtmaschine(S.einstellungen.licht.zonen);
    this.t0 = performance.now();
    this.laeuft = true;
    this.meldung = "";
    this.verbinden("sound");
    this.verbinden("licht");
    this.takt = setInterval(() => this.lichtTakt(), 10);
    this.statusTakt = setInterval(() => this.status(), 2000);
    this.lautstaerke();
    zeichneTest();
  }

  stoppen(meldung = "") {
    this.laeuft = false;
    this.meldung = meldung;
    clearInterval(this.takt);
    clearInterval(this.statusTakt);
    for (const r of ["sound", "licht"]) {
      const ws = this.ws[r];
      this.ws[r] = null;
      if (ws) ws.close();
    }
    if (this.audio) {
      this.audio.pause();
      this.audio.removeAttribute("src");
    }
    this.ctx?.close();
    this.ctx = null;
    this.stimmen = [];
    this.overlays = [];
    this.werte = [];
    zeichneTest();
  }

  jetztMs() {
    return Math.floor(performance.now() - this.t0) >>> 0;
  }

  // ----------------------------------------------------------------- Verbindungen
  verbinden(rolle) {
    const ws = new WebSocket(`${WS}?rolle=${rolle}&fw=${FW}`);
    this.ws[rolle] = ws;
    ws.onopen = () => this.senden(rolle, this.hallo(rolle));
    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (rolle === "sound") this.soundNachricht(m);
      else this.lichtNachricht(m);
    };
    ws.onclose = (ev) => {
      if (this.ws[rolle] !== ws || !this.laeuft) return;
      // Ein anderes Gerät derselben Rolle hat übernommen (der echte Pi ist wieder da) -> Test beenden
      const pi = S.geraete?.[rolle]?.verbunden && S.geraete[rolle].hallo?.plattform !== "browser";
      if (pi) {
        this.stoppen("Der echte Pi hat sich gemeldet – PC-Test beendet");
        return;
      }
      this.ws[rolle] = null;
      setTimeout(() => this.laeuft && this.verbinden(rolle), 2000);
    };
  }

  senden(rolle, m) {
    const ws = this.ws[rolle];
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(m));
  }

  hallo(rolle) {
    if (rolle === "sound") {
      return {
        t: "hallo", rolle, fw: FW, plattform: "browser", ip: "Browser", mac: "",
        fx_frei_kb: 1e7, fx_gesamt_kb: 1e7, max_stimmen: 8, kann_sagen: true,
        neustart_grund: "PC-Test gestartet", neustart_normal: true,
      };
    }
    return {
      t: "hallo", rolle, fw: FW, plattform: "browser", verbindung: "browser", ip: "Browser", mac: "",
      zonen: this.maschine.zonen, effekte: EFFEKTE, aktionen: AKTIONEN.slice(1),
    };
  }

  status() {
    const ctx = this.ctx;
    this.senden("sound", {
      t: "status", musik: !!this.musikSoll, musik_verbunden: this.musikSpielt, musik_spielt: this.musikSpielt,
      musik_quelle: "stream", format_fehler: false, puffer: 100, puffer_s: 0, stimmen: this.stimmen.length,
      unterlaeufe: 0, alsa: true, alsa_xruns: 0, uart: true, sync: false,
      latenz_gemessen_ms: ctx ? Math.round((ctx.outputLatency || ctx.baseLatency || 0) * 1000) : 0,
      neustart_grund: "PC-Test gestartet", plattform: "browser",
    });
    this.senden("licht", {
      t: "status", verbindung: "browser", fps: 100, uart: true, sound_musik: this.musikSpielt,
      musik: Math.floor(this.maschine.musikNorm * 100), fx: Math.floor(this.maschine.fxNorm * 100),
      neustart_grund: "PC-Test gestartet",
    });
  }

  // ----------------------------------------------------------------- Sound
  soundNachricht(m) {
    switch (m.t) {
      case "config":
        for (const k of Object.keys(this.cfg)) if (k in m) this.cfg[k] = Number(m[k]) || 0;
        this.lautstaerke();
        break;
      case "musik":
        if (m.aktion === "play") {
          this.musikPfad = m.pfad || "/stream/ambiente";
          this.musikSoll = true;
          this.musikStarten();
        } else if (m.aktion === "sprung") {
          this.musikStarten();             // Pi verwirft seinen Vorrat – hier: Stream neu öffnen
        } else {
          this.musikSoll = false;
          this.audio.pause();
          this.audio.removeAttribute("src");
          this.audio.load();
        }
        break;
      case "fx":
        this.fxSpielen(String(m.id || ""), Number(m.vol ?? 100), m.licht);
        break;
      case "sag":
        this.sagen(String(m.pfad || ""), Number(m.vol ?? 100), m.licht);
        break;
      case "fx_stop":
        for (const s of this.stimmen) try { s.quelle.stop(); } catch { /* schon zu Ende */ }
        this.stimmen = [];
        break;
      case "fx_sync":
        // Der Browser lädt Effekte erst beim Abspielen (Vorhör-MP3) – nichts abzugleichen
        this.senden("sound", { t: "fx_sync_fertig", geladen: 0, geloescht: 0, fehler: 0, fx_frei_kb: 1e7 });
        break;
    }
  }

  musikStarten() {
    if (!this.musikSoll) return;
    this.audio.src = `${this.musikPfad}${this.musikPfad.includes("?") ? "&" : "?"}t=${Date.now()}`;
    this.audio.play().catch(() => (this.musikSpielt = false));
  }

  lautstaerke() {
    if (!this.ctx) return;
    const c = this.cfg;
    const t = this.ctx.currentTime;
    this.master.gain.setTargetAtTime(c.master / 100, t, 0.05);
    this.fxGain.gain.setTargetAtTime(c.fx_vol / 100, t, 0.05);
    const duck = this.stimmen.length ? 1 - c.duck / 100 : 1;
    // wie am Pi: schnell runter, langsam wieder hoch
    this.musikGain.gain.setTargetAtTime((c.musik_vol / 100) * duck, t, duck < 1 ? 0.03 : 0.6);
  }

  async laden(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return this.ctx.decodeAudioData(await r.arrayBuffer());
  }

  async fxSpielen(id, vol, licht) {
    try {
      if (!this.puffer.has(id)) this.puffer.set(id, await this.laden(`/api/fx/${id}/vorhoeren`));
    } catch (e) {
      this.senden("sound", { t: "log", text: `PC-Test: Effekt ${id} nicht ladbar (${e.message})` });
      return;
    }
    const f = (S.medien?.fx || []).find((x) => x.id === id);
    this.klang(this.puffer.get(id), vol, licht, f ? f.name : id, "fx");
  }

  async sagen(pfad, vol, licht) {
    if (!pfad.startsWith("/")) return;
    try {
      this.klang(await this.laden(pfad), vol, licht, "KI-Stimme", "sag");
    } catch (e) {
      this.senden("sound", { t: "log", text: `PC-Test: Ansage nicht geladen (${e.message})` });
    }
  }

  klang(puffer, vol, licht, name, art) {
    if (!this.ctx) return;
    const q = this.ctx.createBufferSource();
    q.buffer = puffer;
    const g = this.ctx.createGain();
    g.gain.value = vol / 100;
    q.connect(g);
    g.connect(this.fxGain);
    const s = { name, art, start: performance.now(), dauer: puffer.duration * 1000, quelle: q };
    this.stimmen.push(s);
    q.onended = () => {
      this.stimmen = this.stimmen.filter((x) => x !== s);
      this.lautstaerke();
    };
    q.start();
    this.lautstaerke();
    // Licht zum Sound: wie am Pi um die Ausgabe-Latenz verzögert (hier die des Browsers)
    if (licht && licht.name && licht.name !== "keine") {
      const verz = Math.round((this.ctx.outputLatency || this.ctx.baseLatency || 0) * 1000) + (Number(licht.verz_ms) || 0);
      this.lichtAktion({ ...licht, verz_ms: verz });
    }
  }

  // ----------------------------------------------------------------- Licht
  lichtNachricht(m) {
    const g = this.maschine.global;
    switch (m.t) {
      case "config":
        Object.assign(this.lichtCfg, m);
        if (Array.isArray(m.zone_max)) m.zone_max.forEach((v, i) => (g.zoneMax[i] = v));
        if (m.strobo_max_hz != null) g.stroboMaxHz = Number(m.strobo_max_hz);
        break;
      case "zustand":
        this.zustand = m;
        g.an = !!m.an;
        g.hell = Number(m.hell ?? 80);
        g.musikReaktiv = Number(m.musik_reaktiv ?? 0);
        (m.zonen || []).forEach((z, i) => {
          if (i < this.maschine.zonen) {
            this.maschine.setzeZone(i, { effekt: effektVonName(z.e), tempo: z.tempo, staerke: z.staerke, basis: z.basis });
          }
        });
        break;
      case "aktion":
        this.lichtAktion(m);
        break;
      case "stopp":
        this.maschine.aktionenStoppen();
        this.overlays = [];
        break;
      case "vorschau":
        this.vorschau = !!m.an;
        break;
    }
  }

  lichtAktion(a) {
    const typ = aktionVonName(String(a.name || ""));
    if (!typ) return;
    let maske = 0;
    for (const z of a.zonen || []) if (z >= 0 && z < 8) maske |= 1 << z;
    if (!maske) maske = (1 << this.maschine.zonen) - 1;
    const verz = Math.max(0, Number(a.verz_ms) || 0);
    const dauer = Math.max(50, Number(a.dauer_ms) || 1000);
    this.maschine.aktion(typ, this.jetztMs(), verz, dauer, maske, Number(a.staerke ?? 100), Number(a.param) || 0);
    const jetzt = performance.now();
    this.overlays.push({ name: a.name, ab: jetzt + verz, bis: jetzt + verz + dauer, zonen: a.zonen || [] });
  }

  lichtTakt() {
    if (!this.ctx) return;
    const t = this.jetztMs();
    // Hüllkurve wie der Mischer des Pi: Musik-RMS * 2 (vor Ducking), Effekte als Spitze
    const rms = this.musikSpielt ? this.analyse.rms() : 0;
    this.fxAnalyser.getFloatTimeDomainData(this.fxZeit);
    let spitze = 0;
    for (const x of this.fxZeit) spitze = Math.max(spitze, Math.abs(x));
    if (!this.stimmen.length) spitze = 0;
    this.huelle = [Math.min(255, rms * 2 * 255), Math.min(255, spitze * 255)];
    this.maschine.huelle(this.huelle[0] | 0, this.huelle[1] | 0, t);
    this.analyseTakt = (this.analyseTakt || 0) + 1;
    if (this.analyseTakt % 2 === 0) {
      this.spektrum = this.musikSpielt ? this.analyse.schritt(0.02, rms) : [0, 0, 0, 0];
      this.maschine.spektrum(...this.spektrum, t);
    }
    this.linear = Array.from(this.maschine.berechne(t));
    this.werte = werteFuerVorschau(this.linear);
    // Vorschau für die anderen Oberflächen (Startseite): wie der ESP ~20x pro Sekunde
    if (this.vorschau && this.analyseTakt % 5 === 0) this.senden("licht", { t: "werte", v: this.werte });
    const jetzt = performance.now();
    this.overlays = this.overlays.filter((o) => o.bis > jetzt);
  }
}

const test = new PcTest();
window.pcTest = test;

// ---------------------------------------------------------------------------
// Oberfläche: Reiter „Test“
// ---------------------------------------------------------------------------
let testbildStand = 0;
let testInfo = null;

async function testInfoLaden() {
  try {
    window.simulationStand = await api("GET", "/api/test/simulation");
    testInfo = await api("GET", "/api/test");
    aktualisieren(["test"]);
  } catch { /* Toast kommt von api() */ }
}

bereich("test-start", "test", ["test", "geraete"], () => {
  const echt = ["sound", "licht"].find((r) => S.geraete?.[r]?.verbunden && S.geraete[r].hallo?.plattform !== "browser");
  return `
    <div class="karte">
      <h2>🧪 PC-Test ohne Pi</h2>
      <p class="hinweis">Der Browser spielt hier den Raspberry Pi samt Licht: Musik, Effekte und KI-Stimme kommen aus den
        Lautsprechern dieses Geräts, das Licht rechnet dieselbe Effekt-Engine wie die Firmware und wird unten live gezeigt.
        Alles andere läuft echt über den Server – Zufallsereignisse, Szenen, Auslöser, KI, Regisseur.</p>
      ${test.laeuft
        ? `<div class="knopfreihe"><span class="marke orange">läuft</span><button type="button" class="klein" data-aktion="test-stopp">⏹ Test beenden</button></div>`
        : echt
          ? `<p class="hinweis warnung">Der echte Pi ist verbunden. Test geht nur, wenn er aus ist (sonst würden sich beide gegenseitig verdrängen).</p>`
          : `<div class="knopfreihe"><button type="button" class="lila" data-aktion="test-start">▶️ PC-Test starten</button></div>`}
      ${test.meldung ? `<p class="hinweis warnung">${esc(test.meldung)}</p>` : ""}
      <p class="hinweis">Tipp: Show, Ambiente und Zufallsereignisse schaltest du wie immer auf der Startseite.
        Licht und Ton sind am PC auf die Browser-Latenz abgestimmt, nicht auf die Box draußen.</p>
    </div>`;
});

bereich("test-buehne", "test", ["test", "daten"], () => {
  const n = S.einstellungen.licht.zonen;
  const namen = S.einstellungen.licht.zonen_namen;
  const farben = S.einstellungen.licht.zonen_farben;
  const { streifen } = aufbauBerechnen();
  const wand = S.einstellungen.aufbau.wand_m;
  const lichter = streifen.filter((st) => st.gueltig).map((st) => {
    const x = 20 + (st.x / wand) * 760;
    return `<g class="tb-licht" data-zone="${st.zone}" opacity="0">
        <ellipse cx="${x}" cy="150" rx="${Math.max(40, 760 / streifen.length * 0.9)}" ry="140" fill="url(#tg${st.zone})"/>
        <rect x="${x - 3}" y="60" width="6" height="180" rx="3" fill="${farben[st.zone]}"/>
      </g>`;
  }).join("");
  const verlaeufe = [...Array(n).keys()].map((z) => `<radialGradient id="tg${z}"><stop offset="0" stop-color="${farben[z]}" stop-opacity=".95"/>
      <stop offset=".5" stop-color="${farben[z]}" stop-opacity=".35"/><stop offset="1" stop-color="${farben[z]}" stop-opacity="0"/></radialGradient>`).join("");
  return `
    <div class="karte" style="padding:0;overflow:hidden">
      <svg class="test-buehne" viewBox="0 0 800 300" role="img" aria-label="Netz mit Licht, live">
        <defs>${verlaeufe}
          <pattern id="tnetz" width="8" height="8" patternUnits="userSpaceOnUse"><path d="M8 0H0V8" fill="none" stroke="#fff" stroke-opacity=".12" stroke-width=".7"/></pattern>
        </defs>
        <rect width="800" height="300" fill="#07050c"/>
        ${lichter}
        <rect x="20" y="20" width="760" height="260" fill="url(#tnetz)"/>
        <g fill="#000" opacity=".65">
          <path d="M236 270 q0-110 55-110 t55 110 l-12-14-14 14-14-14-14 14-14-14-14 14z"/>
          <path d="M536 272 l18-72 18 72z M514 200 h80 l-7 10 h-66z M554 200 q-4-46 32-60 q-18 24-14 60z"/>
        </g>
        <text id="tb-aktion" x="400" y="36" text-anchor="middle" fill="#fff" font-size="20" font-weight="700"></text>
      </svg>
      <div class="test-zonen">
        ${[...Array(n).keys()].map((z) => `
          <div class="test-zone">
            <div class="test-balken"><div id="tz-balken-${z}" style="background:${farben[z]}"></div></div>
            <b id="tz-wert-${z}">–</b>
            <span>${esc(namen[z])}</span>
            <small id="tz-effekt-${z}">–</small>
          </div>`).join("")}
      </div>
      <div class="test-musik">
        <span>Musik</span><div class="test-pegel"><div id="tm-musik"></div></div>
        <span>Bass</span><div class="test-pegel"><div id="tm-bass"></div></div>
        <span>Mitte</span><div class="test-pegel"><div id="tm-mitte"></div></div>
        <span>Höhen</span><div class="test-pegel"><div id="tm-hoehen"></div></div>
        <span>Effekt</span><div class="test-pegel"><div id="tm-fx"></div></div>
        <span>Schlag</span><div class="test-pegel"><div id="tm-beat"></div></div>
      </div>
    </div>`;
}, () => buehneAnimieren());

bereich("test-jetzt", "test", ["test", "zustand", "protokoll", "ki"], () => `
  <div class="karte">
    <h2>Was gerade passiert</h2>
    <ul class="protokoll" id="test-klaenge"></ul>
    <p class="hinweis">Musik: ${esc(S.ambiente_jetzt || "–")}${S.szene ? ` · Szene: ${esc(S.szene.name)}` : ""}
      · Licht: ${esc(S.overlay || (S.choreo ? "Choreografie – " + S.choreo : "Grundlicht"))}</p>
    ${kiStatus.regie ? `<h3>🎬 Regisseur ${S.zustand.regie_an ? "" : "(aus)"}</h3>
    <ul class="protokoll">
      ${kiStatus.regie.verlauf.slice(0, 5).map((e) => `<li><time>${zeit(e.zeit)}</time><span><b>${esc(e.name || e.effekt || "–")}</b> · ${esc(e.grund)}</span></li>`).join("") || '<li class="leer">Noch keine Entscheidung.</li>'}
    </ul>` : ""}
    <h3>Protokoll</h3>
    <ul class="protokoll">
      ${(S.protokoll || []).slice(0, 12).map((p) => `<li class="${p.art === "fehler" ? "fehler" : ""}"><time>${zeit(p.zeit)}</time><span>${esc(p.text)}</span></li>`).join("")}
    </ul>
  </div>`);

bereich("test-besuch", "test", ["test", "ki", "daten"], () => {
  const r = testInfo?.rollen || {};
  const b = kiStatus.besuch;
  const sim = window.simulationStand;
  const knopf = (rolle, an, text) => `<button type="button" class="klein" data-aktion="test-sensor" data-rolle="${rolle}" data-an="${an ? 1 : 0}" ${r[rolle] ? "" : "disabled"}>${text}</button>`;
  const e = kiStatus.verlauf?.[0];
  return `
    <div class="karte">
      <h2>Besucher spielen ${b ? `<span class="marke ${b.zustand === "leer" || b.zustand === "nachlauf" ? "" : "orange"}">${esc(b.text)}</span>` : ""}</h2>
      <p class="hinweis">Löst die Auslöser genau wie Home Assistant aus (gleiche Sperren und Regeln).</p>
      <h3>Besuch simulieren</h3>
      <p class="hinweis">Schaltet die Sensoren nach Drehbuch; die KI bekommt statt des Kamerabilds eine Beschreibung der
        Leute (zufällige Gruppe). So laufen Besuchs-Ablauf, KI-Stimme und Regisseur wie draußen.</p>
      ${sim?.laeuft
        ? `<div class="knopfreihe"><span class="marke orange">${esc(sim.drehbuecher[sim.drehbuch] || sim.drehbuch)} · ${sim.vergangen}/${Math.round(sim.dauer)} s</span>
             <button type="button" class="klein" data-aktion="test-sim-stopp">⏹ Abbrechen</button></div>
           <p class="hinweis">${esc(sim.schritt)}</p>`
        : `<div class="knopfreihe" style="flex-wrap:wrap">${Object.entries(sim?.drehbuecher || {}).map(([k, t]) =>
            `<button type="button" class="klein lila" data-aktion="test-sim" data-drehbuch="${k}">▶️ ${esc(t)}</button>`).join("")}</div>`}
      <h3>Von Hand</h3>
      <div class="test-sensoren">
        <span>🚶 Weg${r.fern ? "" : " <small>(kein Auslöser mit Rolle „Weg“)</small>"}</span>${knopf("fern", true, "Person erkannt")}${knopf("fern", false, "weg")}
        <span>🚪 Tür${r.nah ? "" : " <small>(kein Auslöser mit Rolle „Tür“)</small>"}</span>${knopf("nah", true, "Person erkannt")}${knopf("nah", false, "weg")}
        <span>🔔 Klingel${r.klingel ? "" : " <small>(kein Auslöser mit Rolle „Klingel“)</small>"}</span>${knopf("klingel", true, "drücken")}<span></span>
      </div>
      <h3>Was die KI sieht</h3>
      ${testInfo?.testbild
        ? `<img class="ki-bild" src="/api/test/bild?t=${testbildStand}" alt="Testbild"><div class="knopfreihe"><button type="button" class="klein" data-aktion="test-bild-weg">Testbild entfernen (wieder Kamera)</button></div>`
        : `<p class="hinweis">Die echte Kamera. Steht dort niemand, sagt die KI „kein Mensch“ und spielt den Ersatz. Lade ein Foto mit Leuten
            (z. B. Kinder im Kostüm), dann sieht die KI dieses Bild statt der Kamera.</p>`}
      <label class="feld"><span>Testbild laden (JPG/PNG)</span><input type="file" accept="image/*" id="test-bild"></label>
      ${e ? `<p class="hinweis">Zuletzt: ${esc(e.quelle)} · ${esc(e.satz ? `„${e.satz}“` : e.ergebnis)}${e.beschreibung ? `<br>${esc(e.beschreibung)}` : ""}</p>` : ""}
    </div>`;
});

bereich("test-schnell", "test", ["daten"], () => `
  <div class="karte">
    <h2>Schnell auslösen</h2>
    <div class="knopfreihe" style="flex-wrap:wrap">
      ${Object.entries(S.kategorien || {}).filter(([k]) => !["system"].includes(k)).map(([k, name]) =>
        `<button type="button" class="klein" data-aktion="test-kategorie" data-kategorie="${k}">${KATEGORIE_SYMBOL[k] || "🔊"} ${esc(name)}</button>`).join("")}
    </div>
    <h3>Szenen</h3>
    <div class="knopfreihe" style="flex-wrap:wrap">
      ${(S.szenen || []).map((s) => `<button type="button" class="klein" data-aktion="test-szene" data-id="${esc(s.id)}">🎬 ${esc(s.name)}</button>`).join("")}
    </div>
  </div>`);

// Live-Anzeige: läuft mit der Bildrate, solange der Reiter offen ist (Bereiche werden dabei nicht neu gezeichnet)
const anzeigeWert = [];
let buehneLaeuft = false;
function buehneAnimieren() {
  if (buehneLaeuft) return;
  buehneLaeuft = true;
  let letzte = performance.now();
  const bild = (jetzt) => {
    if (seite !== "test" || document.hidden) {
      buehneLaeuft = false;
      return;
    }
    const dt = Math.min(0.1, (jetzt - letzte) / 1000);
    letzte = jetzt;
    const werte = test.laeuft ? test.werte : (Date.now() - letzteWerte < 3000 ? werte_ : []);
    for (const g of document.querySelectorAll(".tb-licht")) {
      const z = +g.dataset.zone;
      const ziel = (werte[z] ?? 0) / 100;
      const ist = anzeigeWert[z] ?? 0;
      anzeigeWert[z] = ist + (ziel - ist) * (1 - Math.exp(-dt * (ziel > ist ? 40 : 12)));
      g.setAttribute("opacity", Math.pow(anzeigeWert[z], 0.6).toFixed(3));
    }
    const zust = test.zustand?.zonen || S.zustand?.licht?.zonen || [];
    for (let z = 0; z < S.einstellungen.licht.zonen; z++) {
      const w = werte[z];
      const balken = document.getElementById(`tz-balken-${z}`);
      if (!balken) continue;
      balken.style.height = `${w ?? 0}%`;
      document.getElementById(`tz-wert-${z}`).textContent = w == null ? "–" : `${Math.round(w)} %`;
      const e = zust[z];
      document.getElementById(`tz-effekt-${z}`).textContent = e ? `${EFFEKT_NAMEN[e.e] || e.e} · B${e.basis} S${e.staerke} T${e.tempo}` : "–";
    }
    const jetztMs = performance.now();
    const aktiv = test.overlays.filter((o) => o.ab <= jetztMs).map((o) => `${AKTION_SYMBOL[o.name] || "✨"} ${AKTION_NAMEN[o.name] || o.name}`);
    const t = document.getElementById("tb-aktion");
    if (t) t.textContent = [...new Set(aktiv)].join("  ");
    const pegel = (id, v) => { const el = document.getElementById(id); if (el) el.style.width = `${Math.min(100, v)}%`; };
    pegel("tm-musik", test.huelle[0] / 2.55);
    pegel("tm-fx", test.huelle[1] / 2.55);
    pegel("tm-bass", test.spektrum[0] / 2.55);
    pegel("tm-mitte", test.spektrum[1] / 2.55);
    pegel("tm-hoehen", test.spektrum[2] / 2.55);
    if (test.spektrum[3]) pegel("tm-beat", 100);
    else { const b = document.getElementById("tm-beat"); if (b) b.style.width = `${Math.max(0, parseFloat(b.style.width || 0) - dt * 300)}%`; }
    const liste = document.getElementById("test-klaenge");
    if (liste) {
      const html = test.stimmen.map((s) => {
        const rest = Math.max(0, (s.start + s.dauer - jetztMs) / 1000);
        return `<li><time>${rest.toFixed(1)} s</time><span>${s.art === "sag" ? "🗣️" : "🔊"} ${esc(s.name)}</span></li>`;
      }).join("") || `<li class="leer">${test.laeuft ? "Gerade kein Effekt" : "PC-Test läuft nicht"}</li>`;
      if (liste.innerHTML !== html) liste.innerHTML = html;
    }
    requestAnimationFrame(bild);
  };
  requestAnimationFrame(bild);
}
// app.js hält die Werte des echten ESP in „werte“ – ohne laufenden PC-Test zeigt die Bühne die
let werte_ = [];
setInterval(() => (werte_ = typeof werte !== "undefined" ? werte : []), 50);

function zeichneTest() {
  aktualisieren(["test"]);
  if (seite === "test") buehneAnimieren();
}

// Bedienung
document.addEventListener("click", async (ev) => {
  const el = ev.target.closest("[data-aktion]");
  if (!el) return;
  switch (el.dataset.aktion) {
    case "test-start": await test.starten(); break;
    case "test-stopp": test.stoppen(); break;
    case "test-sensor": {
      const r = await api("POST", "/api/test/sensor", { rolle: el.dataset.rolle, an: el.dataset.an === "1" });
      if (el.dataset.an === "1" && !r.ausgeloest) toast("Nicht ausgelöst (Show aus, Sperrzeit oder Gruselstufe)");
      break;
    }
    case "test-bild-weg": await api("DELETE", "/api/test/bild"); await testInfoLaden(); break;
    case "test-sim": window.simulationStand = await api("POST", "/api/test/simulation", { drehbuch: el.dataset.drehbuch }); aktualisieren(["test"]); break;
    case "test-sim-stopp": window.simulationStand = await api("DELETE", "/api/test/simulation"); aktualisieren(["test"]); break;
    case "test-kategorie": await api("POST", `/api/fx/kategorie/${el.dataset.kategorie}/abspielen`); break;
    case "test-szene": await api("POST", `/api/szenen/${el.dataset.id}/start`); break;
  }
});
document.addEventListener("change", async (ev) => {
  if (ev.target.id !== "test-bild" || !ev.target.files?.[0]) return;
  const fd = new FormData();
  fd.append("datei", ev.target.files[0]);
  await api("POST", "/api/test/bild", fd, true);
  testbildStand = Date.now();
  await testInfoLaden();
  kiLaden();
});

// Container anlegen (app.js hat die übrigen Seiten schon gefüllt) und ggf. die gemerkte Seite zeigen
$("#seite-test").innerHTML = BEREICHE.filter((b) => b.seite === "test").map((b) => `<div id="${b.id}"></div>`).join("");
document.querySelector('#reiter button[data-seite="test"]')?.addEventListener("click", () => {
  testInfoLaden();
  kiLaden();
  setTimeout(buehneAnimieren, 50);
});
let gemerkteSeite = "";
try { gemerkteSeite = localStorage.getItem("spuk-seite") || ""; } catch { /* egal */ }
if (gemerkteSeite === "test") {
  zeigeSeite("test");
  testInfoLaden();
  kiLaden();
  setTimeout(buehneAnimieren, 200);
}
// Server-Daten können nach dem Modul kommen: dann die Testseite nachzeichnen
setInterval(() => { if (seite === "test" && S && !document.getElementById("tz-wert-0")) zeichneTest(); }, 1000);
