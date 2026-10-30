"""PC-Test: Besuche simulieren – Sensoren schalten nach Drehbuch, die KI liest statt des Kamerabilds eine Beschreibung.

Neben den festen Drehbüchern gibt es freie: ein Prüfer (Mensch oder KI-Agent) schickt eigene Schritte
(POST /api/test/simulation mit „schritte“) – beliebige Gruppen, Zeiten, Chaos. Live geht es auch ohne Drehbuch:
/api/test/szene setzt die Beschreibung, /api/test/sensor schaltet die Sensoren.

Läuft genau über die echten Wege (Zentrale.test_sensor wie Home Assistant), dadurch reagieren Besuchs-Ablauf,
KI-Stimme und Regisseur so wie draußen. Die Gruppen werden zufällig gewählt, damit jeder Durchlauf anders klingt.
"""

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .zentrale import Zentrale

log = logging.getLogger(__name__)

GRUPPEN = [
    "drei Kinder (etwa 8 bis 10 Jahre) als Hexe mit spitzem Hut, Skelett und Kürbis, mit Süßigkeitentüten",
    "zwei kleine Kinder (etwa 4 und 6) als Fledermaus und Prinzessin an der Hand einer Erwachsenen",
    "vier Jugendliche, zwei mit Scream-Masken, einer als Zombie mit Kunstblut, einer mit Kettensäge aus Pappe",
    "ein Junge (etwa 9) im Vampirumhang mit roter Innenseite und einem Kürbiseimer",
    "zwei Mädchen (etwa 11) als Geister mit weißen Bettlaken und schwarzen Augenlöchern",
    "eine Familie: Vater als Werwolf, Mutter als Hexe, ein Kind (etwa 7) als Spinne mit acht Beinen",
    "drei Teenager ohne richtiges Kostüm, nur mit Teufelshörnern, sie lachen und filmen mit dem Handy",
]
ORT_WEG = "Abend, Laternenlicht, Kamera schaut auf den gepflasterten Weg zum Haus"
ORT_TUER = "Abend, Kamera an der Haustür, Blick von oben auf die Leute direkt vor der Tür"

# Drehbuch: (Sekunde, Aktion, Wert) – Aktionen: szene (Text, {g} = Gruppe), fern/nah (an/aus), klingel
DREHBUECHER: dict[str, tuple[str, list[tuple[float, str, object]]]] = {
    "kommt_klingelt": ("Kommt, klingelt, geht", [
        (0, "szene", f"{ORT_WEG}: {{g}} kommen den Weg herauf auf das Haus zu."),
        (0, "fern", True),
        (14, "fern", False),
        (16, "szene", f"{ORT_TUER}: {{g}} stehen direkt vor der Haustür und schauen sich um."),
        (16, "nah", True),
        (32, "klingel", True),
        (48, "nah", False),
        (50, "szene", f"{ORT_WEG}: {{g}} gehen mit Süßigkeiten den Weg zurück, Rücken zur Kamera."),
        (50, "fern", True),
        (62, "fern", False),
    ]),
    "zoegert": ("Zögert und geht wieder", [
        (0, "szene", f"{ORT_WEG}: {{g}} bleiben am Anfang des Wegs stehen und schauen unsicher zum Haus."),
        (0, "fern", True),
        (40, "szene", f"{ORT_WEG}: {{g}} drehen sich um und gehen weg, Rücken zur Kamera."),
        (40, "fern", False),
        (42, "fern", True),
        (55, "fern", False),
    ]),
    "an_tuer_ohne_klingeln": ("An der Tür, traut sich nicht", [
        (0, "szene", f"{ORT_WEG}: {{g}} kommen langsam den Weg herauf."),
        (0, "fern", True),
        (10, "fern", False),
        (12, "szene", f"{ORT_TUER}: {{g}} stehen vor der Tür, zögern und flüstern miteinander."),
        (12, "nah", True),
        (38, "nah", False),
        (40, "szene", f"{ORT_WEG}: {{g}} laufen schnell vom Haus weg, Rücken zur Kamera."),
        (40, "fern", True),
        (52, "fern", False),
    ]),
    "zwei_gruppen": ("Eine Gruppe geht, die nächste kommt", [
        (0, "szene", f"{ORT_TUER}: {{g}} stehen vor der Tür."),
        (0, "nah", True),
        (14, "nah", False),
        (16, "szene", f"{ORT_WEG}: {{g}} gehen vom Haus weg, Rücken zur Kamera."),
        (16, "fern", True),
        (30, "fern", False),
        (52, "szene", f"{ORT_WEG}: {{g2}} kommen den Weg herauf auf das Haus zu."),
        (52, "fern", True),
        (66, "fern", False),
        (68, "szene", f"{ORT_TUER}: {{g2}} stehen vor der Tür."),
        (68, "nah", True),
        (82, "klingel", True),
        (95, "nah", False),
    ]),
}


class Simulation:
    def __init__(self, z: "Zentrale") -> None:
        self.z = z
        self.task: asyncio.Task | None = None
        self.drehbuch = ""
        self.titel = ""
        self.start = 0.0
        self.dauer = 0.0
        self.schritt = ""

    @property
    def laeuft(self) -> bool:
        return self.task is not None and not self.task.done()

    def info(self) -> dict:
        return {"laeuft": self.laeuft, "drehbuch": self.drehbuch, "titel": self.titel, "schritt": self.schritt,
                "vergangen": round(time.monotonic() - self.start) if self.laeuft else 0, "dauer": self.dauer,
                "szene": self.z.ki.testszene or "",
                "drehbuecher": {k: v[0] for k, v in DREHBUECHER.items()}}

    async def starten(self, name: str) -> None:
        if name not in DREHBUECHER:
            raise KeyError(name)
        titel, schritte = DREHBUECHER[name]
        g, g2 = random.sample(GRUPPEN, 2)
        schritte = [(t, a, w.format(g=g, g2=g2) if a == "szene" else w) for t, a, w in schritte]
        await self._starten(name, titel, schritte, frisch=True, gruppe=g)

    async def starten_frei(self, titel: str, schritte: list[tuple[float, str, object]], frisch: bool = True) -> None:
        """Eigenes Drehbuch: Schritte (Sekunde, „szene“|„fern“|„nah“|„klingel“, Text bzw. an/aus)."""
        await self._starten("frei", titel, sorted(schritte, key=lambda s: s[0]), frisch=frisch, gruppe="")

    async def _starten(self, name: str, titel: str, schritte, frisch: bool, gruppe: str) -> None:
        await self.stoppen()
        self.drehbuch = name
        self.titel = titel
        self.task = asyncio.create_task(self._lauf(titel, schritte, frisch, gruppe), name="simulation")

    async def stoppen(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None

    async def _lauf(self, titel: str, schritte, frisch: bool, gruppe: str) -> None:
        if frisch:
            self.z.besuch.zuruecksetzen()
        self.start = time.monotonic()
        self.dauer = max((t for t, *_ in schritte), default=0)
        self.z.protokollieren(f"PC-Test: Simulation „{titel}“" + (f" – {gruppe}" if gruppe else ""), "info")
        self._melden()
        try:
            for t, aktion, wert in schritte:
                warten = self.start + t - time.monotonic()
                if warten > 0:
                    await asyncio.sleep(warten)
                if aktion == "szene":
                    self.z.ki.testszene = str(wert or "") or None
                    self.schritt = self.z.ki.testszene or "(Kamera leer)"
                else:
                    self.schritt = f"{ {'fern': 'Weg-Sensor', 'nah': 'Tür-Sensor', 'klingel': 'Klingel'}[aktion]} " \
                                   f"{'an' if wert else 'aus'}"
                    try:
                        await self.z.test_sensor(aktion, bool(wert))
                    except KeyError:
                        self.z.protokollieren(f"PC-Test: kein aktiver Auslöser mit der Rolle „{aktion}“", "fehler")
                self._melden()
            await asyncio.sleep(3)   # letzte Ansage noch zu Ende denken lassen
        finally:
            for rolle in ("fern", "nah"):
                if self.z.besuch.an.get(rolle):
                    try:
                        await self.z.test_sensor(rolle, False)
                    except KeyError:
                        pass
            self.z.ki.testszene = None
            self.schritt = ""
            self.z.protokollieren(f"PC-Test: Simulation „{titel}“ zu Ende", "info")
            self._melden()

    def _melden(self) -> None:
        self.z.an_ui({"t": "simulation", **self.info()})
