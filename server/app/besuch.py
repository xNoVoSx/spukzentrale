"""Besuchs-Ablauf der KI-Stimme: fasst Erkennungen von Weg (fern), Haustür (nah) und Klingel zu einem Besuch zusammen.

Zustände:
  leer       niemand da
  gelockt    auf dem Weg angesprochen („komm näher“), höchstens locken_max-mal
  tuer       an der Haustür angesprochen („klingel, wenn du dich traust“) – genau einmal pro Besuch
  geklingelt Klingel gedrückt: Schreck-Szene läuft über den normalen Auslöser, danach tuer_ruhe_s still
  geht       die Leute gehen: höchstens ein Abschied, nie wieder locken
  nachlauf   Besuch vorbei (besuch_ende_s ohne Person), noch nachlauf_s still – Leute auf dem Rückweg

Grundregeln gegen Durcheinander:
  - Es spricht immer nur eine KI-Stimme (KiStimme.laeuft), während einer Szene spricht sie gar nicht.
  - Zufallsereignisse pausieren, solange ein Besuch läuft (siehe ereignisse.Generator).
  - Wer geht, wird nie zurückgelockt; nach dem Klingeln bleibt die KI still.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .modelle import Ausloeser

if TYPE_CHECKING:
    from .zentrale import Zentrale

log = logging.getLogger(__name__)

LOCKEN = ("Die Leute sind noch ein Stück entfernt auf dem Weg zum Haus. Kommen sie oder stehen sie: sprich sie an und "
          "locke sie näher zur Haustür. Gehen sie weg: verabschiede dich gruselig und locke sie NICHT zurück.")
ZOEGERN = ("Du hast sie vor {s} Sekunden schon angesprochen („{satz}“), sie sind aber nicht näher gekommen – vielleicht "
           "haben sie ein bisschen Angst. Ermutige sie sanft und freundlich, ohne Druck. Gehen sie weg: verabschiede "
           "dich freundlich und locke sie NICHT zurück.")
TUER = "Die Leute stehen direkt vor der Haustür – fordere sie auf, die Klingel zu drücken, wenn sie sich trauen."
ABSCHIED = ("Die Leute gehen gerade wieder weg. Verabschiede dich gruselig-freundlich (zum Beispiel: kommt bald "
            "wieder). Locke sie auf KEINEN Fall zurück und fordere sie zu nichts auf.")

TEXTE = {"leer": "niemand da", "gelockt": "auf dem Weg angesprochen", "tuer": "an der Haustür",
         "geklingelt": "hat geklingelt", "geht": "geht wieder", "nachlauf": "vorbei (Nachlauf)"}


class Besuch:
    def __init__(self, z: "Zentrale") -> None:
        self.z = z
        self.zustand = "leer"
        self.seit = time.time()
        self.gelockt = 0
        self.tuer_gesagt = False
        self.abschied_gesagt = False
        self.letzte_ansprache = 0.0        # monotonic
        self.letzter_satz = ""
        self.klingel_zeit = 0.0            # monotonic
        self.an: dict[str, bool] = {"fern": False, "nah": False}
        self.zuletzt_jemand = 0.0          # monotonic: letzter Moment mit Person
        self.pause_bis = 0.0               # time.time()
        self.fern: Ausloeser | None = None  # für Nachfassen per Takt (Kamera, Lautstärke)
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    @property
    def aktiv(self) -> bool:
        """Läuft ein Besuch? Dann pausieren die Zufallsereignisse."""
        return self.zustand in ("gelockt", "tuer", "geklingelt", "geht") or self.z.ki.laeuft

    @property
    def pausiert(self) -> bool:
        return time.time() < self.pause_bis

    def info(self) -> dict:
        return {"zustand": self.zustand, "text": TEXTE[self.zustand], "seit": self.seit, "gelockt": self.gelockt,
                "tuer": self.tuer_gesagt, "abschied": self.abschied_gesagt, "fern": self.an["fern"],
                "nah": self.an["nah"], "pause_bis": self.pause_bis if self.pausiert else 0}

    def pause(self, minuten: float) -> None:
        self.pause_bis = time.time() + minuten * 60 if minuten > 0 else 0
        self.z.protokollieren(f"KI-Besuch: Pause {minuten:g} min" if minuten > 0 else "KI-Besuch: Pause beendet", "info")
        self._melden()

    def _wechsel(self, neu: str) -> None:
        if neu == self.zustand:
            return
        if neu == "gelockt" and self.zustand in ("leer", "nachlauf"):
            self._neu()
        self.zustand, self.seit = neu, time.time()
        log.info("Besuch: %s", neu)
        self._melden()

    def _neu(self) -> None:
        self.gelockt = 0
        self.tuer_gesagt = self.abschied_gesagt = False
        self.letzter_satz = ""
        self.z.ki.letzte.clear()

    def _melden(self) -> None:
        self.z.an_ui({"t": "besuch", **self.info()})

    def _darf_sprechen(self) -> bool:
        if self.pausiert or self.z.ki.laeuft:
            return False
        return not (self.z.spieler and self.z.spieler.laufend)

    # ------------------------------------------------------------------
    async def sensor(self, a: Ausloeser, an: bool, quelle: str) -> None:
        """Personensensor mit Rolle fern/nah hat geschaltet."""
        jetzt = time.monotonic()
        self.an[a.rolle] = an
        if an:
            self.zuletzt_jemand = jetzt
        if a.rolle == "fern":
            self.fern = a
        if not an:
            self._melden()
            return
        st = self.zustand
        if a.rolle == "fern":
            if st in ("leer",):
                self._wechsel("gelockt")
                self.gelockt = 1
                await self._sprechen(a, quelle, LOCKEN, ersatz=True)
            elif st == "gelockt":
                await self._nachfassen(a, quelle)
            elif st == "tuer" and not self.an["nah"]:
                await self._abschied(a, quelle)                  # waren an der Tür, sind jetzt wieder auf dem Weg
            elif st == "geklingelt" and jetzt - self.klingel_zeit >= self.z.einst.ki.tuer_ruhe_s:
                await self._abschied(a, quelle)
            # geht / nachlauf / geklingelt (Ruhe): still
        else:  # nah
            if st in ("tuer", "geklingelt") or self.tuer_gesagt:
                return
            if st in ("leer", "nachlauf"):
                self._neu()
            self._wechsel("tuer")
            self.tuer_gesagt = True
            await self._sprechen(a, quelle, TUER, ersatz=True)

    def klingel(self) -> None:
        """Klingel gedrückt: Szene läuft über den normalen Auslöser – KI hört auf und bleibt still."""
        self.z.ki.abbrechen = True
        self.klingel_zeit = time.monotonic()
        self.zuletzt_jemand = self.klingel_zeit
        self._wechsel("geklingelt")

    async def _nachfassen(self, a: Ausloeser, quelle: str) -> None:
        k = self.z.einst.ki
        if self.gelockt >= k.locken_max or self.an["nah"]:
            return
        if time.monotonic() - self.letzte_ansprache < k.locken_wiederholen_s:
            return
        self.gelockt += 1
        lage = ZOEGERN.format(s=int(time.monotonic() - self.letzte_ansprache), satz=self.letzter_satz or "…")
        await self._sprechen(a, quelle, lage, reaktion=False)

    async def _abschied(self, a: Ausloeser, quelle: str) -> None:
        self._wechsel("geht")
        if self.z.einst.ki.abschied and not self.abschied_gesagt:
            self.abschied_gesagt = True
            await self._sprechen(a, quelle, ABSCHIED, reaktion=False)

    async def _sprechen(self, a: Ausloeser, quelle: str, lage: str, ersatz: bool = False,
                        reaktion: bool = True) -> None:
        if not self._darf_sprechen():
            log.info("Besuch: %s – gerade nicht sprechen (Pause/Szene/KI spricht)", quelle)
            return
        self.letzte_ansprache = time.monotonic()
        ergebnis = await self.z.ki.ansprechen(a.ziel, a.vol, quelle, lage, reaktion=reaktion)
        self.letzte_ansprache = time.monotonic()
        e = self.z.ki.verlauf[0] if self.z.ki.verlauf else {}
        if ergebnis == "gesagt":
            self.letzter_satz = e.get("satz", "")
            if e.get("richtung") == "geht" and self.zustand == "gelockt":
                # das Modell hat sich (laut Vorgabe) schon verabschiedet
                self.abschied_gesagt = True
                self._wechsel("geht")
        elif ergebnis in ("niemand", "fehler") and ersatz:
            await self.z._ki_ersatz(a, f"{quelle} (Ersatz)")
        self._melden()

    # ------------------------------------------------------------------
    async def lauf(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                await self.takt(time.monotonic())
            except Exception:  # noqa: BLE001
                log.exception("Besuch-Takt")

    async def takt(self, jetzt: float) -> None:
        k = self.z.einst.ki
        st = self.zustand
        jemand = self.an["fern"] or self.an["nah"]
        if jemand:
            self.zuletzt_jemand = jetzt
        # Leute bleiben auf dem Weg stehen (Sensor bleibt an, kein neues Ereignis): einmal sanft nachfassen
        if st == "gelockt" and self.an["fern"] and not self.an["nah"] and self.fern and not self.z.ki.laeuft:
            if self.gelockt < k.locken_max and jetzt - self.letzte_ansprache >= k.locken_wiederholen_s:
                await self._nachfassen(self.fern, self.fern.name)
        if st in ("gelockt", "tuer", "geklingelt", "geht") and not jemand and not self.z.ki.laeuft:
            ruhe = st != "geklingelt" or jetzt - self.klingel_zeit >= k.tuer_ruhe_s
            if ruhe and jetzt - self.zuletzt_jemand >= k.besuch_ende_s:
                self.z.protokollieren("KI-Besuch vorbei", "info")
                self._wechsel("nachlauf")
        elif st == "nachlauf" and time.time() - self.seit >= k.nachlauf_s:
            self._wechsel("leer")
