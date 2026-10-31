"""Besuchs-Ablauf der KI-Stimme: fasst Erkennungen von Weg (fern), Haustür (nah) und Klingel zu einem Besuch zusammen.

Zustände:
  leer       niemand da
  gelockt    auf dem Weg angesprochen („komm näher“), höchstens locken_max-mal
  tuer       an der Haustür angesprochen („klingel, wenn du dich traust“) – genau einmal pro Besuch
  geklingelt Klingel gedrückt: Schreck-Szene läuft über den normalen Auslöser, die Tür bleibt tuer_ruhe_s still.
             Auf dem Weg (die Weg-Kamera sieht die Tür nicht) schaut die KI ab neu_pruefen_s nach dem Klingeln hin:
             gehen die Leute -> Abschied, kommen schon die nächsten -> neuer Besuch
  geht       die Leute gehen: höchstens ein Abschied, nie wieder locken
  nachlauf   Besuch vorbei (besuch_ende_s ohne Person), noch nachlauf_s – Leute auf dem Rückweg

In „geht“ und „nachlauf“ kann schon die nächste Gruppe kommen: wird dort jemand erkannt (frühestens neu_pruefen_s
nach der letzten Ansage/Prüfung), schaut die KI hin. Kommt oder steht jemand, beginnt ein neuer Besuch; gehen die
Leute weg, bleibt sie stumm (kein Flüstern, kein Ersatz-Sound).

Grundregeln gegen Durcheinander:
  - Die Tür hat Vorrang: steht jemand an der Tür (Klingel-Kamera), spricht die Stimme niemanden auf dem Weg an.
    Zwei Gruppen an der Tür sind ein Besuch (eine Aufforderung, ein Klingeln).
  - Es spricht immer nur eine KI-Stimme (KiStimme.spricht: bis der Satz zu Ende ist), während einer Szene spricht sie gar nicht.
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
NACH_KLINGEL = ("Die Leute an der Haustür haben eben geklingelt und wurden erschreckt. Jetzt ist jemand auf dem Weg zu "
                "sehen. Schau genau hin: Rücken zur Kamera bzw. vom Haus weg = sie gehen (richtung geht) – dann "
                "verabschiede dich gruselig-freundlich und locke sie NICHT zurück. Nur wenn klar NEUE Leute aufs Haus "
                "zukommen (Gesicht zur Kamera, richtung kommt), locke sie zur Haustür. Im Zweifel: verabschieden.")
ABSCHIED = ("Die Leute gehen gerade wieder weg. Verabschiede dich gruselig-freundlich (zum Beispiel: kommt bald "
            "wieder). Locke sie auf KEINEN Fall zurück und fordere sie zu nichts auf.")

ABSCHIED_PRUEFEN_S = 10       # genug gelockt, sie drehen um: so schnell darf der Abschied kommen
NACH_KLINGEL_S = 8            # nach dem Klingeln: ab dann schauen, ob die Leute gehen (sie sind nach 10–25 s weg)

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
        self.letzte_pruefung = 0.0         # monotonic: zuletzt in geht/nachlauf nach neuen Leuten geschaut
        self.letzter_satz = ""
        self.klingel_zeit = 0.0            # monotonic
        self.an: dict[str, bool] = {"fern": False, "nah": False}
        self.zuletzt_jemand = 0.0          # monotonic: letzter Moment mit Person
        self.pause_bis = 0.0               # time.time()
        # Satz, der gerade nicht ging (Szene/KI beschäftigt): (Auslöser, Lage, Ersatz?, monotonic) – takt() holt nach
        self.ausstehend: tuple[Ausloeser, str, bool, float] | None = None
        self.fern: Ausloeser | None = None  # für Nachfassen per Takt (Kamera, Lautstärke)
        self.nah: Ausloeser | None = None
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
        self.z.regie.neuer_besuch()
        self.gelockt = 0
        self.tuer_gesagt = self.abschied_gesagt = False
        self.letzter_satz = ""
        self.z.ki.letzte.clear()

    def _melden(self) -> None:
        self.z.an_ui({"t": "besuch", **self.info()})

    def _darf_sprechen(self) -> bool:
        if self.pausiert or self.z.ki.spricht():
            return False
        sp = self.z.spieler
        # Szenen des Regisseurs sind kurz: die Stimme wartet mit dem Abspielen, bis sie vorbei sind (ki.py)
        return not (sp and sp.laufend and sp.laufend.get("quelle") != "Regie")

    # ------------------------------------------------------------------
    async def sensor(self, a: Ausloeser, an: bool, quelle: str) -> None:
        """Personensensor mit Rolle fern/nah hat geschaltet."""
        jetzt = time.monotonic()
        self.an[a.rolle] = an
        if an:
            self.zuletzt_jemand = jetzt
        if a.rolle == "fern":
            self.fern = a
        else:
            self.nah = a
        if not an:
            self._melden()
            return
        st = self.zustand
        if a.rolle == "fern" and self.an["nah"]:
            self._melden()                                       # Tür hat Vorrang: niemanden auf dem Weg ansprechen
            return
        if st in ("leer", "nachlauf") or (a.rolle == "nah" and st == "gelockt"):
            await self._zufall_beenden()                         # Besucher gehen vor: eine Zufalls-Szene endet
        if a.rolle == "fern":
            if st in ("leer",):
                self._wechsel("gelockt")
                self.gelockt = 1
                if await self._sprechen(a, quelle, LOCKEN, ersatz=True) == "beschaeftigt":
                    self.gelockt = 0                             # zählt erst, wenn es gesagt wurde
                    self.ausstehend = (a, LOCKEN, True, time.monotonic())
            elif st == "gelockt":
                await self._nachfassen(a, quelle)
            elif st == "tuer" and not self.an["nah"]:
                await self._abschied(a, quelle)                  # waren an der Tür, sind jetzt wieder auf dem Weg
            elif st == "geklingelt":
                await self._nach_klingel(a, quelle)
            elif st in ("geht", "nachlauf"):
                await self._neue_leute(a, quelle)
        else:  # nah
            if st in ("geht", "nachlauf"):
                await self._neue_leute(a, quelle)
                return
            if st in ("tuer", "geklingelt") or self.tuer_gesagt:
                return
            if st in ("leer", "nachlauf"):
                self._neu()
            self._wechsel("tuer")
            self.tuer_gesagt = True                              # gleichzeitige Tür-Meldungen nicht doppelt
            if await self._sprechen(a, quelle, TUER, ersatz=True) == "beschaeftigt":
                self.tuer_gesagt = False
                self.ausstehend = (a, TUER, True, time.monotonic())

    async def _zufall_beenden(self) -> None:
        sp = self.z.spieler
        if sp and sp.laufend and sp.laufend.get("quelle") == "Zufall":
            log.info("Besuch beginnt: Zufalls-Szene %s endet", sp.laufend.get("id"))
            await sp.stoppen()

    def klingel(self) -> None:
        """Klingel gedrückt: Szene läuft über den normalen Auslöser – KI hört auf und bleibt still."""
        self.z.ki.abbrechen = True
        self.z.ki.spricht_bis = 0.0                # die Ansage wird gleich gestoppt (sag_stop)
        # eine gerade laufende Ansage endet – der Klingel-Schreck soll nicht über der Stimme liegen
        try:
            asyncio.get_running_loop()
        except RuntimeError:                       # ohne laufende Event-Schleife (Tests): nichts zu stoppen
            pass
        else:
            self.z._hintergrund(self.z.geraete.senden("sound", {"t": "sag_stop"}))
        self.klingel_zeit = time.monotonic()
        self.zuletzt_jemand = self.klingel_zeit
        self.letzte_pruefung = 0.0                 # nach NACH_KLINGEL_S sofort schauen, ob sie gehen
        self._wechsel("geklingelt")

    async def _nachfassen(self, a: Ausloeser, quelle: str) -> None:
        k = self.z.einst.ki
        if self.an["nah"]:
            return
        if self.gelockt >= k.locken_max:
            await self._weggehen_pruefen(a, quelle)
            return
        if time.monotonic() - self.letzte_ansprache < k.locken_wiederholen_s:
            return
        self.gelockt += 1
        lage = ZOEGERN.format(s=int(time.monotonic() - self.letzte_ansprache), satz=self.letzter_satz or "…")
        if await self._sprechen(a, quelle, lage, reaktion=False) == "beschaeftigt":
            self.gelockt -= 1                                    # nicht verbraucht, takt() versucht es wieder

    async def _neue_leute(self, a: Ausloeser, quelle: str) -> None:
        """geht/nachlauf: kommt da schon die nächste Gruppe? Nur ansprechen, wenn niemand weggeht."""
        k = self.z.einst.ki
        jetzt = time.monotonic()
        if jetzt - max(self.letzte_ansprache, self.letzte_pruefung) < k.neu_pruefen_s or not self._darf_sprechen():
            return
        self.letzte_pruefung = jetzt
        lage = LOCKEN if a.rolle == "fern" else TUER
        alt = self.zustand
        ergebnis = await self._sprechen(a, quelle, lage, reaktion=False, still_bei=("geht",))
        self.letzte_pruefung = time.monotonic()
        if ergebnis != "gesagt" or self.zustand != alt:
            return
        log.info("Besuch: neue Leute (%s)", a.rolle)
        satz = self.letzter_satz
        self._neu()
        self.letzter_satz = satz
        if a.rolle == "fern":
            self.gelockt = 1
            self._wechsel("gelockt")
        else:
            self.tuer_gesagt = True
            self._wechsel("tuer")

    async def _nach_klingel(self, a: Ausloeser, quelle: str) -> None:
        """Nach dem Klingeln jemand auf dem Weg: die Leute gehen (Abschied) oder die nächsten kommen (neuer Besuch)."""
        k = self.z.einst.ki
        jetzt = time.monotonic()
        if jetzt - max(self.klingel_zeit, self.letzte_ansprache) < NACH_KLINGEL_S or not self._darf_sprechen():
            return
        if jetzt - self.letzte_pruefung < k.neu_pruefen_s:
            return
        self.letzte_pruefung = jetzt
        # eigener Hinweis: mit LOCKEN („noch auf dem Weg zum Haus“) hielt das Modell Gehende für Kommende und lockte
        # sie (Prüfbericht 31.10. nachmittags, 4 von 4)
        abschied = k.abschied and not self.abschied_gesagt
        ergebnis = await self._sprechen(a, quelle, NACH_KLINGEL, reaktion=False,
                                        still_bei=() if abschied else ("geht", "steht", "unklar"))
        self.letzte_pruefung = time.monotonic()
        if ergebnis != "gesagt" or self.zustand != "geklingelt":
            return
        e = self.z.ki.verlauf[0] if self.z.ki.verlauf else {}
        if e.get("richtung") != "kommt":                 # im Zweifel gehen sie (der Satz war ein Abschied)
            self.abschied_gesagt = True
            self._wechsel("geht")
            return
        log.info("Besuch: nach dem Klingeln kommen neue Leute")
        satz = self.letzter_satz
        self._neu()
        self.letzter_satz = satz
        self.gelockt = 1
        self._wechsel("gelockt")

    async def _weggehen_pruefen(self, a: Ausloeser, quelle: str) -> None:
        """Genug gelockt: nur noch verabschieden – und nur, wenn das Modell die Leute wirklich gehen sieht."""
        k = self.z.einst.ki
        jetzt = time.monotonic()
        if not k.abschied or self.abschied_gesagt or not self._darf_sprechen():
            return
        if jetzt - max(self.letzte_ansprache, self.letzte_pruefung) < ABSCHIED_PRUEFEN_S:
            return
        self.letzte_pruefung = jetzt
        ergebnis = await self._sprechen(a, quelle, ABSCHIED, reaktion=False, still_bei=("kommt", "steht", "unklar"))
        self.letzte_pruefung = time.monotonic()
        if ergebnis == "gesagt" and self.zustand == "gelockt":
            self.abschied_gesagt = True
            self._wechsel("geht")

    async def _nachholen(self, jetzt: float) -> None:
        """Einen Satz, der beim Eintreffen nicht ging, nachholen – solange die Leute noch da sind (höchstens 20 s)."""
        if not self.ausstehend:
            return
        a, lage, ersatz, seit = self.ausstehend
        passt = (self.an.get(a.rolle) and jetzt - seit < 20
                 and ((a.rolle == "fern" and self.zustand == "gelockt" and self.gelockt == 0 and not self.an["nah"])
                      or (a.rolle == "nah" and self.zustand == "tuer" and not self.tuer_gesagt)))
        if not passt:
            self.ausstehend = None
            return
        if not self._darf_sprechen():
            return
        self.ausstehend = None
        if a.rolle == "fern":
            self.gelockt = 1
        else:
            self.tuer_gesagt = True
        if await self._sprechen(a, f"{a.name} (nachgeholt)", lage, ersatz=ersatz) == "beschaeftigt":
            if a.rolle == "fern":
                self.gelockt = 0
            else:
                self.tuer_gesagt = False
            self.ausstehend = (a, lage, ersatz, seit)

    def zuruecksetzen(self) -> None:
        """Frischer Besuch (PC-Test: jede Simulation beginnt bei „niemand da“), eine Pause bleibt."""
        pause = self.pause_bis
        self.__init__(self.z)
        self.pause_bis = pause
        self._melden()

    async def _abschied(self, a: Ausloeser, quelle: str) -> None:
        self._wechsel("geht")
        if self.z.einst.ki.abschied and not self.abschied_gesagt:
            self.abschied_gesagt = True
            await self._sprechen(a, quelle, ABSCHIED, reaktion=False)

    async def _sprechen(self, a: Ausloeser, quelle: str, lage: str, ersatz: bool = False,
                        reaktion: bool = True, still_bei: tuple[str, ...] = ()) -> str:
        if not self._darf_sprechen():
            log.info("Besuch: %s – gerade nicht sprechen (Pause/Szene/KI spricht)", quelle)
            return "beschaeftigt"
        if still_bei:
            ergebnis = await self.z.ki.ansprechen(a.ziel, a.vol, quelle, lage, reaktion=reaktion, still_bei=still_bei)
            if ergebnis != "gesagt":
                self._melden()
                return ergebnis
            self.letzte_ansprache = time.monotonic()
        else:
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
        return ergebnis

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
        await self._nachholen(jetzt)
        # Leute bleiben auf dem Weg stehen (Sensor bleibt an, kein neues Ereignis): einmal sanft nachfassen
        if st == "gelockt" and self.an["fern"] and not self.an["nah"] and self.fern and not self.z.ki.spricht():
            if self.gelockt < k.locken_max and jetzt - self.letzte_ansprache >= k.locken_wiederholen_s:
                await self._nachfassen(self.fern, self.fern.name)
        # nach dem Klingeln bleibt der Weg-Sensor an: gehen die Leute oder kommen schon die nächsten?
        if st == "geklingelt" and self.an["fern"] and not self.an["nah"] and self.fern and not self.z.ki.spricht():
            await self._nach_klingel(self.fern, self.fern.name)
        # geht/nachlauf und der Sensor bleibt an: ab und zu schauen, ob schon die nächste Gruppe kommt
        if st in ("geht", "nachlauf") and jemand and not self.z.ki.spricht():
            a = self.nah if self.an["nah"] and self.nah else self.fern if self.an["fern"] else None
            if a:
                await self._neue_leute(a, a.name)
        if st in ("gelockt", "tuer", "geklingelt", "geht") and not jemand and not self.z.ki.spricht():
            ruhe = st != "geklingelt" or jetzt - self.klingel_zeit >= k.tuer_ruhe_s
            if ruhe and jetzt - self.zuletzt_jemand >= k.besuch_ende_s:
                self.z.protokollieren("KI-Besuch vorbei", "info")
                self._wechsel("nachlauf")
        elif st == "nachlauf" and time.time() - self.seit >= k.nachlauf_s:
            self._wechsel("leer")
