"""KI-Regisseur: steuert während eines Besuchs das Horror-Haus – Geräusche, Licht, Szenen und die Musik.

Standardmäßig aus (Zustand.regie_an). Ohne Besuch läuft alles wie bisher über den Zufalls-Generator. Der Regisseur
darf Sounds (mit Licht), reine Licht-Effekte, kurze Szenen (z. B. Stromausfall, Werwolf) und die Hintergrundmusik
(Totenstille, leiser, düsteres Stück) einsetzen. Er entscheidet zu zwei Anlässen:
  - direkt nach einem Satz der KI-Stimme (Pointe: Lachen, Blitz … oder nichts), auch nach dem Abschied
  - in Pausen, solange die Leute auf dem Weg/an der Tür sind (frühestens regie_abstand_s nach dem letzten Effekt)
Was passiert, entscheidet Claude (Anthropic-API, Modell regie_modell) – nur mit Text: Lage, Gemmas Beschreibung der
Leute, letzter Satz, Musiklage. Kamerabilder verlassen das Haus nie. Ist die API weg, zu langsam oder lehnt sie ab,
entscheidet das lokale Sprachmodell (Gemma). WANN es passiert, bestimmt der Code: Sounds warten (höchstens ein paar
Sekunden) auf eine passende Stelle der Musik (abstimmung.planen – nicht in den Gesang, ruhige Stelle, Schlag).

Gegen Durcheinander:
  - nur Kategorien aus aktiven Zufallsregeln, die zur Gruselstufe passen (dieselbe Auswahl wie der Generator),
    keine Stimmen-Kategorien, keine Sounds länger als regie_max_s, Szenen höchstens regie_szene_max_s lang
    und nur bis zur Gruselstufe der Show
  - nie während die KI spricht, nie während einer Szene, nicht nach dem Klingeln, nicht in einer Besuchs-Pause
  - solange ein Regie-Effekt läuft (belegt_bis), wartet die KI-Stimme mit dem Abspielen bis er vorbei ist
  - feste Regeln: höchstens ein Musik-Eingriff pro Besuch, nach einem heftigen Schreck RUHE_NACH_SCHRECK_S nichts
    Heftiges, in Pausen nur, solange wirklich jemand im Bild ist
  - Fehler oder Zeitüberschreitung bei beiden Modellen: einfach kein Effekt
"""

import asyncio
import collections
import json
import logging
import os
import time
from typing import TYPE_CHECKING

import anthropic
import httpx

from . import abstimmung
from .modelle import KATEGORIEN, LichtAktion

if TYPE_CHECKING:
    from .zentrale import Zentrale

log = logging.getLogger(__name__)

NICHT_FUER_REGIE = {"stimme", "reaktion", "system", "spieluhr", "sonstiges"}   # Spieluhr: eigene Musik, nur gezielt
# nur Licht – Name: (Aktion, Dauer ms, Beschreibung fürs Modell). Kein „Flackern“: ohne Sound dimmt es nur leicht nach unten
# und war an der Wand unsichtbar (Prüfbericht 31.10. nachts) – flackerndes Licht gibt es über die Licht-Szenen.
LICHT = {
    "licht_blitz": ("blitz", 400, "ein greller Blitz"),
    "licht_welle": ("welle", 2000, "eine Lichtwelle läuft über die Wand"),
    "licht_puls": ("puls", 1500, "das Licht pulsiert einmal auf"),
}
MUSIK = {
    "musik_stille": "Hintergrundmusik verstummt plötzlich für 8 Sekunden (Totenstille – sehr unheimlich)",
    "musik_leise": "Hintergrundmusik wird für 15 Sekunden ganz leise (man hört jedes Geräusch)",
    "musik_duester": "zu einem düsteren, bedrohlichen Musikstück wechseln",
}
SYSTEM = (
    "Du bist der unsichtbare Regisseur eines Horror-Hauses an Halloween. Du steuerst Licht, Geräusche, kurze "
    "Szenen und die Hintergrundmusik. Deine Aufgabe: den Besuchern, die die Kamera gerade sieht, eine echte "
    "Gänsehaut verpassen – sie sollen sich gruseln, erschrecken und es danach weitererzählen.\n"
    "So arbeitest du wie ein guter Horrorfilm:\n"
    "- Spannung aufbauen statt Dauerbeschallung: erst leise, unheimliche Dinge (Musik verstummt, Flüstern, "
    "Schritte, Herzschlag, flackerndes Licht, ein Geist zieht vorbei), dann ein Schreck (Blackout + Blitz, Schrei, "
    "Werwolf). Stille und Dunkelheit sind deine stärksten Werkzeuge. Nach einem Schreck kurz Ruhe.\n"
    "- Auf dem Weg zum Haus: neugierig machen und unheimlich locken, nicht verscheuchen. An der Tür vor dem "
    "Klingeln: nur Spannung (Stille, Flüstern, Herzschlag, flackerndes Licht) – den großen Schreck löst die Klingel "
    "aus, nimm ihn nicht vorweg. Beim Weggehen: ein letzter leiser Gruß (Lachen, Flüstern).\n"
    "- Wird es dunkel, muss danach etwas Gruseliges passieren – Dunkelheit ohne Folge wirkt wie ein Defekt.\n"
    "- Publikum: Die Show läuft ab 17–18 Uhr, meist Schulkinder, Jugendliche und Erwachsene – die darfst du richtig "
    "erschrecken. Siehst du doch Kleinkinder (an der Hand, Kinderwagen): gruselig, aber nicht verstören – keine "
    "Schreie, keine Kettensäge, kein Stroboskop. Die Gruselstufe der Show ist die Obergrenze.\n"
    "- Abwechslung: nicht zweimal dasselbe hintereinander. Die Geisterstimme spricht selbst – du ergänzt sie.\n"
    "- Die Hintergrundmusik ist Teil der Inszenierung: du kennst ihre Stimmung und was gleich in ihr passiert "
    "(Gesang, ruhige Stelle). Ein Musik-Eingriff (Totenstille, leise, Wechsel) wirkt nur selten – höchstens einmal "
    "pro Besuch. Den genauen Zeitpunkt deines Effekts legt die Technik passend zur Musik fest; du wählst, WAS "
    "passiert.\n"
    "Antworte als JSON: {\"effekt\": eine ID aus der Liste, \"grund\": höchstens 12 Wörter}"
)
VERLAUF = 15
# heftige Effekte: danach RUHE_NACH_SCHRECK_S nichts Heftiges mehr (Spannungsbogen statt Dauerfeuer)
HEFTIG = {"schrei", "kettensaege", "werwolf", "zombie", "klopfen", "szene_schreck", "szene_kettensaege",
          "szene_strobo-salve", "szene_werwolf", "szene_donner-aus-dem-dunkel"}
RUHE_NACH_SCHRECK_S = 25
TUER_GEDULD_S = 20             # an der Tür vor dem Klingeln so lange nur Spannung – der Schreck gehört der Klingel
MUSIK_WARTEN_MAX_S = 4.0       # so lange darf ein Sound auf eine passende Stelle der Musik warten
SCHEMA_GRUND = {"type": "string"}


class Regie:
    def __init__(self, z: "Zentrale") -> None:
        self.z = z
        self.belegt_bis = 0.0          # monotonic: bis dahin läuft ein Regie-Effekt
        self.letzte_aktion = 0.0       # monotonic
        self.ki_ende = 0.0             # monotonic: Ende des letzten KI-Satzes
        self.letzte: collections.deque[tuple[str, float]] = collections.deque(maxlen=5)   # (Effekt, monotonic)
        self.verlauf: collections.deque[dict] = collections.deque(maxlen=VERLAUF)
        self.denkt = False
        self.musik_eingriff = False    # in diesem Besuch schon an der Musik gedreht
        self.letzter_schreck = 0.0     # monotonic
        self._claude: anthropic.AsyncAnthropic | None = None

    def neuer_besuch(self) -> None:
        self.musik_eingriff = False

    # ------------------------------------------------------------------
    @property
    def aktiv(self) -> bool:
        z = self.z
        return (z.zustand.regie_an and z.zustand.ki_an and z.zustand.licht.an and not z.besuch.pausiert
                and bool(z.einst.ki.llm_url or self.claude_bereit))

    @property
    def claude_bereit(self) -> bool:
        return bool(self.z.einst.ki.regie_modell and os.environ.get("ANTHROPIC_API_KEY"))

    def frei_in(self) -> float:
        """Sekunden, bis der laufende Regie-Effekt vorbei ist."""
        return max(0.0, self.belegt_bis - time.monotonic())

    def info(self) -> dict:
        return {"an": self.z.zustand.regie_an, "aktiv": self.aktiv, "verlauf": list(self.verlauf),
                "optionen": list(self.optionen()), "claude": self.claude_bereit,
                "modell": self.z.einst.ki.regie_modell}

    def optionen(self) -> dict[str, str]:
        """ID -> Beschreibung fürs Modell: Kategorien aus aktiven Zufallsregeln (Gruselstufe), reine Licht-Effekte,
        kurze Szenen bis zur Gruselstufe, Musik-Steuerung."""
        z = self.z
        stufe = z.zustand.gruselstufe
        max_ms = z.einst.ki.regie_max_s * 1000
        # alle Kategorien mit Sounds – unabhängig davon, ob ihre Zufallsregel an ist (die steuert nur den Zufall);
        # die Gruselstufe einer Regel gilt aber auch hier
        stufe_min: dict[str, int] = {}
        for r in z.ereignisse.regeln:
            if r.typ == "kategorie":
                stufe_min[r.ziel] = min(stufe_min.get(r.ziel, 3), r.gruselstufe_min)
        kats = [k for k in KATEGORIEN if k not in NICHT_FUER_REGIE and stufe_min.get(k, 1) <= stufe]
        aus = {"keiner": "kein Effekt, Stille wirkt auch"}
        # einzelne Sounds mit Beschreibung (eigene oder aus der Analyse) – so kann der Regisseur gezielt wählen
        for k in kats:
            for f in z.medien.fx_der_kategorie(k):
                if f.hoerbar_ms <= max_ms:
                    aus[f"fx_{f.id}"] = f"Geräusch ({KATEGORIEN.get(k, k)}, {f.hoerbar_ms / 1000:.0f} s, mit Licht): {f.text}"
        for n, (_, _, text) in LICHT.items():
            aus[n] = f"nur Licht: {text}"
        if z.spieler:
            # den Schreck der Klingel nicht vorwegnehmen
            klingel = {a.ziel for a in z.einst.ausloeser if a.aktiv and a.rolle == "klingel" and a.typ == "szene"}
            for s in z.szenen.liste:
                if s.id in klingel:
                    continue
                dauer = z.spieler.dauer_ms(s)
                if s.gruselstufe <= stufe and 0 < dauer <= z.einst.ki.regie_szene_max_s * 1000 and self._szene_geht(s):
                    aus[f"szene_{s.id}"] = f"Szene „{s.name}“ ({dauer / 1000:.0f} s): {self._szene_text(s)}"
        b = z.besuch
        tuer_geduld = b.zustand == "tuer" and time.time() - b.seit < TUER_GEDULD_S
        if tuer_geduld or time.monotonic() - self.letzter_schreck < RUHE_NACH_SCHRECK_S:
            for e in [e for e in aus if self._heftig(e)]:
                del aus[e]
        if z.zustand.ambiente_an and not self.musik_eingriff and not z.musik_daempfung:
            aus.update(MUSIK)
            if not any(m.analyse and m.analyse.stimmung in ("duester", "dramatisch") and m.id != z.ambiente.jetzt_id
                       for m in z.medien.daten.musik):
                del aus["musik_duester"]
        return aus

    def _name(self, effekt: str, aus: dict[str, str]) -> str:
        """Kurzer Name für Protokoll und Oberfläche."""
        if effekt.startswith("fx_"):
            f = self.z.medien.fx(effekt[3:])
            return f"{KATEGORIEN.get(f.kategorie, f.kategorie)}: {f.name}" if f else effekt
        return aus.get(effekt, effekt).split(" (")[0] if effekt.startswith("szene_") else aus.get(effekt, effekt)

    def _heftig(self, effekt: str) -> bool:
        if effekt.startswith("fx_"):
            f = self.z.medien.fx(effekt[3:])
            return bool(f and f.kategorie in HEFTIG)
        return effekt in HEFTIG

    def _szene_geht(self, s) -> bool:
        """Szenen, deren Sounds alle fehlen, wären nur halbe Szenen – nur nehmen, wenn jeder Sound-Schritt klingt."""
        m = self.z.medien
        return all(sch.typ != "fx" or (sch.fx_id and m.fx(sch.fx_id)) or (sch.kategorie and m.fx_der_kategorie(sch.kategorie))
                   for sch in s.schritte)

    def _szene_text(self, s) -> str:
        teile = []
        for sch in s.schritte:
            if sch.typ == "fx":
                teile.append(KATEGORIEN.get(sch.kategorie or "", "Sound"))
            elif sch.typ == "aktion" and sch.aktion:
                teile.append(f"Licht {sch.aktion.name}")
            elif sch.typ == "stimmung":
                st = next((x for x in self.z.stimmungen.liste if x.id == sch.stimmung_id), None)
                teile.append(f"Licht „{st.name if st else '?'}“")
            elif sch.typ == "musik":
                teile.append("Musik verstummt" if sch.musik == "stille" else "Musik leise")
        return " → ".join(teile)

    def _darf(self) -> bool:
        z = self.z
        return (self.aktiv and not self.denkt and not z.ki.laeuft and not (z.spieler and z.spieler.laufend)
                and self.frei_in() == 0 and time.monotonic() >= z.spieluhr_bis)   # Spieluhr = eigene Musik

    # ------------------------------------------------------------------
    async def nach_satz(self, satz: str, dauer_ms: int) -> None:
        """Die KI-Stimme hat gerade einen Satz gestartet: nach dessen Ende ggf. eine Pointe setzen."""
        self.ki_ende = time.monotonic() + dauer_ms / 1000
        if not self.aktiv or self.z.besuch.zustand not in ("gelockt", "tuer", "geht"):
            return
        await asyncio.sleep(dauer_ms / 1000 + 0.3)
        if self.z.besuch.zustand in ("gelockt", "tuer", "geht") and self._darf():
            await self.entscheiden("direkt nach diesem Satz der Geisterstimme", satz)

    async def lauf(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                await self.takt(time.monotonic())
            except Exception:  # noqa: BLE001
                log.exception("Regie-Takt")

    async def takt(self, jetzt: float) -> None:
        k = self.z.einst.ki
        b = self.z.besuch
        if b.zustand not in ("gelockt", "tuer") or not (b.an["fern"] or b.an["nah"]) or not self._darf():
            return
        if jetzt - self.letzte_aktion < k.regie_abstand_s or jetzt - self.ki_ende < 4:
            return
        # seit wann nichts passiert – höchstens seit Beginn dieser Phase des Besuchs
        ruhe = int(min(jetzt - max(self.letzte_aktion, self.ki_ende), time.time() - b.seit))
        await self.entscheiden(f"Pause – seit {ruhe} Sekunden ist nichts passiert")

    # ------------------------------------------------------------------
    async def entscheiden(self, anlass: str, satz: str = "") -> str:
        """Modell fragen und den gewählten Effekt spielen. Ergebnis: die gewählte ID ("" bei Fehler)."""
        z = self.z
        aus = self.optionen()
        self.denkt = True
        self.letzte_aktion = time.monotonic()
        eintrag = {"zeit": time.time(), "anlass": anlass, "effekt": "", "grund": "", "ms": 0}
        t0 = time.monotonic()
        try:
            effekt, grund, eintrag["modell"] = await self._fragen(anlass, satz, aus)
        except Exception as e:  # noqa: BLE001 – kein Effekt, Show läuft weiter
            eintrag["grund"] = f"Fehler: {e or type(e).__name__}"
            log.warning("Regie: %s", eintrag["grund"])
            return ""
        finally:
            self.denkt = False
            eintrag["ms"] = int((time.monotonic() - t0) * 1000)
            if eintrag["grund"].startswith("Fehler"):
                self._merken(eintrag)
        eintrag["effekt"], eintrag["grund"] = effekt, grund
        eintrag["name"] = self._name(effekt, aus)
        # Lage kann sich in der Denkzeit geändert haben (geklingelt, KI spricht, Szene …)
        if effekt != "keiner" and (z.besuch.zustand == "geklingelt" or z.ki.laeuft or (z.spieler and z.spieler.laufend)):
            eintrag["grund"] = "verworfen – inzwischen passiert etwas anderes"
            effekt = "keiner"
        if effekt != "keiner":
            await self._spielen(effekt)
            self.letzte.append((effekt, time.monotonic()))
            if self._heftig(effekt):
                self.letzter_schreck = time.monotonic()
            if effekt in MUSIK:
                self.musik_eingriff = True
            if not effekt.startswith("szene_"):     # Szenen trägt der Szenen-Spieler selbst ein
                z.protokollieren(f"Regie: {eintrag['name']} – {grund}", "fx")
        self.letzte_aktion = time.monotonic()
        self._merken(eintrag)
        return effekt

    def _merken(self, eintrag: dict) -> None:
        self.verlauf.appendleft(eintrag)
        self.z.an_ui({"t": "regie", "eintrag": eintrag})

    def _musik_text(self) -> str:
        """Was gerade in der Hintergrundmusik passiert – damit der Regisseur nicht blind hineinspielt."""
        z = self.z
        if not z.zustand.ambiente_an:
            return "Musik: aus."
        if z.musik_daempfung:
            return f"Musik: gerade gedämpft ({'Totenstille' if z.musik_daempfung[0] == 'stille' else 'leise'})."
        stueck, pos = z.musik_lage()
        if stueck is None or pos is None:
            return f"Musik: läuft{f' – „{z.ambiente.jetzt}“' if z.ambiente.jetzt else ''}."
        teile = [f"Musik: „{stueck.name}“, bei {pos:.0f} von {stueck.dauer_s:.0f} s"]
        a = stueck.analyse
        if a:
            teile.append(f"Stimmung {a.stimmung}" + (f", {a.bpm:.0f} BPM" if a.bpm else ""))
            ruhig = next(((b0, b1) for b0, b1 in a.ruhig if b1 > pos), None)
            if ruhig and ruhig[0] <= pos:
                teile.append(f"gerade eine ruhige Stelle (noch {ruhig[1] - pos:.0f} s)")
            elif ruhig and ruhig[0] - pos <= 20:
                teile.append(f"in {ruhig[0] - pos:.0f} s kommt eine ruhige Stelle")
        gesang = next(((g0, g1) for g0, g1 in stueck.stimme if g1 > pos), None)
        if gesang and gesang[0] <= pos:
            teile.append(f"gerade Gesang (noch {gesang[1] - pos:.0f} s)")
        elif gesang and gesang[0] - pos <= 20:
            teile.append(f"in {gesang[0] - pos:.0f} s setzt Gesang ein")
        return ", ".join(teile) + "."

    async def _fragen(self, anlass: str, satz: str, aus: dict[str, str]) -> tuple[str, str, str]:
        """(Effekt, Grund, Modell) – erst Claude, sonst das lokale Modell."""
        z = self.z
        b = z.besuch
        ki = z.ki.verlauf[0] if z.ki.verlauf else {}
        jetzt = time.monotonic()
        teile = [
            f"Lage: {b.info()['text']} (seit {int(time.time() - b.seit)} s; Weg: {'jemand da' if b.an['fern'] else 'leer'}, "
            f"Haustür: {'jemand da' if b.an['nah'] else 'leer'}).",
            self._musik_text(),
            f"Was die Kamera zuletzt gesehen hat: {ki.get('beschreibung') or 'unbekannt'}",
            f"Zuletzt gesagt: „{satz or ki.get('satz') or '–'}“",
            f"Anlass: {anlass}.",
            f"Gruselstufe {z.zustand.gruselstufe} von 3.",
        ]
        if self.letzte:
            teile.append("Deine letzten Effekte: " + ", ".join(f"{e} (vor {jetzt - t:.0f} s)" for e, t in self.letzte))
        teile.append("Auswahl:\n" + "\n".join(f"- {i}: {t}" for i, t in aus.items()))
        text = "\n".join(teile)
        schema = {"type": "object", "additionalProperties": False, "required": ["effekt", "grund"],
                  "properties": {"effekt": {"type": "string", "enum": list(aus)}, "grund": SCHEMA_GRUND}}
        if self.claude_bereit:
            try:
                return (*await self._claude_fragen(text, schema, aus), _modell_name(z.einst.ki.regie_modell))
            except Exception as e:  # noqa: BLE001 – Internet weg, zu langsam, abgelehnt: das lokale Modell übernimmt
                log.warning("Regie: Claude nicht verfügbar (%s) – lokales Modell", e or type(e).__name__)
        if not z.einst.ki.llm_url:
            raise RuntimeError("kein Modell erreichbar")
        return (*await self._lokal_fragen(text, schema, aus), "Gemma (lokal)")

    async def _claude_fragen(self, text: str, schema: dict, aus: dict[str, str]) -> tuple[str, str]:
        k = self.z.einst.ki
        if self._claude is None:
            self._claude = anthropic.AsyncAnthropic(max_retries=0)    # Schlüssel: ANTHROPIC_API_KEY
        r = await self._claude.beta.messages.create(
            model=k.regie_modell,
            max_tokens=4000,
            system=SYSTEM,
            messages=[{"role": "user", "content": text}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            timeout=k.regie_zeitlimit_s,
        )
        if r.stop_reason == "refusal":
            raise RuntimeError("Anfrage abgelehnt")
        antwort = next(b.text for b in r.content if b.type == "text")
        return _lesen(antwort, aus)

    async def _lokal_fragen(self, text: str, schema: dict, aus: dict[str, str]) -> tuple[str, str]:
        k = self.z.einst.ki
        anfrage = {
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
            "max_tokens": 80,
            "temperature": 0.8,
            "response_format": {"type": "json_schema", "json_schema": {"name": "regie", "schema": schema}},
        }
        if k.modell:
            anfrage["model"] = k.modell
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(f"{k.llm_url.rstrip('/')}/v1/chat/completions", json=anfrage,
                             headers={"Connection": "close"})
        if r.status_code != 200:
            raise RuntimeError(f"Sprachmodell: HTTP {r.status_code}")
        return _lesen(r.json()["choices"][0]["message"]["content"], aus)

    async def _spielen(self, effekt: str) -> None:
        z = self.z
        if effekt in MUSIK:
            if effekt == "musik_duester":
                await z.musik_duester("Regie")
            else:
                dauer = 8000 if effekt == "musik_stille" else 15000
                await z.musik_daempfen("stille" if effekt == "musik_stille" else "leise", dauer)
            return
        if effekt.startswith("szene_"):
            s = next((x for x in z.szenen.liste if x.id == effekt[6:]), None)
            if s and z.spieler:
                await z.spieler.starten(s, "Regie")
                self.belegt_bis = time.monotonic() + z.spieler.dauer_ms(s) / 1000
            return
        if effekt in LICHT:
            name, dauer, _ = LICHT[effekt]
            await z.aktion(LichtAktion(name=name, dauer_ms=dauer), dauer_ms=dauer, quelle="Regie")
            self.belegt_bis = time.monotonic() + dauer / 1000
            return
        if effekt.startswith("fx_"):
            f = z.medien.fx(effekt[3:])
            if f is None:
                return
        else:                                   # Kategorie (ältere Antworten/Tests): zufälliger passender Sound
            max_ms = z.einst.ki.regie_max_s * 1000
            kandidaten = [f for f in z.medien.fx_der_kategorie(effekt) if f.hoerbar_ms <= max_ms]
            if not kandidaten:
                return
            f = z.fx_waehlen(effekt)
            if f is None or f.hoerbar_ms > max_ms:
                f = kandidaten[0]
        # Zeitpunkt passend zur Musik: nicht in den Gesang, gern in eine ruhige Stelle, auf den Schlag –
        # aber die Leute stehen jetzt da: höchstens MUSIK_WARTEN_MAX_S warten
        self.belegt_bis = time.monotonic() + MUSIK_WARTEN_MAX_S + f.hoerbar_ms / 1000 + 0.3
        gewartet = False
        for _ in range(3):
            stueck, pos = z.musik_lage()
            plan = abstimmung.planen(stueck, pos, f.kategorie, f.hoerbar_ms / 1000, z.einst.musik, gewartet)
            if plan.jetzt:
                if plan.verzoegerung_s > 0:
                    await asyncio.sleep(plan.verzoegerung_s)
                break
            if gewartet or plan.warten_s > MUSIK_WARTEN_MAX_S:
                break
            await asyncio.sleep(plan.warten_s)
            gewartet = True
        if z.besuch.zustand == "geklingelt" or z.ki.laeuft or (z.spieler and z.spieler.laufend):
            self.belegt_bis = 0.0
            return
        await z.fx_spielen(f)
        self.belegt_bis = time.monotonic() + f.hoerbar_ms / 1000 + 0.3


def _lesen(antwort: str, aus: dict[str, str]) -> tuple[str, str]:
    d = json.loads(antwort)
    effekt = str(d.get("effekt", "keiner"))
    return (effekt if effekt in aus else "keiner"), str(d.get("grund", ""))[:160]


def _modell_name(modell: str) -> str:
    return {"claude-opus-5-5": "Claude Opus 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5"}.get(modell, modell)
