"""Zeitplan (Show an/aus nach Uhrzeit oder Sonnenuntergang), Ruhezeit, Gruselstufe nach Uhrzeit.

Alles reagiert nur auf Wechsel: Wer zwischendurch von Hand schaltet, wird erst beim
nächsten Wechsel wieder „überstimmt“.
"""

import asyncio
import logging
import os
from datetime import date, datetime, time as dtime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun

if TYPE_CHECKING:
    from .zentrale import Zentrale

log = logging.getLogger(__name__)
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))


def minuten(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_zeitraum(jetzt_min: int, ab: str, bis: str) -> bool:
    """Ob jetzt im Zeitraum [ab, bis) liegt – auch über Mitternacht."""
    a, b = minuten(ab), minuten(bis)
    if a == b:
        return False
    if a < b:
        return a <= jetzt_min < b
    return jetzt_min >= a or jetzt_min < b


def stufe_nach_uhrzeit(jetzt_min: int, stufe2_ab: str, stufe3_ab: str) -> int:
    # ab Mittag gezählt, damit "nach Mitternacht" als spät gilt
    def abend(m: int) -> int:
        return (m - 12 * 60) % (24 * 60)

    m = abend(jetzt_min)
    if m >= abend(minuten(stufe3_ab)):
        return 3
    if m >= abend(minuten(stufe2_ab)):
        return 2
    return 1


class Zeitplan:
    def __init__(self, z: "Zentrale") -> None:
        self.z = z
        self.war_im_fenster: bool | None = None
        self.letzte_auto_stufe: int | None = None

    def sonnenuntergang(self, tag: date) -> datetime | None:
        s = self.z.einst.standort
        try:
            ort = LocationInfo(latitude=s.breite, longitude=s.laenge, timezone=str(TZ))
            return sun(ort.observer, date=tag, tzinfo=TZ)["sunset"]
        except ValueError:  # Polartag/-nacht
            return None

    def fenster(self, tag: date) -> tuple[datetime, datetime]:
        zp = self.z.einst.zeitplan
        start = None
        if zp.start_modus == "sonnenuntergang":
            su = self.sonnenuntergang(tag)
            if su:
                start = su + timedelta(minutes=zp.versatz_min)
        if start is None:
            h, m = map(int, zp.start.split(":"))
            start = datetime.combine(tag, dtime(h, m), TZ)
        h, m = map(int, zp.ende.split(":"))
        ende = datetime.combine(tag, dtime(h, m), TZ)
        if ende <= start:
            ende += timedelta(days=1)
        return start, ende

    def im_fenster(self, jetzt: datetime) -> tuple[bool, datetime, datetime]:
        for tag in (jetzt.date() - timedelta(days=1), jetzt.date()):
            a, e = self.fenster(tag)
            if a <= jetzt < e:
                return True, a, e
        a, e = self.fenster(jetzt.date())
        if jetzt >= e:
            a, e = self.fenster(jetzt.date() + timedelta(days=1))
        return False, a, e

    async def lauf(self) -> None:
        while True:
            try:
                await self.tick(datetime.now(TZ))
            except Exception:
                log.exception("Zeitplan")
            await asyncio.sleep(15)

    async def tick(self, jetzt: datetime) -> None:
        z = self.z
        e = z.einst
        jetzt_min = jetzt.hour * 60 + jetzt.minute

        # --- Show an/aus ---
        if e.zeitplan.aktiv:
            drin, a, ende = self.im_fenster(jetzt)
            z.show_fenster = {"im_fenster": drin, "start": a.isoformat(), "ende": ende.isoformat()}
            if self.war_im_fenster is None:
                # Start des Servers bzw. Zeitplan eben eingeschaltet: nur einschalten, nie ausschalten
                if drin and not z.zustand.licht.an:
                    await z.show(True, e.zeitplan.ambiente, e.zeitplan.ereignisse, "Zeitplan")
            elif drin != self.war_im_fenster:
                await z.show(drin, e.zeitplan.ambiente, e.zeitplan.ereignisse, "Zeitplan")
            self.war_im_fenster = drin
        else:
            self.war_im_fenster = None
            z.show_fenster = {}

        # --- Ruhezeit ---
        ruhe = e.ruhezeit.aktiv and in_zeitraum(jetzt_min, e.ruhezeit.ab, e.ruhezeit.bis)
        if ruhe != z.ruhezeit_aktiv:
            z.ruhezeit_aktiv = ruhe
            z.protokollieren("Ruhezeit beginnt" if ruhe else "Ruhezeit vorbei", "info")
            await z.ton_geaendert()

        # --- Gruselstufe nach Uhrzeit ---
        if e.grusel.auto:
            stufe = stufe_nach_uhrzeit(jetzt_min, e.grusel.stufe2_ab, e.grusel.stufe3_ab)
            if stufe != self.letzte_auto_stufe:
                self.letzte_auto_stufe = stufe
                z.gruselstufe(stufe)
        else:
            self.letzte_auto_stufe = None
