"""KI-Regisseur: wählt während eines Besuchs die Effekte (Geräusch mit Licht oder nur Licht) passend zur Lage.

Standardmäßig aus (Zustand.regie_an). Ohne Besuch läuft alles wie bisher über den Zufalls-Generator, die
Hintergrundmusik bleibt immer unberührt. Der Regisseur entscheidet zu zwei Anlässen:
  - direkt nach einem Satz der KI-Stimme (Pointe: Lachen, Blitz … oder nichts), auch nach dem Abschied
  - in Pausen, solange die Leute auf dem Weg/an der Tür sind (frühestens regie_abstand_s nach dem letzten Effekt)
Die Entscheidung trifft dasselbe Sprachmodell wie die Stimme, nur mit Text (Lage, letzte Beschreibung, letzter
Satz) – ohne Bild, dauert deshalb nur einen Bruchteil einer Sekunde.

Gegen Durcheinander:
  - nur Kategorien aus aktiven Zufallsregeln, die zur Gruselstufe passen (dieselbe Auswahl wie der Generator),
    keine Stimmen-Kategorien, keine Sounds länger als regie_max_s
  - nie während die KI spricht, nie während einer Szene, nicht nach dem Klingeln, nicht in einer Besuchs-Pause
  - solange ein Regie-Effekt läuft (belegt_bis), wartet die KI-Stimme mit dem Abspielen bis er vorbei ist
  - Fehler oder Zeitüberschreitung: einfach kein Effekt
"""

import asyncio
import collections
import json
import logging
import time
from typing import TYPE_CHECKING

import httpx

from .modelle import KATEGORIEN, LichtAktion

if TYPE_CHECKING:
    from .zentrale import Zentrale

log = logging.getLogger(__name__)

NICHT_FUER_REGIE = {"stimme", "reaktion", "system"}
# nur Licht – Name: (Aktion, Dauer ms, Beschreibung fürs Modell)
LICHT = {
    "licht_blitz": ("blitz", 400, "ein greller Blitz"),
    "licht_flackern": ("flackern", 2500, "das Licht flackert unruhig"),
    "licht_welle": ("welle", 2000, "eine Lichtwelle läuft über die Wand"),
    "licht_blackout": ("blackout", 2500, "plötzlich ist alles dunkel (2,5 s)"),
    "licht_puls": ("puls", 1500, "das Licht pulsiert einmal auf"),
}
SYSTEM = (
    "Du bist der Regisseur eines Halloween-Spukhauses. Gerade sind Besucher da (oft Kinder). Eine Geisterstimme "
    "spricht sie an; du entscheidest, welcher Effekt JETZT passt – oder keiner. Ziel: Spannung aufbauen, gruselig "
    "aber kinderfreundlich. Kleine Kinder oder ängstlich wirkende Besucher: sanft (Herzschlag, Wind, leises "
    "Licht). Mutige Jugendliche oder Erwachsene: ruhig heftiger. Abwechslung: nicht dasselbe wie zuletzt. "
    "Lieber auch mal keiner als Dauerbeschallung. Direkt nach einem Satz passt oft eine kurze Pointe "
    "(Lachen, Blitz) – zu einem Abschied etwas Leises.\n"
    "Antworte als JSON: {\"effekt\": eine ID aus der Liste, \"grund\": höchstens 12 Wörter}"
)
VERLAUF = 15


class Regie:
    def __init__(self, z: "Zentrale") -> None:
        self.z = z
        self.belegt_bis = 0.0          # monotonic: bis dahin läuft ein Regie-Effekt
        self.letzte_aktion = 0.0       # monotonic
        self.ki_ende = 0.0             # monotonic: Ende des letzten KI-Satzes
        self.letzte: collections.deque[str] = collections.deque(maxlen=4)
        self.verlauf: collections.deque[dict] = collections.deque(maxlen=VERLAUF)
        self.denkt = False

    # ------------------------------------------------------------------
    @property
    def aktiv(self) -> bool:
        z = self.z
        return (z.zustand.regie_an and z.zustand.ki_an and z.zustand.licht.an and not z.besuch.pausiert
                and bool(z.einst.ki.llm_url))

    def frei_in(self) -> float:
        """Sekunden, bis der laufende Regie-Effekt vorbei ist."""
        return max(0.0, self.belegt_bis - time.monotonic())

    def info(self) -> dict:
        return {"an": self.z.zustand.regie_an, "aktiv": self.aktiv, "verlauf": list(self.verlauf),
                "optionen": list(self.optionen())}

    def optionen(self) -> dict[str, str]:
        """ID -> Beschreibung fürs Modell: Kategorien aus aktiven Zufallsregeln (Gruselstufe) + reine Licht-Effekte."""
        z = self.z
        stufe = z.zustand.gruselstufe
        max_ms = z.einst.ki.regie_max_s * 1000
        kats: list[str] = []
        for r in z.ereignisse.regeln:
            if (r.aktiv and r.typ == "kategorie" and r.gruselstufe_min <= stufe and r.ziel not in NICHT_FUER_REGIE
                    and r.ziel not in kats and any(f.dauer_ms <= max_ms for f in z.medien.fx_der_kategorie(r.ziel))):
                kats.append(r.ziel)
        aus = {"keiner": "kein Effekt, Stille wirkt auch"}
        for k in kats:
            aus[k] = f"Geräusch „{KATEGORIEN.get(k, k)}“ mit passendem Licht"
        for n, (_, _, text) in LICHT.items():
            aus[n] = f"nur Licht: {text}"
        return aus

    def _darf(self) -> bool:
        z = self.z
        return (self.aktiv and not self.denkt and not z.ki.laeuft and not (z.spieler and z.spieler.laufend)
                and self.frei_in() == 0)

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
        if self.z.besuch.zustand not in ("gelockt", "tuer") or not self._darf():
            return
        if jetzt - self.letzte_aktion < k.regie_abstand_s or jetzt - self.ki_ende < 4:
            return
        ruhe = int(jetzt - max(self.letzte_aktion, self.ki_ende))
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
            effekt, grund = await self._fragen(anlass, satz, aus)
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
        # Lage kann sich in der Denkzeit geändert haben (geklingelt, KI spricht, Szene …)
        if effekt != "keiner" and (z.besuch.zustand == "geklingelt" or z.ki.laeuft or (z.spieler and z.spieler.laufend)):
            eintrag["grund"] = "verworfen – inzwischen passiert etwas anderes"
            effekt = "keiner"
        if effekt != "keiner":
            await self._spielen(effekt)
            self.letzte.append(effekt)
            z.protokollieren(f"Regie: {aus.get(effekt, effekt)} – {grund}", "fx")
        self.letzte_aktion = time.monotonic()
        self._merken(eintrag)
        return effekt

    def _merken(self, eintrag: dict) -> None:
        self.verlauf.appendleft(eintrag)
        self.z.an_ui({"t": "regie", "eintrag": eintrag})

    async def _fragen(self, anlass: str, satz: str, aus: dict[str, str]) -> tuple[str, str]:
        z = self.z
        k = z.einst.ki
        b = z.besuch
        ki = z.ki.verlauf[0] if z.ki.verlauf else {}
        teile = [
            f"Lage: {b.info()['text']} (Weg: {'jemand da' if b.an['fern'] else 'leer'}, "
            f"Haustür: {'jemand da' if b.an['nah'] else 'leer'}).",
            f"Was die Kamera zuletzt gesehen hat: {ki.get('beschreibung') or 'unbekannt'}",
            f"Zuletzt gesagt: „{satz or ki.get('satz') or '–'}“",
            f"Anlass: {anlass}.",
            f"Gruselstufe {z.zustand.gruselstufe} von 3.",
        ]
        if self.letzte:
            teile.append("Deine letzten Effekte: " + ", ".join(self.letzte))
        teile.append("Auswahl:\n" + "\n".join(f"- {i}: {t}" for i, t in aus.items()))
        anfrage = {
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(teile)}],
            "max_tokens": 80,
            "temperature": 0.8,
            "response_format": {"type": "json_schema", "json_schema": {"name": "regie", "schema": {
                "type": "object",
                "properties": {"effekt": {"type": "string", "enum": list(aus)}, "grund": {"type": "string"}},
                "required": ["effekt", "grund"],
            }}},
        }
        if k.modell:
            anfrage["model"] = k.modell
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(f"{k.llm_url.rstrip('/')}/v1/chat/completions", json=anfrage,
                             headers={"Connection": "close"})
        if r.status_code != 200:
            raise RuntimeError(f"Sprachmodell: HTTP {r.status_code}")
        d = json.loads(r.json()["choices"][0]["message"]["content"])
        effekt = str(d.get("effekt", "keiner"))
        return (effekt if effekt in aus else "keiner"), str(d.get("grund", ""))[:120]

    async def _spielen(self, effekt: str) -> None:
        z = self.z
        if effekt in LICHT:
            name, dauer, _ = LICHT[effekt]
            await z.aktion(LichtAktion(name=name, dauer_ms=dauer), dauer_ms=dauer, quelle="Regie")
            self.belegt_bis = time.monotonic() + dauer / 1000
            return
        max_ms = z.einst.ki.regie_max_s * 1000
        kandidaten = [f for f in z.medien.fx_der_kategorie(effekt) if f.dauer_ms <= max_ms]
        if not kandidaten:
            return
        f = z.fx_waehlen(effekt)
        if f is None or f.dauer_ms > max_ms:
            f = kandidaten[0]
        await z.fx_spielen(f)
        self.belegt_bis = time.monotonic() + f.dauer_ms / 1000 + 0.3
