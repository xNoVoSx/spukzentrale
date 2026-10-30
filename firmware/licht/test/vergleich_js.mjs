// Abgleich C++-Engine <-> JavaScript-Port. Aufruf über vergleich_js.sh.
//   node vergleich_js.mjs erzeuge drehbuch.txt [saat] [mal] -> schreibt das Drehbuch (feste Saat, wiederholbar;
//                                                            mal = Faktor für die Länge des gemischten Teils)
//   node vergleich_js.mjs pruefe drehbuch.txt cpp.txt   -> spielt es mit dem JS-Modul ab und vergleicht
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const hier = dirname(fileURLToPath(import.meta.url));
const { Lichtmaschine, EFFEKTE, AKTIONEN, effektVonName, aktionVonName, werteFuerVorschau } = await import(
  join(hier, "../../../server/static/lichtmaschine.js")
);

const GRENZE = 0.002;  // erlaubte Abweichung je Zone

// ---------------------------------------------------------------------------------------------------------------
// Drehbuch
// ---------------------------------------------------------------------------------------------------------------
function erzeuge(saat = 0xC0FFEE, mal = 1) {
  const rnd = () => {  // mulberry32
    saat = (saat + 0x6d2b79f5) >>> 0;
    let x = saat;
    x = Math.imul(x ^ (x >>> 15), x | 1);
    x ^= x + Math.imul(x ^ (x >>> 7), x | 61);
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
  };
  const ganz = (a, b) => a + Math.floor(rnd() * (b - a + 1));
  const wahl = (arr) => arr[Math.floor(rnd() * arr.length)];
  const z = [];
  const out = (s) => z.push(s);

  let n = 5;
  let glob = { an: 1, hell: 80, mr: 0, zm: [100, 100, 100, 100, 100, 100], strobo: "3" };
  const G = () => out(`G ${glob.an} ${glob.hell} ${glob.mr} ${glob.zm.join(" ")} ${glob.strobo}`);
  const zoneZufall = (i, e) => {
    const tempo = wahl([0, 1, 20, 50, 50, 73, 99, 100, ganz(0, 100)]);
    const staerke = wahl([0, 30, 80, 100, ganz(0, 100)]);
    const basis = wahl([0, 5, 10, 40, ganz(0, 100)]);
    out(`Z ${i} ${e} ${tempo} ${staerke} ${basis}`);
  };
  const zufallsAktion = (t) => {
    const typ = rnd() < 0.05 ? wahl([0, 11, 200]) : ganz(1, AKTIONEN.length - 1);
    const verz = wahl([0, 0, 0, 100, ganz(0, 1500)]);
    const dauer = wahl([0, 1, 2, 300, 800, 1500, 2500, ganz(1, 6000)]);
    const maske = wahl([0, 0, 1, 2, 4, 0b10101, 0b11000, 63, ganz(0, 255), 0xffffffff]);
    const staerke = wahl([100, 100, 50, 0, 150, ganz(0, 255)]);
    const param = typ === 3 ? wahl([0, 0, 200, 900, 5000, ganz(0, 3000)]) : wahl([0, 0, ganz(0, 65535)]);
    out(`A ${typ} ${t >>> 0} ${verz} ${dauer} ${maske >>> 0} ${staerke} ${param}`);
  };

  // synthetische Musik: Grundpegel + Schläge (Kick) alle ~470 ms, Effekt-Sounds als Hüllkurven-Bursts
  let fxBis = 0, fxPegel = 0, letzterKick = -1e9, kickAbstand = 470;
  const audio = (tRel, t, mitHuelle, mitSpektrum) => {
    let beat = 0;
    if (tRel - letzterKick >= kickAbstand) {
      letzterKick = tRel;
      kickAbstand = wahl([470, 470, 235, 500, ganz(300, 900)]);
      beat = ganz(1, 255);
    }
    const seitKick = tRel - letzterKick;
    const kick = Math.max(0, 1 - seitKick / 150);
    const musik = Math.min(255, Math.round(50 + 30 * Math.sin(tRel / 3700) + 160 * kick + 10 * rnd()));
    if (tRel >= fxBis && rnd() < 0.004) {
      fxBis = tRel + ganz(200, 3000);
      fxPegel = ganz(60, 255);
    }
    const fx = tRel < fxBis ? Math.min(255, Math.round(fxPegel * (0.5 + 0.5 * rnd()))) : 0;
    if (mitHuelle) out(`H ${musik} ${fx} ${t >>> 0}`);
    if (mitSpektrum) {
      const bass = Math.min(255, Math.round(40 + 200 * kick + 15 * rnd()));
      const mitte = Math.min(255, Math.round(80 + 60 * Math.sin(tRel / 900) + 20 * rnd()));
      const hoehen = Math.min(255, Math.round(rnd() < 0.2 ? 255 * rnd() : 30 * rnd()));
      out(`P ${bass} ${mitte} ${hoehen} ${beat} ${t >>> 0}`);
    }
  };

  // Ein Lauf: Maschine mit n Zonen ab Startzeit t0
  const lauf = (zonen, t0, phasen) => {
    n = zonen;
    out(`N ${n}`);
    glob = { an: 1, hell: 80, mr: 0, zm: [100, 100, 100, 100, 100, 100], strobo: "3" };
    let t = t0, tRel = 0;
    let summe = 0;
    for (const ph of phasen) ph.bis = summe += ph.dauer;
    let phIdx = 0, phStart = 0;
    let naechsteAktion = 0, naechsterWechsel = 0, naechsteGlobal = 0;
    let huelleAus = 0, spektrumAus = 0;
    while (phIdx < phasen.length) {
      const ph = phasen[phIdx];
      if (tRel >= ph.bis) { phIdx++; phStart = tRel; naechsterWechsel = tRel; continue; }
      const imSegment = tRel - phStart;

      if (ph.art === "effekt") {
        // ein Effekt auf allen Zonen, 6 s; nach 3 s neue Parameter (gleicher Effekt), Spektrum in der 2. Hälfte
        if (imSegment === 0) for (let i = 0; i < n; i++) zoneZufall(i, ph.effekt);
        if (imSegment >= 3000 && !ph.mitte) {
          ph.mitte = true;
          for (let i = 0; i < n; i++) if (rnd() < 0.7) zoneZufall(i, ph.effekt);
          glob.hell = wahl([100, 80, 50, 100]);
          glob.strobo = wahl(["3", "15", "0.2", "7.5"]);
          G();
        }
        audio(tRel, t, imSegment < 5200 || imSegment > 5900, imSegment >= 3000);
      } else {
        // gemischt: Effektwechsel (Überblendung), Aktionen, globale Parameter, Lücken in Hüllkurve/Spektrum
        if (tRel >= naechsterWechsel) {
          naechsterWechsel = tRel + ganz(400, 4000);
          const anzahl = ganz(1, n);
          for (let k = 0; k < anzahl; k++) zoneZufall(ganz(0, n - 1), ganz(0, EFFEKTE.length - 1));
        }
        if (tRel >= naechsteAktion) {
          naechsteAktion = tRel + wahl([ganz(50, 400), ganz(300, 1200), 0]);
          zufallsAktion(t);
          if (rnd() < 0.03) for (let k = 0; k < 8; k++) zufallsAktion(t);  // mehr als 6 -> ältester fliegt raus
          if (rnd() < 0.02) out("S");
        }
        if (tRel >= naechsteGlobal) {
          naechsteGlobal = tRel + ganz(1500, 8000);
          const was = ganz(0, 5);
          if (was === 0) glob.an = glob.an ? 0 : 1;
          if (was === 1) glob.hell = wahl([0, 20, 50, 80, 100, ganz(0, 100)]);
          if (was === 2) glob.mr = wahl([0, 0, 30, 100, ganz(0, 100)]);
          if (was === 3) glob.zm = glob.zm.map(() => wahl([100, 100, 60, 0, ganz(0, 100)]));
          if (was === 4) glob.strobo = wahl(["3", "15", "0.2", "0.5", "7.5", "4.37"]);
          if (was === 5) glob.an = 1;
          G();
        }
        if (tRel >= huelleAus + 800 && rnd() < 0.002) huelleAus = tRel + ganz(300, 2000);
        if (tRel >= spektrumAus + 800 && rnd() < 0.003) spektrumAus = tRel + ganz(200, 2000);
        audio(tRel, t, tRel >= huelleAus, ph.spektrum && tRel >= spektrumAus);
      }
      out(`B ${t >>> 0}`);
      // 100 Hz wie die Firmware, mit etwas Zittern und gelegentlichen Aussetzern (dt > 0,5 s)
      let schritt = 10;
      const r = rnd();
      if (r < 0.05) schritt = 9;
      else if (r < 0.10) schritt = 11;
      else if (r < 0.102) schritt = ganz(30, 200);
      else if (r < 0.1023) schritt = ganz(501, 900);
      tRel += schritt;
      t += schritt;
    }
  };

  // 1) 5 Zonen: jeder Effekt einzeln (16 x 6 s), danach 100 s gemischt (60 s mit, 40 s ohne Spektrum)
  lauf(5, 1000, [
    ...EFFEKTE.map((e, idx) => ({ art: "effekt", effekt: idx, dauer: 6000 })),
    { art: "gemischt", spektrum: true, dauer: 60000 * mal },
    { art: "gemischt", spektrum: false, dauer: 40000 * mal },
  ]);
  // 2) 1 Zone, 25 s gemischt
  lauf(1, 5000, [{ art: "gemischt", spektrum: true, dauer: 25000 }]);
  // 3) 6 Zonen über den uint32-Überlauf von millis() (nach ~49,7 Tagen), 40 s
  lauf(6, 4294967296 - 15000, [
    ...[7, 13, 11, 2].map((e) => ({ art: "effekt", effekt: e, dauer: 6000 })),
    { art: "gemischt", spektrum: true, dauer: 16000 },
  ]);
  // 4) 3 Zonen ab t = 0, 20 s
  lauf(3, 0, [{ art: "gemischt", spektrum: true, dauer: 20000 }]);
  return z.join("\n") + "\n";
}

// ---------------------------------------------------------------------------------------------------------------
// Abspielen + Vergleich
// ---------------------------------------------------------------------------------------------------------------
function pruefe(drehbuch, cppText) {
  // C++ gibt die float-Bits als Hex aus (verlustfrei), die Zeit dezimal
  const bits = new DataView(new ArrayBuffer(4));
  const alsFloat = (h) => (bits.setUint32(0, parseInt(h, 16)), bits.getFloat32(0));
  const cpp = cppText.split("\n").filter(Boolean).map((l) => l.split(" ").map((x, i) => (i ? alsFloat(x) : Number(x))));
  let m = null, bild = 0;
  const jeEffekt = new Map(), jeAktion = new Map();
  let maxGesamt = 0, maxNorm = 0, bilderGleich = 0, werteGesamt = 0, werteExakt = 0, vorschauAnders = 0;
  let ersterFehler = null;
  const merke = (map, k, d) => {
    const e = map.get(k) || { max: 0, n: 0 };
    e.max = Math.max(e.max, d);
    e.n++;
    map.set(k, e);
  };
  for (const zeile of drehbuch.split("\n")) {
    if (!zeile) continue;
    const [cmd, ...r] = zeile.split(" ");
    const zn = r.map(Number);
    switch (cmd) {
      case "N": m = new Lichtmaschine(zn[0]); break;
      case "Z": m.setzeZone(zn[0], { effekt: zn[1], tempo: zn[2], staerke: zn[3], basis: zn[4] }); break;
      case "G": {
        const g = m.global;
        g.an = zn[0] !== 0; g.hell = zn[1]; g.musikReaktiv = zn[2];
        for (let i = 0; i < 6; i++) g.zoneMax[i] = zn[3 + i];
        g.stroboMaxHz = parseFloat(r[9]);
        break;
      }
      case "A": m.aktion(zn[0], zn[1], zn[2], zn[3], zn[4], zn[5], zn[6]); break;
      case "S": m.aktionenStoppen(); break;
      case "H": m.huelle(zn[0], zn[1], zn[2]); break;
      case "P": m.spektrum(zn[0], zn[1], zn[2], zn[3], zn[4]); break;
      case "B": {
        const t = zn[0];
        const blende = m._z.map((z) => z.blende);
        const w = m.berechne(t);
        const c = cpp[bild++];
        if (!c || c[0] !== t) throw new Error(`C++-Ausgabe passt nicht zum Drehbuch (Bild ${bild}, t=${t})`);
        let gleich = true;
        for (let i = 0; i < m.zonen; i++) {
          const d = Math.abs(w[i] - c[1 + i]);
          werteGesamt++;
          if (d === 0) werteExakt++;
          else gleich = false;
          maxGesamt = Math.max(maxGesamt, d);
          if (d > GRENZE && !ersterFehler) ersterFehler = `Bild ${bild} t=${t} Zone ${i}: JS ${w[i]} C++ ${c[1 + i]}`;
          merke(jeEffekt, EFFEKTE[m.zone(i).effekt] + (blende[i] ? " (Überblendung)" : ""), d);
          for (const o of m._o) {
            if (o.typ === 0 || ((t - o.start) | 0) < 0 || ((t - o.start) >>> 0) >= o.dauer) continue;
            if ((o.maske >>> i) & 1) merke(jeAktion, AKTIONEN[o.typ], d);
          }
        }
        const vJs = werteFuerVorschau(w), vC = werteFuerVorschau(c.slice(1, 1 + m.zonen));
        if (vJs.some((x, i) => x !== vC[i])) vorschauAnders++;
        const dn = Math.max(Math.abs(m.musikNorm - c[1 + m.zonen]), Math.abs(m.fxNorm - c[2 + m.zonen]));
        maxNorm = Math.max(maxNorm, dn);
        if (gleich) bilderGleich++;
        break;
      }
    }
  }
  if (bild !== cpp.length) throw new Error(`C++ hat ${cpp.length} Bilder, JS ${bild}`);

  // Namen-Hilfen gegenprüfen
  let namenOk = EFFEKTE.every((e, i) => effektVonName(e) === i) && effektVonName("gibtsnicht") === 2 &&
    AKTIONEN.every((a, i) => aktionVonName(a) === i) && aktionVonName("gibtsnicht") === 0;

  const fmt = (x) => (x === 0 ? "0 (exakt)" : x.toExponential(2));
  console.log(`Bilder: ${bild} (${(bild / 100).toFixed(0)} s bei 100 Hz), Zonenwerte: ${werteGesamt}, ` +
    `davon bitgleich: ${werteExakt} (${((100 * werteExakt) / werteGesamt).toFixed(3)} %)`);
  console.log("Max. Abweichung je Effekt:");
  for (const [k, e] of [...jeEffekt].sort()) console.log(`  ${k.padEnd(26)} ${fmt(e.max).padEnd(12)} (${e.n} Werte)`);
  console.log("Max. Abweichung je Aktion (Zonen, in denen sie gerade wirkt):");
  for (const [k, e] of [...jeAktion].sort()) console.log(`  ${k.padEnd(26)} ${fmt(e.max).padEnd(12)} (${e.n} Werte)`);
  console.log(`musikNorm/fxNorm: max. Abweichung ${fmt(maxNorm)}; Vorschau-Prozent abweichend in ${vorschauAnders} Bildern`);
  if (!namenOk) console.log("FEHLER: Namen/Enums stimmen nicht");
  if (maxGesamt > GRENZE || maxNorm > GRENZE || !namenOk) {
    console.log(`FEHLER: max. Abweichung ${maxGesamt} > ${GRENZE}` + (ersterFehler ? `\n  zuerst: ${ersterFehler}` : ""));
    process.exit(1);
  }
  console.log(`OK, max. Abweichung ${fmt(maxGesamt)} (Grenze ${GRENZE})`);
}

const [modus, a, b] = process.argv.slice(2);
if (modus === "erzeuge") writeFileSync(a, erzeuge(Number(process.argv[4] ?? 0xC0FFEE), Number(process.argv[5] ?? 1)));
else if (modus === "pruefe") pruefe(readFileSync(a, "utf8"), readFileSync(b, "utf8"));
else {
  console.error("Aufruf: node vergleich_js.mjs erzeuge drehbuch.txt | pruefe drehbuch.txt cpp.txt");
  process.exit(2);
}
