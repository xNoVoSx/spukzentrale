/* Spukzentrale – Weboberfläche (Vanilla-JS)
 *
 * Der Server schickt per WebSocket (/ws/ui) den kompletten Zustand ("alles") und danach
 * Änderungen. Befehle gehen per REST an /api/... – die Antwort kommt wieder über den
 * WebSocket, die Oberfläche zeigt also immer den echten Stand.
 *
 * Die Seiten bestehen aus „Bereichen“. Jeder Bereich hängt an Themen (zustand, geraete,
 * medien, daten, protokoll) und wird nur neu gezeichnet, wenn sich eines davon ändert –
 * und nie, während man darin gerade etwas bedient.
 */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

let S = null;              // letzter Stand vom Server
let seite = "show";
let werte = [];            // Live-Werte der Zonen (0..100)
let letzteWerte = 0;       // Zeitpunkt der letzten Werte
let haSensoren = null;     // Auswahlliste aus Home Assistant
let entwurf = null;        // Szene im Editor
let regelEntwurf = null;   // Ereignis-Regeln im Editor
let ausloeserEntwurf = null; // Auslöser im Editor
const offeneDetails = new Set(["e-zeitplan"]);
const vorhoerer = new Audio();

const AKTION_NAMEN = {
  keine: "Kein Licht", blitz: "Blitz", env_blitz: "Blitze zum Sound", schreck: "Schreck", blackout: "Blackout",
  welle: "Welle", alle_an: "Alle an", puls: "Puls", test: "Test", flackern: "Flackern", env_puls: "Puls zum Sound",
};
const EFFEKT_NAMEN = {
  aus: "Aus", an: "An", flackern: "Kerze", feuer: "Feuer", atmen: "Atmen", herzschlag: "Herzschlag",
  strobo: "Stroboskop", gewitter: "Gewitter", lauflicht: "Lauflicht", wandern: "Geist wandert", pingpong: "Ping-Pong",
  neon: "Kaputte Röhre", zufall: "Zufall", musik: "Zur Musik",
};
const KATEGORIE_SYMBOL = {
  donner: "⚡", schrei: "😱", lachen: "🧙", herz: "❤️", wind: "🌬️", kette: "⛓️", kettensaege: "🪚", stimme: "🗣️", sonstiges: "👻", system: "🔧",
};
const PLAN_FARBEN = ["#ff7a1a", "#9b6bff", "#5fd17a", "#ffd166", "#4cc9f0", "#ff4d5e"];
const AKTION_SYMBOL = { blitz: "⚡", schreck: "😱", blackout: "🌑", welle: "🌊", puls: "💓", alle_an: "☀️", flackern: "🕯️" };

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------
async function api(methode, pfad, daten, form = false) {
  const opt = { method: methode, headers: {} };
  if (form) {
    opt.body = daten;
  } else if (daten !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(daten);
  }
  let r;
  try {
    r = await fetch(pfad, opt);
  } catch (e) {
    toast("Server nicht erreichbar", true);
    throw e;
  }
  const text = await r.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch { /* kein JSON */ }
  if (!r.ok) {
    let meldung = json?.detail ?? text ?? r.statusText;
    if (Array.isArray(meldung)) meldung = meldung.map((d) => `${(d.loc || []).slice(-1)[0] ?? ""}: ${d.msg}`).join(", ");
    toast(String(meldung), true);
    throw new Error(meldung);
  }
  return json;
}

function toast(text, fehler = false) {
  const t = $("#toast");
  t.textContent = text;
  t.className = "toast" + (fehler ? " fehler" : "");
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), fehler ? 5000 : 2500);
}

function verbinden() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/ui`);
  ws.onmessage = (ev) => empfangen(JSON.parse(ev.data));
  ws.onopen = () => ($("#getrennt").hidden = true);
  ws.onclose = () => {
    $("#getrennt").hidden = false;
    setTimeout(verbinden, 2000);
  };
}

function empfangen(m) {
  switch (m.t) {
    case "alles":
      S = m;
      aktualisieren(["zustand", "geraete", "medien", "daten", "protokoll"]);
      break;
    case "zustand":
      Object.assign(S, m);
      aktualisieren(["zustand"]);
      break;
    case "geraete":
      S.geraete = m.geraete;
      aktualisieren(["geraete"]);
      break;
    case "geraet_status":
      if (S.geraete[m.rolle]) S.geraete[m.rolle].status = m.status;
      aktualisieren(["geraete"]);
      break;
    case "medien":
      Object.assign(S.medien, m);
      aktualisieren(["medien"]);
      break;
    case "log":
      S.protokoll.unshift(m);
      S.protokoll.length = Math.min(S.protokoll.length, 60);
      aktualisieren(["protokoll"]);
      break;
    case "werte":
      werte = m.v;
      letzteWerte = Date.now();
      vorschauZeichnen();
      break;
  }
}

// ---------------------------------------------------------------------------
// Bereiche
// ---------------------------------------------------------------------------
const BEREICHE = [];

function bereich(id, seiteName, themen, render, nachher) {
  BEREICHE.push({ id, seite: seiteName, themen, render, nachher, veraltet: true });
}

function beschaeftigt(el) {
  const a = document.activeElement;
  return a && el.contains(a) && ["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName) && a.type !== "button";
}

function zeichne(b) {
  const el = document.getElementById(b.id);
  if (!el || !S) return;
  if (beschaeftigt(el)) {
    b.veraltet = true;
    return;
  }
  el.innerHTML = b.render();
  for (const d of $$("details[id]", el)) d.open = offeneDetails.has(d.id);
  b.nachher?.(el);
  b.veraltet = false;
}

function aktualisieren(themen) {
  for (const b of BEREICHE) {
    if (!b.themen.some((t) => themen.includes(t))) continue;
    b.veraltet = true;
    if (b.seite === seite || b.seite === "*") zeichne(b);
  }
}

function zeigeSeite(name) {
  seite = name;
  for (const s of $$(".seite")) s.hidden = s.id !== `seite-${name}`;
  for (const k of $$("#reiter button")) k.classList.toggle("aktiv", k.dataset.seite === name);
  for (const b of BEREICHE) if (b.seite === name && b.veraltet) zeichne(b);
  window.scrollTo(0, 0);
  try { localStorage.setItem("spuk-seite", name); } catch { /* egal */ }
}

// nach dem Bedienen eines Feldes nachholen, was inzwischen geändert wurde
document.addEventListener("focusout", () => {
  setTimeout(() => {
    for (const b of BEREICHE) if (b.veraltet && (b.seite === seite || b.seite === "*")) zeichne(b);
  }, 50);
});

// ---------------------------------------------------------------------------
// Bausteine
// ---------------------------------------------------------------------------
const schalter = (aktion, an, extra = "") =>
  `<label class="schalter"><input type="checkbox" data-aenderung="${aktion}" ${an ? "checked" : ""} ${extra}><i></i></label>`;

const regler = (aktion, label, wert, extra = "", min = 0, max = 100, einheit = "") => `
  <div class="regler">
    <label>${esc(label)}</label>
    <input type="range" min="${min}" max="${max}" value="${wert}" data-aenderung="${aktion}" data-einheit="${einheit}" ${extra}>
    <output>${wert}${einheit}</output>
  </div>`;

const optionen = (liste, gewaehlt) =>
  liste.map(([w, t]) => `<option value="${esc(w)}" ${String(w) === String(gewaehlt) ? "selected" : ""}>${esc(t)}</option>`).join("");

const zeit = (ts) => new Date(ts * 1000).toLocaleTimeString("de-DE");
const dauer = (ms) => (ms >= 60000 ? `${Math.floor(ms / 60000)}:${String(Math.round((ms % 60000) / 1000)).padStart(2, "0")} min` : `${(ms / 1000).toFixed(1)} s`);

const zonenAnzahl = () => S.einstellungen.licht.zonen;
const zonenName = (i) => S.einstellungen.licht.zonen_namen[i] || `Zone ${i + 1}`;
const kategorien = (mitSystem = false) => Object.entries(S.kategorien).concat(mitSystem ? [["system", "System"]] : []);
const fxDerKategorie = (k) => S.medien.fx.filter((f) => f.kategorie === k);

// ---------------------------------------------------------------------------
// Seite: Show
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Aufbau: Streifen an der Wand, Kabelwege, Strom (für Montageplan und Vorschau)
// ---------------------------------------------------------------------------
const KUPFER = 0.0175; // Ω·mm²/m

function aufbauBerechnen() {
  const a = S.einstellungen.aufbau;
  const zonen = zonenAnzahl();
  const abstand = a.wand_m / a.streifen;
  const streifen = [...Array(a.streifen).keys()].map((i) => {
    const zone = a.zuordnung[i] ?? Math.floor((i * zonen) / a.streifen);
    return { i, x: abstand * (i + 0.5), zone, gueltig: zone < zonen };
  });
  const ampereProStreifen = (a.streifen_m * a.watt_pro_m) / 12;
  const rProM = (2 * KUPFER) / a.querschnitt_mm2; // Hin- und Rückleiter
  const zonenInfo = [...Array(zonen).keys()].map((z) => {
    const eigene = streifen.filter((st) => st.zone === z);
    let kabel = 0, abfall = 0;
    const strecken = [];
    // Der Kasten speist nach links und rechts je eine Kette (Zuleitung, dann Brücken)
    for (const seite of [eigene.filter((st) => st.x < a.kasten_m).sort((p, q) => q.x - p.x),
                         eigene.filter((st) => st.x >= a.kasten_m).sort((p, q) => p.x - q.x)]) {
      let vorher = null, u = 0;
      seite.forEach((st, k) => {
        const laenge = vorher ? Math.abs(st.x - vorher.x) : Math.abs(st.x - a.kasten_m) + a.zuleitung_extra_m;
        const strom = (seite.length - k) * ampereProStreifen; // fließt zu diesem und allen weiteren Streifen
        u += strom * laenge * rProM;
        kabel += laenge;
        strecken.push({ von: vorher ? vorher.x : a.kasten_m, nach: st.x, laenge, bruecke: !!vorher });
        vorher = st;
      });
      abfall = Math.max(abfall, u);
    }
    const watt = eigene.length * a.streifen_m * a.watt_pro_m;
    return { zone: z, streifen: eigene, watt, ampere: watt / 12, kabel, abfall, strecken };
  });
  const watt = zonenInfo.reduce((m, z) => m + z.watt, 0);
  return { a, abstand, streifen, zonenInfo, watt, kabel: zonenInfo.reduce((m, z) => m + z.kabel, 0) };
}

bereich("show-vorschau", "show", ["daten"], () => {
  const { a, streifen, abstand } = aufbauBerechnen();
  const massstab = 372 / a.wand_m;
  const breite = Math.max(10, abstand * massstab * 1.6);
  const lichter = streifen.filter((st) => st.gueltig).map((st) => {
    const x = 14 + st.x * massstab;
    const farbe = S.einstellungen.licht.zonen_farben[st.zone];
    return `
      <g class="vz" data-zone="${st.zone}" opacity="0">
        <ellipse cx="${x}" cy="100" rx="${breite}" ry="95" fill="url(#zg${st.zone})"/>
        <rect x="${x - 1.5}" y="40" width="3" height="120" rx="1.5" fill="${farbe}"/>
      </g>`;
  }).join("");
  const verlaeufe = [...Array(zonenAnzahl()).keys()].map((z) => {
    const farbe = S.einstellungen.licht.zonen_farben[z];
    return `<radialGradient id="zg${z}"><stop offset="0" stop-color="${farbe}" stop-opacity=".9"/>
      <stop offset=".5" stop-color="${farbe}" stop-opacity=".3"/><stop offset="1" stop-color="${farbe}" stop-opacity="0"/></radialGradient>`;
  }).join("");
  return `
    <div class="karte" style="padding:0">
      <div class="vorschau">
        <svg viewBox="0 0 400 200" role="img" aria-label="Live-Vorschau des Netzes">
          <defs>
            ${verlaeufe}
            <pattern id="netz" width="6" height="6" patternUnits="userSpaceOnUse">
              <path d="M6 0H0V6" fill="none" stroke="#fff" stroke-opacity=".12" stroke-width=".6"/>
            </pattern>
            <linearGradient id="nacht" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stop-color="#110c1a"/><stop offset="1" stop-color="#05040a"/>
            </linearGradient>
          </defs>
          <rect width="400" height="200" fill="url(#nacht)"/>
          ${lichter}
          <rect x="14" y="14" width="372" height="172" fill="url(#netz)"/>
          <g fill="#000" opacity=".6">
            <path d="M118 160 q0-62 30-62 t30 62 l-7-8-8 8-7-8-8 8-7-8-8 8z"/>
            <path d="M268 162 l10-40 10 40z M256 122 h44 l-4 6 h-36z M278 122 q-2-26 18-34 q-10 14-8 34z"/>
            <path d="M206 64 q8-10 16 0 q8-10 16 0 q-6-2-8 4 q-8-4-16 0 q-2-6-8-4z"/>
          </g>
        </svg>
        <div class="offline" id="vorschau-offline" hidden>Licht-ESP nicht verbunden – keine Live-Vorschau</div>
      </div>
    </div>`;
}, () => vorschauZeichnen());

function vorschauZeichnen() {
  const offline = !S?.geraete?.licht?.verbunden || Date.now() - letzteWerte > 3000;
  const hinweis = $("#vorschau-offline");
  if (hinweis) hinweis.hidden = !offline;
  for (const g of $$(".vz")) {
    const v = offline ? 0 : (werte[+g.dataset.zone] ?? 0) / 100;
    g.setAttribute("opacity", Math.pow(v, 0.6).toFixed(3)); // Auge sieht nicht linear
  }
}
setInterval(vorschauZeichnen, 1000);

bereich("show-steuerung", "show", ["zustand", "daten"], () => {
  const z = S.zustand;
  const st = S.zustand.gruselstufe;
  const auto = S.einstellungen.grusel.auto;
  return `
    <div class="karte">
      <button class="power ${z.licht.an ? "an" : ""}" data-aktion="show">${z.licht.an ? "Show läuft" : "Show starten"}</button>
      <div class="schalter-zeile" style="margin-top:8px">
        <div>Ambiente<small>${esc(S.ambiente_jetzt ? "♪ " + S.ambiente_jetzt : z.ambiente_an ? "startet …" : "Soundtrack aus")}</small></div>
        ${schalter("ambiente", z.ambiente_an)}
      </div>
      <div class="schalter-zeile">
        <div>Zufallsereignisse<small>Donner, Gelächter, Schreie … in zufälligen Abständen</small></div>
        ${schalter("ereignisse", z.ereignisse_an)}
      </div>
      <h3 style="margin-top:12px">Gruselstufe ${auto ? '<span class="marke lila">automatisch nach Uhrzeit</span>' : ""}</h3>
      <div class="segmente">
        ${[[1, "1 · Kinder"], [2, "2 · Gruselig"], [3, "3 · Schreck"]].map(([n, t]) =>
          `<button data-aktion="gruselstufe" data-stufe="${n}" class="${st === n ? "aktiv" : ""}">${t}</button>`).join("")}
      </div>
      ${regler("hell", "Helligkeit", z.licht.hell, "", 0, 100, " %")}
      ${regler("lautst", "Lautstärke", S.einstellungen.ton.master, "", 0, 100, " %")}
      ${S.ruhezeit ? `<p class="hinweis warnung">🌙 Ruhezeit: ${S.einstellungen.ruhezeit.modus === "stumm" ? "Ton aus, nur Licht" : `Lautstärke max. ${S.einstellungen.ruhezeit.lautst} %`}</p>` : ""}
      ${S.show_fenster?.start ? `<p class="hinweis">⏰ Zeitplan: ${S.show_fenster.im_fenster ? "läuft bis" : "nächster Start"} ${new Date(S.show_fenster.im_fenster ? S.show_fenster.ende : S.show_fenster.start).toLocaleString("de-DE", { weekday: "short", hour: "2-digit", minute: "2-digit" })}</p>` : ""}
    </div>`;
});

bereich("show-jetzt", "show", ["zustand"], () => {
  const sz = S.szene;
  return `
    <div class="karte">
      <h2>Jetzt</h2>
      <div class="jetzt">
        <div class="name">🎵 ${esc(S.ambiente_jetzt || "–")}<small>Ambiente</small></div>
        <button class="klein" data-aktion="ambiente-weiter" ${S.ambiente_jetzt ? "" : "disabled"}>⏭ Weiter</button>
      </div>
      <div class="jetzt">
        <div class="name">🎬 ${esc(sz ? sz.name : "–")}<small>Szene${S.overlay ? ` · Stimmung „${esc(S.overlay)}“` : ""}</small></div>
        <button class="klein" data-aktion="szene-stopp" ${sz ? "" : "disabled"}>⏹ Stopp</button>
      </div>
      <button class="gefahr" style="width:100%;margin-top:8px" data-aktion="alles-stopp">Alles stoppen</button>
    </div>`;
});

bereich("show-schnell", "show", ["medien", "daten", "zustand"], () => {
  const sounds = kategorien().map(([k, name]) => {
    const anzahl = fxDerKategorie(k).length;
    return `<button data-aktion="fx-kategorie" data-kategorie="${k}" ${anzahl ? "" : "disabled"} title="${anzahl} Sounds">
      <span class="gross">${KATEGORIE_SYMBOL[k]}</span>${esc(name)}</button>`;
  }).join("");
  const licht = ["blitz", "schreck", "blackout", "welle", "puls", "alle_an"].map((a) =>
    `<button data-aktion="licht-aktion" data-name="${a}"><span class="gross">${AKTION_SYMBOL[a]}</span>${AKTION_NAMEN[a]}</button>`).join("");
  const szenen = S.szenen.map((s) =>
    `<button data-aktion="szene-start" data-id="${esc(s.id)}" class="${S.szene?.id === s.id ? "primaer" : ""}">
      <span class="gross">🎬</span>${esc(s.name)}</button>`).join("");
  const stimmungen = S.stimmungen.map((s) =>
    `<button data-aktion="stimmung" data-id="${esc(s.id)}" class="${S.zustand.stimmung_id === s.id ? "aktiv" : ""}">${esc(s.name)}</button>`).join("");
  return `
    <div class="karte">
      <h2>Auslösen</h2>
      <h3>Sounds (mit Licht)</h3>
      <div class="knopfgitter">${sounds}</div>
      ${S.medien.fx.filter((f) => f.kategorie !== "system").length ? "" : '<p class="hinweis">Noch keine Effekt-Sounds – unter „Sounds“ hochladen.</p>'}
      <h3>Nur Licht</h3>
      <div class="knopfgitter">${licht}</div>
      <h3>Szenen</h3>
      <div class="knopfgitter">${szenen || '<span class="leer">keine</span>'}</div>
      <h3>Stimmung</h3>
      <div class="chips">${stimmungen}</div>
    </div>`;
});

bereich("show-protokoll", "show", ["protokoll"], () => `
  <div class="karte">
    <h2>Was passiert ist</h2>
    <ul class="protokoll">
      ${S.protokoll.slice(0, 40).map((e) => `<li class="${e.art === "fehler" ? "fehler" : ""}"><time>${zeit(e.zeit)}</time><span>${esc(e.text)}</span></li>`).join("") || '<li class="leer">Noch nichts.</li>'}
    </ul>
  </div>`);

// ---------------------------------------------------------------------------
// Seite: Licht
// ---------------------------------------------------------------------------
bereich("licht-zonen", "licht", ["zustand", "daten"], () => {
  const z = S.zustand.licht;
  const effekte = S.effekte.map((e) => [e, EFFEKT_NAMEN[e] || e]);
  const zonen = z.zonen.map((zone, i) => `
    <div class="zone">
      <div class="zone-kopf">
        <span class="farbpunkt" style="background:${esc(S.einstellungen.licht.zonen_farben[i])}"></span>
        <strong>${esc(zonenName(i))}</strong>
        <button class="klein" data-aktion="zone-test" data-zone="${i}" title="Nur diese Zone 3 s hell">🔦 Test</button>
        <button class="klein" data-aktion="zone-alle" data-zone="${i}" title="Diese Einstellung auf alle Zonen übertragen">⇉ alle</button>
      </div>
      <select data-aenderung="zone" data-zone="${i}" data-feld="e">${optionen(effekte, zone.e)}</select>
      ${regler("zone", "Tempo", zone.tempo, `data-zone="${i}" data-feld="tempo"`)}
      ${regler("zone", "Stärke", zone.staerke, `data-zone="${i}" data-feld="staerke"`, 0, 100, " %")}
      ${regler("zone", "Grundlicht", zone.basis, `data-zone="${i}" data-feld="basis"`, 0, 100, " %")}
    </div>`).join("");
  return `
    <div class="karte">
      <h2>Zonen</h2>
      ${S.overlay ? `<p class="hinweis warnung">Gerade läuft die Stimmung „${esc(S.overlay)}“ aus einer Szene. Änderungen hier beenden sie.</p>` : ""}
      ${zonen}
      ${regler("musik-reaktiv", "Musik-Puls", z.musik_reaktiv, "", 0, 100, " %")}
      <p class="hinweis">Musik-Puls: alle Zonen pulsieren zusätzlich zur Lautstärke des Ambientes.</p>
    </div>`;
});

bereich("licht-stimmungen", "licht", ["daten", "zustand"], () => `
  <div class="karte">
    <h2>Stimmungen</h2>
    <ul class="liste">
      ${S.stimmungen.map((s) => `
        <li>
          <div class="name">${esc(s.name)}<small>${s.zonen.map((z) => EFFEKT_NAMEN[z.e] || z.e).join(" · ")}</small></div>
          ${S.zustand.stimmung_id === s.id ? '<span class="marke orange">aktiv</span>' : ""}
          <button class="klein" data-aktion="stimmung" data-id="${esc(s.id)}">Anwenden</button>
          <button class="klein symbol" data-aktion="stimmung-umbenennen" data-id="${esc(s.id)}" title="Umbenennen">✏️</button>
          <button class="klein symbol gefahr" data-aktion="stimmung-loeschen" data-id="${esc(s.id)}" title="Löschen">🗑</button>
        </li>`).join("")}
    </ul>
    <button class="lila" style="margin-top:10px" data-aktion="stimmung-speichern">Aktuelle Zonen als Stimmung speichern</button>
  </div>`);

// ---------------------------------------------------------------------------
// Seite: Sounds
// ---------------------------------------------------------------------------
bereich("sounds-ambiente", "sounds", ["medien", "zustand", "daten"], () => {
  const m = S.medien;
  const pl = m.playlist;
  const nachId = Object.fromEntries(m.musik.map((x) => [x.id, x]));
  const liste = pl.reihenfolge.map((id, i) => {
    const s = nachId[id];
    if (!s) return "";
    const laeuft = S.ambiente_jetzt === s.name;
    return `
      <li>
        <div class="name">${laeuft ? "▶️ " : ""}${esc(s.name)}<small>${dauer(s.dauer_s * 1000)}</small></div>
        <button class="klein symbol" data-aktion="musik-hoch" data-i="${i}" ${i ? "" : "disabled"} title="Nach oben">↑</button>
        <button class="klein symbol" data-aktion="vorhoeren" data-url="/api/musik/${s.id}/datei" title="Im Browser anhören">🎧</button>
        <button class="klein symbol" data-aktion="musik-umbenennen" data-id="${s.id}" title="Umbenennen">✏️</button>
        <button class="klein symbol gefahr" data-aktion="musik-loeschen" data-id="${s.id}" title="Löschen">🗑</button>
      </li>`;
  }).join("");
  return `
    <div class="karte">
      <h2>Ambiente</h2>
      <div class="schalter-zeile"><div>Soundtrack läuft${S.ambiente_jetzt ? `<small>♪ ${esc(S.ambiente_jetzt)}</small>` : ""}</div>${schalter("ambiente", S.zustand.ambiente_an)}</div>
      ${regler("ton", "Lautstärke", S.einstellungen.ton.musik_vol, 'data-feld="musik_vol"', 0, 100, " %")}
      ${regler("ton", "Leiser bei FX", S.einstellungen.ton.duck, 'data-feld="duck"', 0, 100, " %")}
      <div class="knopfreihe" style="margin:8px 0">
        <button class="klein" data-aktion="ambiente-weiter" ${S.ambiente_jetzt ? "" : "disabled"}>⏭ Nächstes Stück</button>
        <button class="klein" data-aktion="vorhoeren" data-url="/stream/ambiente">🎧 Stream im Browser</button>
      </div>
      <h3>Playlist</h3>
      <ul class="liste">${liste || '<li class="leer">Noch keine Musik.</li>'}</ul>
      <div class="schalter-zeile"><div>Zufällige Reihenfolge</div>${schalter("playlist-zufall", pl.zufall)}</div>
      ${regler("playlist-ueberblend", "Überblenden", pl.ueberblend_s, "", 0, 15, " s")}
      <h3>Hochladen</h3>
      <input type="file" id="musik-datei" accept="audio/*,.mp3,.ogg,.wav,.flac,.m4a" multiple>
      <button class="lila" style="margin-top:8px;width:100%" data-aktion="musik-hochladen">Musik hochladen</button>
      <p class="hinweis">Jedes Format, das ffmpeg kennt. Wird auf gleiche Lautheit gebracht. Tipp: freesound.org (Filter CC0) oder pixabay.com/sound-effects.</p>
    </div>`;
});

bereich("sounds-fx", "sounds", ["medien", "daten"], () => {
  const m = S.medien;
  const prozent = m.fx_kapazitaet_kb ? Math.min(100, (100 * m.fx_belegt_kb) / m.fx_kapazitaet_kb) : 0;
  const gruppen = kategorien(true).map(([k, name]) => {
    const fx = fxDerKategorie(k);
    if (!fx.length) return "";
    return `
      <h3>${KATEGORIE_SYMBOL[k]} ${esc(name)} (${fx.length})</h3>
      <ul class="liste">${fx.map((f) => `
        <li>
          <div class="name">${esc(f.name)}<small>${dauer(f.dauer_ms)} · ${f.licht ? "💡 " + (AKTION_NAMEN[f.licht.name] || f.licht.name) : "ohne Licht"}${f.vol < 100 ? ` · ${f.vol} %` : ""}</small></div>
          <button class="klein symbol" data-aktion="fx-spielen" data-id="${f.id}" title="Draußen abspielen">▶️</button>
          <button class="klein symbol" data-aktion="vorhoeren" data-url="/api/fx/${f.id}/vorhoeren" title="Im Browser anhören">🎧</button>
          ${k === "system" ? "" : `
            <button class="klein symbol" data-aktion="fx-bearbeiten" data-id="${f.id}" title="Bearbeiten">✏️</button>
            <button class="klein symbol gefahr" data-aktion="fx-loeschen" data-id="${f.id}" title="Löschen">🗑</button>`}
        </li>`).join("")}
      </ul>`;
  }).join("");
  return `
    <div class="karte">
      <h2>Effekt-Sounds</h2>
      <div class="balken ${prozent > 95 ? "voll" : ""}"><i style="width:${prozent.toFixed(1)}%"></i></div>
      <p class="hinweis">Speicher im Sound-ESP: ${m.fx_belegt_kb} von ${m.fx_kapazitaet_kb} KB (≈ ${Math.max(0, Math.round((m.fx_kapazitaet_kb - m.fx_belegt_kb) / 11))} s frei)</p>
      ${regler("ton", "Lautstärke", S.einstellungen.ton.fx_vol, 'data-feld="fx_vol"', 0, 100, " %")}
      ${gruppen}
      <h3>Hochladen</h3>
      <div class="feldreihe">
        <label class="feld"><span>Kategorie</span><select id="fx-kategorie">${optionen(kategorien(), "donner")}</select></label>
        <label class="feld"><span>Name (optional)</span><input type="text" id="fx-name" maxlength="60" placeholder="aus dem Dateinamen"></label>
      </div>
      <input type="file" id="fx-datei" accept="audio/*,.mp3,.ogg,.wav,.flac,.m4a" multiple>
      <button class="lila" style="margin-top:8px;width:100%" data-aktion="fx-hochladen">Effekt hochladen</button>
      <p class="hinweis">Max. 60 s pro Sound. Stille am Anfang wird abgeschnitten, damit das Licht genau auf dem Knall sitzt. Das passende Licht wird je Kategorie vorgeschlagen.</p>
    </div>`;
});

// ---------------------------------------------------------------------------
// Seite: Szenen
// ---------------------------------------------------------------------------
function schrittText(s) {
  if (s.typ === "fx") {
    const quelle = s.fx_id ? (S.medien.fx.find((f) => f.id === s.fx_id)?.name ?? "?") : `zufällig: ${S.kategorien[s.kategorie] ?? s.kategorie}`;
    return `${KATEGORIE_SYMBOL[s.kategorie] || "🔊"} ${quelle}`;
  }
  if (s.typ === "aktion") return `💡 ${AKTION_NAMEN[s.aktion?.name] || "?"}`;
  return `🎨 ${S.stimmungen.find((x) => x.id === s.stimmung_id)?.name ?? "?"} (${dauer(s.dauer_ms)})`;
}

bereich("szenen-liste", "szenen", ["daten", "zustand", "medien"], () => `
  <div class="karte">
    <h2>Szenen</h2>
    <ul class="liste">
      ${S.szenen.map((s) => `
        <li>
          <div class="name">${esc(s.name)} <span class="marke">Stufe ${s.gruselstufe}</span>
            <small>${s.schritte.map(schrittText).map(esc).join(" → ") || "leer"}</small></div>
          <button class="klein ${S.szene?.id === s.id ? "primaer" : ""}" data-aktion="szene-start" data-id="${esc(s.id)}">▶️</button>
          <button class="klein symbol" data-aktion="szene-bearbeiten" data-id="${esc(s.id)}" title="Bearbeiten">✏️</button>
          <button class="klein symbol gefahr" data-aktion="szene-loeschen" data-id="${esc(s.id)}" title="Löschen">🗑</button>
        </li>`).join("")}
    </ul>
    <button class="lila" style="margin-top:10px" data-aktion="szene-neu">Neue Szene</button>
    <p class="hinweis">Schritte mit „zufällig aus Kategorie“ werden übersprungen, solange es in der Kategorie keine Sounds gibt.</p>
  </div>`);

function regelZeile(r, i) {
  const ziele = r.typ === "kategorie" ? kategorien() : S.szenen.map((s) => [s.id, s.name]);
  return `
    <div class="schritt">
      <div class="schalter-zeile" style="padding-top:0">
        <div>${schalter("regel", r.aktiv, `data-i="${i}" data-feld="aktiv"`)}</div>
        <select data-aenderung="regel" data-i="${i}" data-feld="typ" style="width:auto">${optionen([["kategorie", "Sound aus Kategorie"], ["szene", "Szene"]], r.typ)}</select>
        <button class="klein symbol gefahr" data-aktion="regel-loeschen" data-i="${i}">🗑</button>
      </div>
      <div class="feldreihe">
        <label class="feld"><span>Was</span><select data-aenderung="regel" data-i="${i}" data-feld="ziel">${optionen(ziele, r.ziel)}</select></label>
        <label class="feld"><span>Alle … bis … s</span>
          <div style="display:flex;gap:6px"><input type="number" min="5" max="7200" value="${r.min_s}" data-aenderung="regel" data-i="${i}" data-feld="min_s">
          <input type="number" min="5" max="7200" value="${r.max_s}" data-aenderung="regel" data-i="${i}" data-feld="max_s"></div></label>
        <label class="feld"><span>Ab Gruselstufe</span><select data-aenderung="regel" data-i="${i}" data-feld="gruselstufe_min" data-typ="zahl">${optionen([[1, "1"], [2, "2"], [3, "3"]], r.gruselstufe_min)}</select></label>
        ${r.typ === "kategorie" ? `<label class="feld"><span>Lautstärke %</span><input type="number" min="0" max="100" value="${r.vol}" data-aenderung="regel" data-i="${i}" data-feld="vol"></label>` : ""}
      </div>
    </div>`;
}

bereich("szenen-ereignisse", "szenen", ["daten", "medien"], () => {
  if (!regelEntwurf) regelEntwurf = structuredClone(S.ereignisse);
  const e = regelEntwurf;
  return `
    <div class="karte">
      <h2>Zufallsereignisse</h2>
      <p class="hinweis">Laufen, solange „Zufallsereignisse“ an ist. Jede Regel hat ihren eigenen Zufallstakt.</p>
      <label class="feld"><span>Mindestabstand zwischen zwei Ereignissen (s)</span>
        <input type="number" min="0" max="600" value="${e.min_abstand_s}" data-aenderung="regel-abstand"></label>
      ${e.regeln.map(regelZeile).join("")}
      <div class="knopfreihe">
        <button class="klein" data-aktion="regel-neu">+ Regel</button>
        <button class="klein" data-aktion="regeln-verwerfen">Verwerfen</button>
        <button class="primaer" data-aktion="regeln-speichern" style="margin-left:auto">Speichern</button>
      </div>
    </div>`;
});

// ---------------------------------------------------------------------------
// Seite: Einstellungen
// ---------------------------------------------------------------------------
const feld = (name, label, typ, wert, extra = "") =>
  `<label class="feld"><span>${esc(label)}</span><input type="${typ}" name="${name}" value="${esc(wert)}" ${extra}></label>`;
const kasten = (name, label, wert, hinweis = "") =>
  `<div class="schalter-zeile"><div>${esc(label)}${hinweis ? `<small>${esc(hinweis)}</small>` : ""}</div>
   <label class="schalter"><input type="checkbox" name="${name}" ${wert ? "checked" : ""}><i></i></label></div>`;
const auswahl = (name, label, liste, wert, extra = "") =>
  `<label class="feld"><span>${esc(label)}</span><select name="${name}" ${extra}>${optionen(liste, wert)}</select></label>`;

function abschnitt(id, titel, teil, inhalt, marke = "") {
  return `
    <details class="karte" id="${id}">
      <summary>${esc(titel)}${marke}</summary>
      <form class="inhalt" data-teil="${teil}">
        ${inhalt}
        ${teil ? '<div class="knopfreihe"><button type="submit" class="primaer" style="margin-left:auto">Speichern</button></div>' : ""}
      </form>
    </details>`;
}

function ausloeserZeile(a, i) {
  const ziele = a.typ === "kategorie" ? kategorien() : S.szenen.map((s) => [s.id, s.name]);
  return `
    <div class="schritt">
      <div class="schalter-zeile" style="padding-top:0">
        <input type="text" value="${esc(a.name)}" maxlength="40" data-aenderung="ausloeser" data-i="${i}" data-feld="name" style="flex:1">
        ${schalter("ausloeser", a.aktiv, `data-i="${i}" data-feld="aktiv"`)}
      </div>
      <label class="feld"><span>Sensor in Home Assistant</span>
        <input type="text" list="ha-liste" value="${esc(a.entity_id)}" placeholder="binary_sensor.…" data-aenderung="ausloeser" data-i="${i}" data-feld="entity_id"></label>
      <div class="feldreihe">
        <label class="feld"><span>Was passiert</span><select data-aenderung="ausloeser" data-i="${i}" data-feld="typ">${optionen([["szene", "Szene"], ["kategorie", "Sound aus Kategorie"]], a.typ)}</select></label>
        <label class="feld"><span>${a.typ === "szene" ? "Szene" : "Kategorie"}</span><select data-aenderung="ausloeser" data-i="${i}" data-feld="ziel">${optionen(ziele, a.ziel)}</select></label>
      </div>
      <div class="feldreihe">
        <label class="feld"><span>Sperrzeit danach (s)</span><input type="number" min="0" max="3600" value="${a.sperre_s}" data-aenderung="ausloeser" data-i="${i}" data-feld="sperre_s"></label>
        <label class="feld"><span>Ab Gruselstufe</span><select data-aenderung="ausloeser" data-i="${i}" data-feld="gruselstufe_min" data-typ="zahl">${optionen([[1, "1"], [2, "2"], [3, "3"]], a.gruselstufe_min)}</select></label>
        ${a.typ === "kategorie" ? `<label class="feld"><span>Lautstärke %</span><input type="number" min="0" max="100" value="${a.vol}" data-aenderung="ausloeser" data-i="${i}" data-feld="vol"></label>` : ""}
      </div>
      <div class="schalter-zeile"><div>Nur wenn die Show läuft</div>${schalter("ausloeser", a.nur_wenn_an, `data-i="${i}" data-feld="nur_wenn_an"`)}</div>
      <div class="knopfreihe">
        <button type="button" class="klein" data-aktion="ausloeser-test" data-id="${esc(a.id)}" ${a.id ? "" : "disabled"}>▶️ Jetzt testen</button>
        <button type="button" class="klein gefahr" data-aktion="ausloeser-loeschen" data-i="${i}" style="margin-left:auto">🗑 Entfernen</button>
      </div>
    </div>`;
}

bereich("einst-ausloeser", "einstellungen", ["daten", "zustand"], () => {
  if (!ausloeserEntwurf) ausloeserEntwurf = structuredClone(S.einstellungen.ausloeser);
  const liste = haSensoren || [];
  return `
    <details class="karte" id="e-ausloeser">
      <summary>🔔 Auslöser (Klingel, Personenerkennung)<span class="marke ${S.ha === "verbunden" ? "orange" : ""}">${esc(S.ha)}</span></summary>
      <div class="inhalt">
        <p class="hinweis">Ein Sensor aus Home Assistant (z. B. Reolink: <code>…_visitor</code> = Klingel gedrückt,
          <code>…_person</code> = Person erkannt) startet eine Szene oder einen Sound. Home Assistant muss unten eingerichtet sein.</p>
        <datalist id="ha-liste">${liste.map((x) => `<option value="${esc(x.entity_id)}">${esc(x.name)}</option>`).join("")}</datalist>
        <button type="button" class="klein" data-aktion="ha-sensoren">Sensoren aus Home Assistant laden</button>
        <div style="margin-top:10px">${ausloeserEntwurf.map(ausloeserZeile).join("")}</div>
        <div class="knopfreihe">
          <button type="button" class="klein" data-aktion="ausloeser-neu">+ Auslöser</button>
          <button type="button" class="klein" data-aktion="ausloeser-verwerfen">Verwerfen</button>
          <button type="button" class="primaer" data-aktion="ausloeser-speichern" style="margin-left:auto">Speichern</button>
        </div>
        <p class="hinweis">Tipp: In der Reolink-App eine Erkennungszone nur für euren Weg einstellen – sonst löst jeder auf dem Gehweg aus.</p>
      </div>
    </details>`;
});

bereich("einst-allgemein", "einstellungen", ["daten", "zustand"], () => {
  const e = S.einstellungen;
  const zp = e.zeitplan, rz = e.ruhezeit, gr = e.grusel, ha = e.homeassistant, mq = e.mqtt, so = e.standort;
  const statusMarke = (t) => `<span class="marke ${t === "verbunden" ? "orange" : ""}">${esc(t)}</span>`;
  return [
    abschnitt("e-zeitplan", "⏰ Zeitplan", "zeitplan", `
      ${kasten("aktiv", "Zeitplan aktiv", zp.aktiv, "schaltet Licht, Ambiente und Ereignisse")}
      ${auswahl("start_modus", "Start", [["sonnenuntergang", "Sonnenuntergang"], ["uhrzeit", "feste Uhrzeit"]], zp.start_modus)}
      <div class="feldreihe">
        ${feld("start", "Uhrzeit (bei fester Uhrzeit)", "time", zp.start)}
        ${feld("versatz_min", "Versatz zum Sonnenuntergang (min)", "number", zp.versatz_min, 'min="-180" max="180"')}
        ${feld("ende", "Ende", "time", zp.ende)}
      </div>
      ${kasten("ambiente", "Ambiente mit einschalten", zp.ambiente)}
      ${kasten("ereignisse", "Zufallsereignisse mit einschalten", zp.ereignisse)}`),
    abschnitt("e-ruhe", "🌙 Ruhezeit", "ruhezeit", `
      ${kasten("aktiv", "Ruhezeit aktiv", rz.aktiv, "Rücksicht auf die Nachbarn")}
      <div class="feldreihe">${feld("ab", "Ab", "time", rz.ab)}${feld("bis", "Bis", "time", rz.bis)}</div>
      ${auswahl("modus", "Was passiert", [["leiser", "leiser"], ["stumm", "Ton aus, Licht läuft weiter"]], rz.modus)}
      ${feld("lautst", "Lautstärke in der Ruhezeit (%)", "number", rz.lautst, 'min="0" max="100"')}`, S.ruhezeit ? statusMarke("jetzt aktiv") : ""),
    abschnitt("e-grusel", "💀 Gruselstufe nach Uhrzeit", "grusel", `
      ${kasten("auto", "Automatisch umschalten", gr.auto, "früh für Kinder, später richtig gruselig")}
      <div class="feldreihe">${feld("stufe2_ab", "Stufe 2 ab", "time", gr.stufe2_ab)}${feld("stufe3_ab", "Stufe 3 ab", "time", gr.stufe3_ab)}</div>`),
    abschnitt("e-ha", "🏠 Home Assistant", "homeassistant", `
      ${feld("url", "Adresse", "url", ha.url, 'placeholder="http://192.168.1.20:8123"')}
      ${feld("token", "Langzeit-Zugriffstoken", "password", ha.token, 'autocomplete="off"')}
      <p class="hinweis">Token anlegen: Home Assistant → Profil → Sicherheit → Langlebige Zugangstoken. Wird für die Auslöser gebraucht.</p>`, statusMarke(S.ha)),
    abschnitt("e-mqtt", "📡 MQTT", "mqtt", `
      ${kasten("aktiv", "MQTT aktiv", mq.aktiv, "Gerät „Spukzentrale“ erscheint automatisch in Home Assistant")}
      <div class="feldreihe">${feld("host", "Broker", "text", mq.host, 'placeholder="192.168.1.30"')}${feld("port", "Port", "number", mq.port, 'min="1" max="65535"')}</div>
      <div class="feldreihe">${feld("benutzer", "Benutzer", "text", mq.benutzer, 'autocomplete="off"')}${feld("passwort", "Passwort", "password", mq.passwort, 'autocomplete="off"')}</div>
      <div class="feldreihe">${feld("praefix", "Topic-Präfix", "text", mq.praefix)}${feld("discovery", "Discovery-Präfix", "text", mq.discovery)}</div>`, statusMarke(S.mqtt)),
    abschnitt("e-standort", "📍 Standort (für den Sonnenuntergang)", "standort", `
      <div class="feldreihe">${feld("breite", "Breitengrad", "number", so.breite, 'step="0.0001" min="-90" max="90"')}${feld("laenge", "Längengrad", "number", so.laenge, 'step="0.0001" min="-180" max="180"')}</div>`),
  ].join("");
});

bereich("einst-ton", "einstellungen", ["daten"], () => {
  const t = S.einstellungen.ton;
  return abschnitt("e-ton", "🔊 Ton", "", `
    ${regler("ton", "Gesamt", t.master, 'data-feld="master"', 0, 100, " %")}
    ${regler("ton", "Ambiente", t.musik_vol, 'data-feld="musik_vol"', 0, 100, " %")}
    ${regler("ton", "Effekte", t.fx_vol, 'data-feld="fx_vol"', 0, 100, " %")}
    ${regler("ton", "Ducking", t.duck, 'data-feld="duck"', 0, 100, " %")}
    ${regler("ton", "Latenz", t.latenz_ms, 'data-feld="latenz_ms"', 0, 300, " ms")}
    <button type="button" class="lila" data-aktion="latenz-test">Ton-Licht-Abgleich: Klick + Blitz</button>
    <p class="hinweis">Draußen stehen und „Klick + Blitz“ drücken: Latenz so einstellen, dass Klick und Blitz genau zusammenfallen. Mit dem DAC über AUX passen meist 40–80 ms.</p>`);
});

bereich("einst-licht", "einstellungen", ["daten"], () => {
  const l = S.einstellungen.licht;
  const zonen = [...Array(6).keys()].map((i) => `
    <div class="feldreihe" style="grid-template-columns: 1fr 64px 90px; ${i >= l.zonen ? "opacity:.45" : ""}">
      ${feld(`zonen_namen.${i}`, `Zone ${i + 1}`, "text", l.zonen_namen[i], 'maxlength="20"')}
      <label class="feld"><span>Farbe</span><input type="color" name="zonen_farben.${i}" value="${esc(l.zonen_farben[i])}"></label>
      ${feld(`zone_max.${i}`, "Max. %", "number", l.zone_max[i], 'min="0" max="100"')}
    </div>`).join("");
  return abschnitt("e-licht", "💡 Licht", "licht", `
    ${auswahl("zonen", "Anzahl Zonen", [1, 2, 3, 4, 5, 6].map((n) => [n, String(n)]), l.zonen, 'data-typ="zahl"')}
    <p class="hinweis">Die Anzahl muss zur Firmware passen (ZONEN_PINS in config.h). Die Farbe gilt nur für die Vorschau – z. B. wenn eine Farbfolie drauf ist.</p>
    ${zonen}
    <div class="feldreihe">
      ${feld("gamma", "Gamma", "number", l.gamma, 'step="0.1" min="1" max="3"')}
      ${feld("strobo_max_hz", "Strobo max. (Hz)", "number", l.strobo_max_hz, 'step="0.5" min="0.5" max="15"')}
    </div>
    <p class="hinweis warnung">⚠️ Stroboskop über 3 Hz kann bei Menschen mit Fotosensibilität Anfälle auslösen.</p>
    <h3>Zonen-Test</h3>
    <div class="knopfreihe">
      ${[...Array(l.zonen).keys()].map((i) => `<button type="button" class="klein" data-aktion="zone-test" data-zone="${i}">${i + 1}</button>`).join("")}
      <button type="button" class="klein" data-aktion="zone-test" data-zone="-1">alle</button>
    </div>`);
});

function montageplan(plan) {
  const { a, streifen, zonenInfo, abstand } = plan;
  const W = 400, rand = 22, m = (W - 2 * rand) / a.wand_m;
  const oben = a.einspeisung === "oben";
  const y0 = 64, hoehe = Math.min(120, Math.max(40, a.streifen_m * 70));
  const yKabel = oben ? y0 - 14 : y0 + hoehe + 14;
  const yKasten = oben ? 16 : y0 + hoehe + 24;
  const x = (meter) => rand + meter * m;
  const kabel = zonenInfo.map((z) => z.strecken.map((st) => {
    const versatz = (z.zone - (zonenInfo.length - 1) / 2) * 2.2;
    const yk = yKabel + (oben ? -versatz : versatz);
    const yAnschluss = oben ? y0 : y0 + hoehe;
    return `<polyline points="${x(st.von)},${st.bruecke ? yk : (oben ? yKasten + 22 : yKasten)} ${x(st.von)},${yk} ${x(st.nach)},${yk} ${x(st.nach)},${yAnschluss}"
      fill="none" stroke="${PLAN_FARBEN[z.zone]}" stroke-width="1.6" ${st.bruecke ? 'stroke-dasharray="3 2"' : ""}/>`;
  }).join("")).join("");
  const leds = streifen.map((st) => `
    <rect x="${x(st.x) - 2.5}" y="${y0}" width="5" height="${hoehe}" rx="2" fill="${st.gueltig ? PLAN_FARBEN[st.zone] : "#555"}" opacity=".9"/>
    <text x="${x(st.x)}" y="${oben ? y0 + hoehe + 12 : y0 - 6}" font-size="7" text-anchor="middle" fill="#a597b0">${st.x.toFixed(2).replace(".", ",")}</text>
    <text x="${x(st.x)}" y="${oben ? y0 + hoehe + 21 : y0 - 15}" font-size="7" text-anchor="middle" fill="${st.gueltig ? PLAN_FARBEN[st.zone] : "#888"}" font-weight="700">Z${st.zone + 1}</text>`).join("");
  const massY = oben ? y0 + hoehe + 38 : y0 - 30;
  const s0 = streifen[0], s1 = streifen[1];
  const abstandMass = s1 ? `
    <line x1="${x(s0.x)}" y1="${massY - 10}" x2="${x(s1.x)}" y2="${massY - 10}" stroke="#ffd166" stroke-width=".8" marker-start="url(#pfeil)" marker-end="url(#pfeil)"/>
    <text x="${(x(s0.x) + x(s1.x)) / 2}" y="${massY - 13}" font-size="7" text-anchor="middle" fill="#ffd166">${Math.round(abstand * 100)} cm</text>` : "";
  const kastenB = 44;
  return `
    <svg viewBox="0 0 ${W} ${y0 + hoehe + 58}" style="width:100%;height:auto;background:#07050a;border-radius:10px" role="img" aria-label="Montageplan">
      <defs><marker id="pfeil" viewBox="0 0 6 6" refX="3" refY="3" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0L6 3L0 6z" fill="#ffd166"/></marker></defs>
      <rect x="${rand}" y="${y0 - 30}" width="${W - 2 * rand}" height="${hoehe + 56}" fill="#15101c" stroke="#34283f"/>
      ${kabel}
      ${leds}
      <rect x="${x(a.kasten_m) - kastenB / 2}" y="${yKasten}" width="${kastenB}" height="22" rx="4" fill="#241b2d" stroke="#ff7a1a"/>
      <text x="${x(a.kasten_m)}" y="${yKasten + 14}" font-size="8" text-anchor="middle" fill="#f1e9f6">Kasten</text>
      ${abstandMass}
      <line x1="${x(0)}" y1="${massY}" x2="${x(a.wand_m)}" y2="${massY}" stroke="#a597b0" stroke-width=".8" marker-start="url(#pfeil)" marker-end="url(#pfeil)"/>
      <text x="${W / 2}" y="${massY + 10}" font-size="8" text-anchor="middle" fill="#a597b0">Wand ${String(a.wand_m).replace(".", ",")} m (Ansicht von vorne, Netz davor)</text>
    </svg>`;
}

bereich("einst-aufbau", "einstellungen", ["daten"], () => {
  const plan = aufbauBerechnen();
  const a = plan.a;
  const zonen = zonenAnzahl();
  const fmt = (n, st = 1) => n.toFixed(st).replace(".", ",");
  const zeilen = plan.zonenInfo.map((z) => `
    <tr>
      <td><span class="farbpunkt" style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${PLAN_FARBEN[z.zone]}"></span> ${esc(zonenName(z.zone))}</td>
      <td>${z.streifen.map((st) => fmt(st.x, 2)).join(" / ") || "–"} m</td>
      <td>${fmt(z.watt, 0)} W · ${fmt(z.ampere)} A</td>
      <td>${fmt(z.kabel)} m</td>
      <td class="${z.abfall > 0.5 ? "warnung" : ""}">${fmt(z.abfall, 2)} V</td>
    </tr>`).join("");
  const ungueltig = plan.streifen.filter((st) => !st.gueltig).length;
  const auslastung = (100 * plan.watt) / a.netzteil_w;
  const zuordnung = plan.streifen.map((st) => `
    <label class="feld" style="min-width:64px"><span>${fmt(st.x, 2)} m</span>
      <select name="zuordnung.${st.i}" data-typ="zahl">${optionen([...Array(zonen).keys()].map((z) => [z, `Zone ${z + 1}`]), st.zone)}</select></label>`).join("");
  return `
    <details class="karte" id="e-aufbau">
      <summary>📐 Aufbau & Montageplan</summary>
      <form class="inhalt" data-teil="aufbau">
        ${montageplan(plan)}
        <p class="hinweis">Durchgezogen: Zuleitung vom Kasten · gestrichelt: Brücke zum nächsten Streifen derselben Zone.
          Die Streifen einer Zone hängen parallel (+ an +, − an −).</p>
        <div style="overflow-x:auto"><table class="tabelle">
          <tr><th>Zone</th><th>Streifen bei</th><th>Last</th><th>Kabel</th><th>Abfall</th></tr>
          ${zeilen}
          <tr><th>Gesamt</th><th>${plan.streifen.length} × ${fmt(a.streifen_m, 2)} m</th><th>${fmt(plan.watt, 0)} W</th><th>${fmt(plan.kabel)} m</th><th></th></tr>
        </table></div>
        <p class="hinweis ${auslastung > 80 ? "warnung" : ""}">Netzteil: ${fmt(auslastung, 0)} % ausgelastet${auslastung > 80 ? " – über 80 %, größeres Netzteil oder weniger LED" : ""}.
          Kabel: zweiadrig, jeweils + und −, plus Reserve für Schlaufen.</p>
        ${ungueltig ? `<p class="hinweis warnung">${ungueltig} Streifen sind einer Zone zugeordnet, die es nicht gibt (Anzahl Zonen unter „Licht“).</p>` : ""}
        <div class="feldreihe">
          ${feld("wand_m", "Wandbreite (m)", "number", a.wand_m, 'step="0.05" min="0.1" max="50"')}
          ${feld("streifen", "Anzahl Streifen", "number", a.streifen, 'min="1" max="60"')}
          ${feld("streifen_m", "Länge je Streifen (m)", "number", a.streifen_m, 'step="0.05" min="0.05" max="10"')}
          ${feld("kasten_m", "Kasten bei (m von links)", "number", a.kasten_m, 'step="0.05" min="0" max="50"')}
          ${feld("zuleitung_extra_m", "Kasten bis Streifen-Anschluss (m, Höhe)", "number", a.zuleitung_extra_m, 'step="0.05" min="0" max="20"')}
          ${auswahl("einspeisung", "Einspeisung", [["oben", "oben"], ["unten", "unten"]], a.einspeisung)}
          ${feld("watt_pro_m", "LED-Leistung (W/m)", "number", a.watt_pro_m, 'step="0.5" min="0.5" max="50"')}
          ${auswahl("querschnitt_mm2", "Kabel-Querschnitt", [[0.5, "0,5 mm²"], [0.75, "0,75 mm²"], [1, "1,0 mm²"], [1.5, "1,5 mm²"], [2.5, "2,5 mm²"]], a.querschnitt_mm2, 'data-typ="zahl"')}
          ${feld("netzteil_w", "Netzteil (W)", "number", a.netzteil_w, 'min="10" max="2000"')}
        </div>
        <h3>Welcher Streifen hängt an welcher Zone?</h3>
        <div class="feldreihe" style="grid-template-columns:repeat(auto-fill,minmax(80px,1fr))">${zuordnung}</div>
        <div class="knopfreihe">
          <button type="button" class="klein" data-aktion="aufbau-gleichmaessig">Gleichmäßig verteilen</button>
          <button type="submit" class="primaer" style="margin-left:auto">Speichern & neu berechnen</button>
        </div>
      </form>
    </details>`;
});

bereich("einst-geraete", "einstellungen", ["geraete", "daten"], () => {
  const zeile = (rolle, titel) => {
    const g = S.geraete[rolle];
    if (!g?.verbunden) return `<li><div class="name">${titel}<small>nicht verbunden</small></div><span class="marke">offline</span></li>`;
    const s = g.status || {};
    // nur bei anderer Haupt-/Unterversion warnen (1.2.x passt zusammen; Patch-Versionen betreffen oft nur eine Seite)
    const hauptversion = (v) => String(v || "").split(".").slice(0, 2).join(".");
    const versionWarnung = hauptversion(g.fw) !== hauptversion(S.version) ? ` · ⚠️ Firmware ${esc(g.fw)} passt nicht zu Server ${esc(S.version)}` : "";
    const audioDiagnose = rolle === "sound" ? `
      <div style="margin-top:10px;padding:10px;border:1px solid var(--rand);border-radius:8px;background:var(--karte);">
        <strong>Audio-Diagnose</strong>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:8px;margin-top:8px;">
          <div><small>Musikstream</small><div>${s.musik ? (s.musik_spielt ? "Läuft" : "Verbindet …") : "Aus"}</div></div>
          <div><small>Stream-Puffer</small><div>${s.puffer ?? "?"} %</div></div>
          <div><small>Aussetzer gesamt</small><div style="font-weight:700;color:${(s.unterlaeufe ?? 0) > 0 ? "var(--orange)" : "inherit"}">${s.unterlaeufe ?? "?"}</div></div>
          <div><small>davon Puffer fast leer</small><div>${s.pcm_teilbloecke ?? "?"}</div></div>
          <div><small>davon Puffer ganz leer</small><div>${s.pcm_leerbloecke ?? "?"}</div></div>
          <div><small>MP3-Neupufferungen</small><div>${s.mp3_neupufferungen ?? "?"}</div></div>
          <div><small>PCM-Minimum</small><div>${s.pcm_min_bytes ?? "?"} Bytes</div></div>
          <div><small>Effekte aktiv</small><div>${s.stimmen ?? "?"}</div></div>
        </div>
        ${s.format_fehler ? '<p class="hinweis warnung">⚠️ Stream-Formatfehler gemeldet</p>' : ""}
        ${s.sync ? '<p class="hinweis">Effekte werden abgeglichen …</p>' : ""}
      </div>` : "";
    const geraeteDetails = rolle === "licht"
      ? `${s.fps ?? "?"} Bilder/s · ${s.uart ? "Kabel zum Sound-ESP ok" : "⚠️ kein Signal vom Sound-ESP"}`
      : `${s.uart ? "Kabel ok" : "⚠️ kein Signal vom Licht-ESP"}`;
    // Stabilität: Neustart-Grund, WLAN-Trennungen, längster Hänger der Hauptschleife (seit dem letzten Status)
    const stabil = s.neustart_grund === undefined ? "" : [
      `Start: ${esc(s.neustart_grund)}${["Strom an", "Reset-Taste", "Neustart"].includes(s.neustart_grund) ? "" : " ⚠️"}`,
      `WLAN-Trennungen: ${s.wlan_trennungen ?? 0}${s.wlan_trennungen ? ` (Grund ${s.wlan_grund})` : ""}`,
      `Hänger: ${s.schleife_max_ms ?? "?"} ms${(s.schleife_max_ms ?? 0) > 500 ? " ⚠️" : ""}${s.haenger_1s ? `, ${s.haenger_1s}× über 1 s` : ""}`,
      ...(rolle === "licht" && s.render_max_us !== undefined ? [`Licht-Takt max. ${(s.render_max_us / 1000).toFixed(1)} ms`] : []),
    ].join(" · ");
    return `
      <li>
        <div class="name">${titel} <span class="marke orange">online</span>
          <small>IP ${esc(g.ip)} · FW ${esc(g.fw)}${versionWarnung} · WLAN ${s.rssi ?? "?"} dBm · Speicher frei ${s.heap ? Math.round(s.heap / 1024) + " KB" : "?"}</small>
          <small>${geraeteDetails}</small>
          ${stabil ? `<small>${stabil}</small>` : ""}
          ${audioDiagnose}
        </div>
        <button type="button" class="klein" data-aktion="neustart" data-rolle="${rolle}">Neustart</button>
      </li>`;
  };
  return abschnitt("e-geraete", "🛰️ Geräte", "", `
    <ul class="liste">${zeile("licht", "Licht-ESP")}${zeile("sound", "Sound-ESP")}</ul>
    <p class="hinweis">Spukzentrale ${esc(S.version)} · <a href="https://github.com/xNoVoSx/spukzentrale" style="color:var(--lila)">GitHub</a> · REST-API: <a href="/docs" style="color:var(--lila)">/docs</a></p>`);
});

bereich("geraete-punkte", "*", ["geraete"], () =>
  ["licht", "sound"].map((r) => `<span class="${S.geraete[r]?.verbunden ? "ok" : ""}">${r === "licht" ? "Licht" : "Sound"}</span>`).join(""));

// ---------------------------------------------------------------------------
// Formulare (Einstellungen)
// ---------------------------------------------------------------------------
function formDaten(form) {
  const d = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    let w;
    if (el.type === "checkbox") w = el.checked;
    else if (el.type === "number" || el.type === "range" || el.dataset.typ === "zahl") w = Number(el.value);
    else w = el.value;
    const teile = el.name.split(".");
    if (teile.length === 2) {
      (d[teile[0]] ??= [])[Number(teile[1])] = w;
    } else {
      d[el.name] = w;
    }
  }
  return d;
}

document.addEventListener("submit", async (e) => {
  const form = e.target.closest("form[data-teil]");
  if (!form || !form.dataset.teil) return;
  e.preventDefault();
  await api("PUT", `/api/einstellungen/${form.dataset.teil}`, formDaten(form));
  form.querySelector("button[type=submit]")?.blur();
  toast("Gespeichert");
});

document.addEventListener("toggle", (e) => {
  const d = e.target;
  if (d.tagName === "DETAILS" && d.id) d.open ? offeneDetails.add(d.id) : offeneDetails.delete(d.id);
}, true);

// ---------------------------------------------------------------------------
// Dialoge
// ---------------------------------------------------------------------------
function dialog(html, beimSpeichern) {
  const d = $("#dialog");
  const f = $("#dialog-inhalt");
  f.innerHTML = html + `
    <div class="dialog-knoepfe">
      <button value="abbrechen" formnovalidate>Abbrechen</button>
      <button value="ok" class="primaer">Speichern</button>
    </div>`;
  d.returnValue = "";
  d.showModal();
  return new Promise((ok) => {
    d.onclose = async () => {
      if (d.returnValue === "ok") ok(await beimSpeichern(f));
      else ok(null);
    };
  });
}

function textAbfrage(titel, wert = "") {
  return dialog(`<h2>${esc(titel)}</h2><input type="text" name="text" maxlength="60" value="${esc(wert)}" required autofocus>`,
    (f) => f.elements.text.value.trim());
}

function lichtAktionFelder(a, praefix = "licht", mitKeine = true, dauerNull = true) {
  a = a || { name: "keine", zonen: [], dauer_ms: 0, staerke: 100, param: 0 };
  const namen = Object.entries(AKTION_NAMEN).filter(([k]) => (mitKeine || k !== "keine") && k !== "test");
  return `
    ${auswahl(`${praefix}_name`, "Licht", namen, a.name)}
    <div class="feldreihe">
      ${feld(`${praefix}_dauer_ms`, dauerNull ? "Dauer ms (0 = Länge des Sounds)" : "Dauer ms", "number", a.dauer_ms, `min="${dauerNull ? 0 : 50}" max="60000" step="50"`)}
      ${feld(`${praefix}_staerke`, "Stärke %", "number", a.staerke, 'min="0" max="100"')}
      ${feld(`${praefix}_param`, "Dunkel vorher ms (Schreck)", "number", a.param, 'min="0" max="65535"')}
    </div>
    <div class="feld"><span>Zonen (keine = alle)</span><div class="knopfreihe">
      ${[...Array(zonenAnzahl()).keys()].map((i) => `<label class="marke" style="padding:6px 10px"><input type="checkbox" name="${praefix}_zone" value="${i}" ${a.zonen.includes(i) ? "checked" : ""}> ${esc(zonenName(i))}</label>`).join("")}
    </div></div>`;
}

function lichtAktionLesen(f, praefix = "licht") {
  const name = f.elements[`${praefix}_name`].value;
  if (name === "keine") return null;
  return {
    name,
    dauer_ms: Number(f.elements[`${praefix}_dauer_ms`].value),
    staerke: Number(f.elements[`${praefix}_staerke`].value),
    param: Number(f.elements[`${praefix}_param`].value),
    zonen: $$(`input[name=${praefix}_zone]:checked`, f).map((x) => Number(x.value)),
  };
}

async function fxBearbeiten(id) {
  const fx = S.medien.fx.find((x) => x.id === id);
  if (!fx) return;
  const daten = await dialog(`
    <h2>Effekt bearbeiten</h2>
    ${feld("name", "Name", "text", fx.name, 'maxlength="60" required')}
    <div class="feldreihe">
      ${auswahl("kategorie", "Kategorie", kategorien(), fx.kategorie)}
      ${feld("vol", "Lautstärke %", "number", fx.vol, 'min="0" max="100"')}
    </div>
    ${lichtAktionFelder(fx.licht)}
    <p class="hinweis">„Blitze zum Sound“ und „Puls zum Sound“ folgen der Lautstärke des Effekts – ideal für Donner und Herzschlag.</p>`,
  (f) => ({ name: f.elements.name.value, kategorie: f.elements.kategorie.value, vol: Number(f.elements.vol.value), licht: lichtAktionLesen(f) }));
  if (daten) await api("PUT", `/api/fx/${id}`, daten);
}

// --- Szenen-Editor ---
function schrittFelder(s, i) {
  const typen = [["fx", "🔊 Sound"], ["aktion", "💡 Licht"], ["stimmung", "🎨 Stimmung"]];
  let inhalt = "";
  if (s.typ === "fx") {
    const quellen = [["", "– bestimmter Sound –"], ...kategorien().map(([k, n]) => [`k:${k}`, `zufällig: ${n}`]),
      ...S.medien.fx.filter((f) => f.kategorie !== "system").map((f) => [f.id, f.name])];
    inhalt = `
      <div class="feldreihe">
        <label class="feld"><span>Sound</span><select data-schritt="${i}" data-feld="quelle">${optionen(quellen, s.fx_id || (s.kategorie ? "k:" + s.kategorie : ""))}</select></label>
        <label class="feld"><span>Lautstärke %</span><input type="number" min="0" max="100" value="${s.vol}" data-schritt="${i}" data-feld="vol"></label>
        <label class="feld"><span>Licht</span><select data-schritt="${i}" data-feld="licht">${optionen([["auto", "wie beim Sound"], ["keine", "kein Licht"], ["eigen", "eigenes"]], s.licht)}</select></label>
      </div>
      ${s.licht === "eigen" ? `<div data-schritt-aktion="${i}">${lichtAktionFelder(s.aktion, `a${i}`, false)}</div>` : ""}`;
  } else if (s.typ === "aktion") {
    inhalt = `<div data-schritt-aktion="${i}">${lichtAktionFelder(s.aktion || { name: "blitz", zonen: [], dauer_ms: 1000, staerke: 100, param: 0 }, `a${i}`, false, false)}</div>`;
  } else {
    inhalt = `
      <div class="feldreihe">
        <label class="feld"><span>Stimmung</span><select data-schritt="${i}" data-feld="stimmung_id">${optionen(S.stimmungen.map((x) => [x.id, x.name]), s.stimmung_id)}</select></label>
        <label class="feld"><span>Dauer (s)</span><input type="number" min="0.1" max="600" step="0.1" value="${s.dauer_ms / 1000}" data-schritt="${i}" data-feld="dauer_s"></label>
      </div>`;
  }
  return `
    <div class="schritt">
      <div class="schritt-kopf">
        <input type="number" min="0" max="600" step="0.1" value="${s.t_ms / 1000}" data-schritt="${i}" data-feld="t_s" title="Startzeit in Sekunden">
        <select data-schritt="${i}" data-feld="typ">${optionen(typen, s.typ)}</select>
        <button type="button" class="klein symbol gefahr" data-aktion="schritt-loeschen" data-i="${i}">🗑</button>
      </div>
      ${inhalt}
    </div>`;
}

function editorZeichnen() {
  const e = entwurf;
  $("#editor-schritte").innerHTML = e.schritte.map(schrittFelder).join("") || '<p class="leer">Noch keine Schritte.</p>';
}

function editorUebernehmen() {
  // Felder der Licht-Aktionen in den Entwurf übernehmen (die übrigen werden live übernommen)
  const f = $("#dialog-inhalt");
  entwurf.schritte.forEach((s, i) => {
    if (f.elements[`a${i}_name`]) s.aktion = lichtAktionLesen(f, `a${i}`);
  });
}

async function szeneBearbeiten(id) {
  const vorlage = id ? S.szenen.find((s) => s.id === id) : { name: "Neue Szene", gruselstufe: 1, schritte: [] };
  entwurf = structuredClone(vorlage);
  const promise = dialog(`
    <h2>${id ? "Szene bearbeiten" : "Neue Szene"}</h2>
    <div class="feldreihe">
      <label class="feld"><span>Name</span><input type="text" id="editor-name" maxlength="40" value="${esc(entwurf.name)}" required></label>
      <label class="feld"><span>Gruselstufe</span><select id="editor-stufe">${optionen([[1, "1 · Kinder"], [2, "2 · Gruselig"], [3, "3 · Schreck"]], entwurf.gruselstufe)}</select></label>
    </div>
    <p class="hinweis">Zeiten in Sekunden ab Start. Licht-Schritte laufen auf derselben Zeitleiste wie der Ton.</p>
    <div id="editor-schritte"></div>
    <div class="knopfreihe">
      <button type="button" class="klein" data-aktion="schritt-neu" data-typ="fx">+ Sound</button>
      <button type="button" class="klein" data-aktion="schritt-neu" data-typ="aktion">+ Licht</button>
      <button type="button" class="klein" data-aktion="schritt-neu" data-typ="stimmung">+ Stimmung</button>
    </div>`,
  () => {
    editorUebernehmen();
    entwurf.name = $("#editor-name").value.trim() || "Szene";
    entwurf.gruselstufe = Number($("#editor-stufe").value);
    entwurf.schritte.sort((a, b) => a.t_ms - b.t_ms);
    return entwurf;
  });
  editorZeichnen();
  const ergebnis = await promise;
  if (!ergebnis) return;
  if (id) await api("PUT", `/api/szenen/${id}`, ergebnis);
  else await api("POST", "/api/szenen", ergebnis);
  toast("Szene gespeichert");
}

document.addEventListener("change", (e) => {
  const el = e.target;
  if (el.dataset.schritt === undefined || !entwurf) return;
  const s = entwurf.schritte[Number(el.dataset.schritt)];
  editorUebernehmen();
  const f = el.dataset.feld;
  if (f === "t_s") s.t_ms = Math.round(Number(el.value) * 1000);
  else if (f === "dauer_s") s.dauer_ms = Math.round(Number(el.value) * 1000);
  else if (f === "vol") s.vol = Number(el.value);
  else if (f === "quelle") {
    s.fx_id = el.value && !el.value.startsWith("k:") ? el.value : null;
    s.kategorie = el.value.startsWith("k:") ? el.value.slice(2) : (S.medien.fx.find((x) => x.id === el.value)?.kategorie ?? null);
  } else s[f] = el.value;
  if (f === "typ" || f === "licht") editorZeichnen();
});

// ---------------------------------------------------------------------------
// Aktionen (Klicks)
// ---------------------------------------------------------------------------
const AKTIONEN = {
  show: () => api("POST", "/api/show", { an: !S.zustand.licht.an }),
  gruselstufe: (el) => api("POST", "/api/gruselstufe", { stufe: Number(el.dataset.stufe) }),
  "ambiente-weiter": () => api("POST", "/api/ambiente/weiter"),
  "alles-stopp": () => api("POST", "/api/stopp"),
  "szene-start": (el) => api("POST", `/api/szenen/${el.dataset.id}/start`),
  "szene-stopp": () => api("POST", "/api/szenen/stopp"),
  "fx-kategorie": (el) => api("POST", `/api/fx/kategorie/${el.dataset.kategorie}/abspielen`, {}),
  "fx-spielen": (el) => api("POST", `/api/fx/${el.dataset.id}/abspielen`, {}),
  "licht-aktion": (el) => {
    const dauern = { blitz: 900, schreck: 2500, blackout: 3000, welle: 1500, puls: 1800, alle_an: 2000 };
    return api("POST", "/api/aktion", { name: el.dataset.name, dauer_ms: dauern[el.dataset.name] ?? 1000, param: el.dataset.name === "schreck" ? 900 : 0 });
  },
  stimmung: (el) => api("POST", `/api/stimmungen/${el.dataset.id}/anwenden`),
  "stimmung-speichern": async () => {
    const name = await textAbfrage("Stimmung speichern");
    if (name) await api("POST", "/api/stimmungen", { name });
  },
  "stimmung-umbenennen": async (el) => {
    const s = S.stimmungen.find((x) => x.id === el.dataset.id);
    const name = await textAbfrage("Stimmung umbenennen", s.name);
    if (name) await api("PUT", `/api/stimmungen/${s.id}`, { ...s, name });
  },
  "stimmung-loeschen": async (el) => {
    if (confirm("Stimmung löschen?")) await api("DELETE", `/api/stimmungen/${el.dataset.id}`);
  },
  "zone-test": (el) => api("POST", "/api/zonen/test", { zone: Number(el.dataset.zone) }),
  "zone-alle": (el) => api("POST", "/api/zonen/alle", S.zustand.licht.zonen[Number(el.dataset.zone)]),
  vorhoeren: (el) => {
    if (!vorhoerer.paused && vorhoerer.dataset.url === el.dataset.url) {
      vorhoerer.pause();
      vorhoerer.removeAttribute("src");
      return;
    }
    vorhoerer.dataset.url = el.dataset.url;
    vorhoerer.src = el.dataset.url;
    vorhoerer.play().catch(() => toast("Abspielen im Browser nicht möglich", true));
    toast("Vorhören im Browser – nochmal tippen zum Stoppen");
  },
  "musik-hochladen": () => hochladen("#musik-datei", "/api/musik", () => ({})),
  "musik-hoch": (el) => {
    const i = Number(el.dataset.i);
    const r = [...S.medien.playlist.reihenfolge];
    [r[i - 1], r[i]] = [r[i], r[i - 1]];
    return api("PUT", "/api/playlist", { ...S.medien.playlist, reihenfolge: r });
  },
  "musik-umbenennen": async (el) => {
    const s = S.medien.musik.find((x) => x.id === el.dataset.id);
    const name = await textAbfrage("Stück umbenennen", s.name);
    if (name) await api("PUT", `/api/musik/${s.id}`, { name });
  },
  "musik-loeschen": async (el) => {
    if (confirm("Stück löschen?")) await api("DELETE", `/api/musik/${el.dataset.id}`);
  },
  "fx-hochladen": () => hochladen("#fx-datei", "/api/fx", () => ({ kategorie: $("#fx-kategorie").value, name: $("#fx-name").value })),
  "fx-bearbeiten": (el) => fxBearbeiten(el.dataset.id),
  "fx-loeschen": async (el) => {
    if (confirm("Effekt löschen? Er wird auch vom Sound-ESP entfernt.")) await api("DELETE", `/api/fx/${el.dataset.id}`);
  },
  "szene-neu": () => szeneBearbeiten(null),
  "szene-bearbeiten": (el) => szeneBearbeiten(el.dataset.id),
  "szene-loeschen": async (el) => {
    if (confirm("Szene löschen?")) await api("DELETE", `/api/szenen/${el.dataset.id}`);
  },
  "schritt-neu": (el) => {
    editorUebernehmen();
    const letzte = entwurf.schritte.reduce((m, s) => Math.max(m, s.t_ms), -2000);
    const s = { t_ms: Math.max(0, letzte + 2000), typ: el.dataset.typ, fx_id: null, kategorie: null, vol: 100, licht: "auto", aktion: null, stimmung_id: null, dauer_ms: 10000 };
    if (s.typ === "fx") s.kategorie = "donner";
    if (s.typ === "aktion") s.aktion = { name: "blitz", zonen: [], dauer_ms: 1000, staerke: 100, param: 0 };
    if (s.typ === "stimmung") s.stimmung_id = S.stimmungen[0]?.id ?? null;
    entwurf.schritte.push(s);
    editorZeichnen();
  },
  "schritt-loeschen": (el) => {
    editorUebernehmen();
    entwurf.schritte.splice(Number(el.dataset.i), 1);
    editorZeichnen();
  },
  "regel-neu": () => {
    regelEntwurf.regeln.push({ id: "", aktiv: true, typ: "kategorie", ziel: "donner", min_s: 120, max_s: 300, gruselstufe_min: 1, vol: 100 });
    zeichne(BEREICHE.find((b) => b.id === "szenen-ereignisse"));
  },
  "regel-loeschen": (el) => {
    regelEntwurf.regeln.splice(Number(el.dataset.i), 1);
    zeichne(BEREICHE.find((b) => b.id === "szenen-ereignisse"));
  },
  "regeln-verwerfen": () => {
    regelEntwurf = null;
    zeichne(BEREICHE.find((b) => b.id === "szenen-ereignisse"));
  },
  "regeln-speichern": async () => {
    await api("PUT", "/api/ereignisse/regeln", regelEntwurf);
    regelEntwurf = null;
    toast("Regeln gespeichert");
  },
  "ha-sensoren": async () => {
    toast("Lade Sensoren …");
    haSensoren = await api("GET", "/api/ha/sensoren");
    toast(`${haSensoren.length} Sensoren gefunden – im Feld „Sensor“ auswählen`);
    zeichne(BEREICHE.find((b) => b.id === "einst-ausloeser"));
  },
  "ausloeser-neu": () => {
    ausloeserEntwurf.push({ id: "", name: "Neuer Auslöser", aktiv: true, entity_id: "", typ: "kategorie", ziel: "schrei", vol: 100, sperre_s: 60, gruselstufe_min: 1, nur_wenn_an: true });
    zeichne(BEREICHE.find((b) => b.id === "einst-ausloeser"));
  },
  "ausloeser-loeschen": (el) => {
    ausloeserEntwurf.splice(Number(el.dataset.i), 1);
    zeichne(BEREICHE.find((b) => b.id === "einst-ausloeser"));
  },
  "ausloeser-verwerfen": () => {
    ausloeserEntwurf = null;
    zeichne(BEREICHE.find((b) => b.id === "einst-ausloeser"));
  },
  "ausloeser-speichern": async () => {
    await api("PUT", "/api/ausloeser", ausloeserEntwurf);
    ausloeserEntwurf = null;
    toast("Auslöser gespeichert");
  },
  "ausloeser-test": (el) => api("POST", `/api/ausloeser/${el.dataset.id}?test=true`),
  "aufbau-gleichmaessig": () => api("PUT", "/api/einstellungen/aufbau", { zuordnung: [] }),
  "latenz-test": () => api("POST", "/api/latenz/test"),
  neustart: async (el) => {
    if (confirm("Gerät neu starten?")) await api("POST", `/api/geraete/${el.dataset.rolle}/neustart`);
  },
};

async function hochladen(feldSel, pfad, zusatz) {
  const dateien = [...($(feldSel)?.files || [])];
  if (!dateien.length) return toast("Erst eine Datei auswählen", true);
  for (const [n, datei] of dateien.entries()) {
    toast(`${datei.name}: wird hochgeladen und umgewandelt (${n + 1}/${dateien.length}) …`);
    const fd = new FormData();
    fd.append("datei", datei);
    for (const [k, v] of Object.entries(zusatz())) if (v) fd.append(k, dateien.length > 1 && k === "name" ? "" : v);
    try {
      await api("POST", pfad, fd, true);
    } catch {
      return;
    }
  }
  toast(dateien.length > 1 ? `${dateien.length} Dateien fertig` : "Fertig");
}

document.addEventListener("click", async (e) => {
  const reiter = e.target.closest("#reiter button");
  if (reiter) return zeigeSeite(reiter.dataset.seite);
  const el = e.target.closest("[data-aktion]");
  if (!el || el.disabled) return;
  const f = AKTIONEN[el.dataset.aktion];
  if (!f) return;
  e.preventDefault();
  el.disabled = true;
  try {
    await f(el, e);
  } catch (err) {
    console.error(err);
  } finally {
    el.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Änderungen (Regler, Schalter, Auswahl)
// ---------------------------------------------------------------------------
const sendeVerzoegert = (() => {
  const timer = {};
  return (schluessel, fn, ms = 120) => {
    clearTimeout(timer[schluessel]);
    timer[schluessel] = setTimeout(fn, ms);
  };
})();

document.addEventListener("input", (e) => {
  const el = e.target;
  if (el.type === "range" && el.nextElementSibling?.tagName === "OUTPUT") el.nextElementSibling.value = el.value + (el.dataset.einheit || "");
  if (el.type === "range" && el.dataset.aenderung) aendern(el, true);
});

document.addEventListener("change", (e) => {
  const el = e.target;
  if (el.dataset.aenderung) aendern(el, false);
  if (el.type === "range") el.blur(); // damit der Bereich gleich wieder aktualisiert werden darf
});

function aendern(el, live) {
  const a = el.dataset.aenderung;
  const wert = el.type === "checkbox" ? el.checked : el.type === "range" || el.type === "number" || el.dataset.typ === "zahl" ? Number(el.value) : el.value;
  const schicke = (fn) => (live ? sendeVerzoegert(a + (el.dataset.zone ?? "") + (el.dataset.feld ?? ""), fn) : fn());
  switch (a) {
    case "ambiente": return api("POST", "/api/ambiente", { an: wert });
    case "ereignisse": return api("POST", "/api/ereignisse", { an: wert });
    case "hell": return schicke(() => api("POST", "/api/licht", { hell: wert }));
    case "musik-reaktiv": return schicke(() => api("POST", "/api/licht", { musik_reaktiv: wert }));
    case "lautst": return schicke(() => api("PUT", "/api/einstellungen/ton", { master: wert }));
    case "ton": return schicke(() => api("PUT", "/api/einstellungen/ton", { [el.dataset.feld]: wert }));
    case "zone": {
      const i = Number(el.dataset.zone);
      const zone = { ...S.zustand.licht.zonen[i], [el.dataset.feld]: wert };
      S.zustand.licht.zonen[i] = zone; // sofort merken, damit schnelle Änderungen nicht verloren gehen
      return schicke(() => api("PUT", `/api/zonen/${i}`, zone));
    }
    case "playlist-zufall": return api("PUT", "/api/playlist", { ...S.medien.playlist, zufall: wert });
    case "playlist-ueberblend": return schicke(() => api("PUT", "/api/playlist", { ...S.medien.playlist, ueberblend_s: wert }), 300);
    case "regel": {
      const r = regelEntwurf.regeln[Number(el.dataset.i)];
      r[el.dataset.feld] = wert;
      if (el.dataset.feld === "typ") {
        r.ziel = wert === "kategorie" ? "donner" : S.szenen[0]?.id ?? "";
        zeichne(BEREICHE.find((b) => b.id === "szenen-ereignisse"));
      }
      return;
    }
    case "regel-abstand":
      regelEntwurf.min_abstand_s = wert;
      return;
    case "ausloeser": {
      const a = ausloeserEntwurf[Number(el.dataset.i)];
      a[el.dataset.feld] = wert;
      if (el.dataset.feld === "typ") {
        a.ziel = wert === "kategorie" ? "schrei" : S.szenen[0]?.id ?? "";
        zeichne(BEREICHE.find((b) => b.id === "einst-ausloeser"));
      }
      return;
    }
  }
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
for (const name of ["show", "licht", "sounds", "szenen", "einstellungen"]) {
  $(`#seite-${name}`).innerHTML = BEREICHE.filter((b) => b.seite === name).map((b) => `<div id="${b.id}"></div>`).join("");
}
let gemerkt = "show";
try { gemerkt = localStorage.getItem("spuk-seite") || "show"; } catch { /* egal */ }
zeigeSeite(["show", "licht", "sounds", "szenen", "einstellungen"].includes(gemerkt) ? gemerkt : "show");
verbinden();
