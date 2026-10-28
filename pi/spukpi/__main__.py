"""spukpi – Dienst auf dem Raspberry Pi.

Meldet sich beim Server wie bisher die ESPs:
  - als „sound“: spielt Ambiente und Effekte über den PCM5102A (statt Sound-ESP),
  - als „licht“: reicht die Nachrichten an den Licht-ESP am USB durch (der hat dann kein WLAN mehr).
Beide Rollen lassen sich in /etc/spukpi/config.toml einzeln einschalten.

Neu gegenüber den ESPs: großer Stream-Vorrat, lückenloser Ersatz aus dem Mitschnitt, Notbetrieb ohne Server
(Ambiente + Zufallseffekte mit Licht), Spektrum/Beat für das Licht, gemessene Latenz, Pi-Gesundheit im Status.
"""

import argparse
import asyncio
import logging
import os
import random
import signal
import sys
import time
from pathlib import Path

from . import VERSION, system
from .ambiente import Ambiente
from .effekte import Effekte
from .konfig import Konfig, laden
from .licht import LichtBruecke
from .mischer import MAX_STIMMEN, Mischer
from .verbindung import Verbindung

log = logging.getLogger("spukpi")

NOTBETRIEB_AB_S = 60          # so lange ohne Server, dann löst der Pi selbst Effekte aus
NOTBETRIEB_PAUSE_S = (35, 110)


class Dienst:
    def __init__(self, k: Konfig) -> None:
        self.k = k
        daten = Path(k.daten)
        self.effekte = Effekte(daten / "fx")
        self.ambiente = Ambiente(k.basis_url, daten, k.puffer_s)
        self.licht = LichtBruecke(k.usb_port, aktiv=k.licht)
        self.mischer = Mischer(k.audio_geraet, self.ambiente, self.effekte, self.licht, analyse=k.analyse)
        self.sound = Verbindung("sound", lambda: f"{k.ws_url}/ws/geraet?rolle=sound&fw={VERSION}",
                                self.sound_nachricht, self.sound_verbunden)
        self.licht_ws = Verbindung("licht", lambda: f"{k.ws_url}/ws/geraet?rolle=licht&fw={self.esp_fw}",
                                   self.licht_nachricht, self.licht_verbunden)
        self.esp_fw = "?"
        self.esp_hallo: dict | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server_weg_seit: float | None = None
        self.musik_bestaetigt = True
        self.marke = daten / "sauber_beendet"
        self.start_grund = "Dienst gestartet" if self.marke.exists() else "Absturz oder Strom weg"
        self.marke.unlink(missing_ok=True)

    # ------------------------------------------------------------------ Sound-Rolle
    async def sound_verbunden(self) -> None:
        self.server_weg_seit = None
        self.mischer.server_verbunden = True
        await self.sound.senden({
            "t": "hallo", "rolle": "sound", "fw": VERSION, "plattform": "pi",
            "mac": system.mac_adresse(), "ip": system.ip_adresse(),
            "fx_frei_kb": self.effekte.frei_kb(), "fx_gesamt_kb": self.effekte.gesamt_kb(),
            "max_stimmen": MAX_STIMMEN,
            "neustart_grund": self.start_grund, "neustart_normal": self.start_grund == "Dienst gestartet",
        })
        self.start_grund = "Dienst gestartet"
        # Der Server schickt „musik play“ nur, wenn Ambiente an ist – kommt nichts, war es aus (z. B. im
        # Notbetrieb weitergelaufen) und wird gestoppt.
        self.musik_bestaetigt = False
        asyncio.get_running_loop().call_later(6, self._musik_pruefen)

    def _musik_pruefen(self) -> None:
        if not self.musik_bestaetigt and self.ambiente.soll and self.sound.verbunden:
            log.info("Server will kein Ambiente – Notbetrieb-Musik endet")
            self.mischer.musik_stop()

    async def sound_nachricht(self, m: dict) -> None:
        t = m.get("t")
        if t == "config":
            self.mischer.konfigurieren(m)
        elif t == "musik":
            if m.get("aktion") == "play":
                self.musik_bestaetigt = True
                self.mischer.musik_play(m.get("pfad") or "/stream/ambiente")
            else:
                self.mischer.musik_stop()
        elif t == "fx":
            fid = str(m.get("id", ""))
            licht = m.get("licht") if isinstance(m.get("licht"), dict) else None
            vol = int(m.get("vol", 100) or 0)
            if self.effekte.sync_laeuft or not self.mischer.fx_start(fid, vol, licht):
                await self.sound.senden({"t": "fx_fehlt", "id": fid})
            else:
                self.effekte.merken(fid, vol, licht)
        elif t == "fx_stop":
            self.mischer.fx_stop_alle()
        elif t == "fx_sync":
            if self.effekte.sync_laeuft:
                return
            basis = self.k.basis_url + str(m.get("basis", "/api/geraet/fx/"))
            liste = m.get("liste") if isinstance(m.get("liste"), list) else []
            asyncio.create_task(self._fx_sync(basis, liste))
        elif t == "neustart":
            log.info("Neustart auf Wunsch des Servers")
            self.beenden(neustart=True)

    async def _fx_sync(self, basis: str, liste: list) -> None:
        erg = await asyncio.get_running_loop().run_in_executor(None, self.effekte.abgleichen, basis, liste)
        await self.sound.senden({"t": "fx_sync_fertig", **erg, "fx_frei_kb": self.effekte.frei_kb()})
        log.info("Effekte abgeglichen: %s", erg)

    async def sound_status(self) -> None:
        while True:
            await asyncio.sleep(2)
            if not self.sound.verbunden:
                continue
            await self.sound.senden({
                "t": "status", **system.status(), **self.mischer.status(),
                "fx_frei_kb": self.effekte.frei_kb(), "fx_gesamt_kb": self.effekte.gesamt_kb(),
                "uart": self.licht.esp_da, "sync": self.effekte.sync_laeuft,
                "neustart_grund": "Dienst gestartet", "wlan_trennungen": self.sound.trennungen,
                "schleife_max_ms": self.mischer.status()["takt_max_ms"],
            })

    # ------------------------------------------------------------------ Licht-Rolle (Brücke)
    def esp_json(self, m: dict) -> None:
        """Vom Licht-ESP (Lese-Thread) -> an den Server."""
        if m.get("t") == "hallo":
            self.esp_hallo = m
            self.esp_fw = str(m.get("fw", "?"))
        if self.loop and self.licht.aktiv:
            asyncio.run_coroutine_threadsafe(self.licht_ws.senden(m), self.loop)

    def esp_log(self, text: str) -> None:
        log.info("Licht-ESP: %s", text)
        if self.loop and self.licht.aktiv and text.startswith("[licht]"):
            asyncio.run_coroutine_threadsafe(self.licht_ws.senden({"t": "log", "text": text[8:]}), self.loop)

    def licht_darf(self) -> bool:
        return self.k.licht and self.licht.esp_da

    async def licht_verbunden(self) -> None:
        # Hallo des ESP weiterreichen (er schickt es auf Anfrage neu)
        self.licht.json_senden({"t": "pi_hallo"})

    async def licht_nachricht(self, m: dict) -> None:
        self.licht.json_senden(m)

    async def esp_wecken(self) -> None:
        """Im aktiven Modus den ESP regelmäßig nach seinem Hallo fragen, bis er antwortet."""
        while True:
            if self.k.licht and not self.licht.esp_da:
                self.licht.json_senden({"t": "pi_hallo"})
            await asyncio.sleep(2)

    # ------------------------------------------------------------------ Notbetrieb
    async def notbetrieb(self) -> None:
        while True:
            await asyncio.sleep(random.uniform(*NOTBETRIEB_PAUSE_S))
            if not (self.k.sound and self.k.notbetrieb):
                continue
            if self.sound.verbunden:
                self.server_weg_seit = None
                continue
            if self.server_weg_seit is None:
                self.server_weg_seit = time.monotonic()
                self.mischer.server_verbunden = False
                continue
            if time.monotonic() - self.server_weg_seit < NOTBETRIEB_AB_S or not self.ambiente.soll:
                continue
            kandidaten = [fid for fid in self.effekte.ids() if fid in self.effekte.meta]
            if not kandidaten:
                continue
            fid = random.choice(kandidaten)
            meta = self.effekte.meta[fid]
            log.info("Notbetrieb: Effekt %s", fid)
            self.mischer.fx_start(fid, int(meta.get("vol", 100)), meta.get("licht"))

    # ------------------------------------------------------------------
    def beenden(self, neustart: bool = False) -> None:
        self.marke.touch()
        self.mischer.beenden()
        # systemd startet den Dienst neu (Restart=always); Exit-Code 0 = sauber
        os._exit(0)

    async def laufen(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.licht.bei_json = self.esp_json
        self.licht.bei_log = self.esp_log
        self.licht.start()
        self.mischer.start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.loop.add_signal_handler(sig, self.beenden)
        log.info("spukpi %s – Server %s, Sound %s, Licht %s", VERSION, self.k.basis_url,
                 "an" if self.k.sound else "aus", "an" if self.k.licht else "aus (nur Log)")
        aufgaben = [self.sound_status(), self.notbetrieb(), self.esp_wecken()]
        if self.k.server_host:
            if self.k.sound:
                aufgaben.append(self.sound.laufen())
            if self.k.licht:
                aufgaben.append(self.licht_ws.laufen(self.licht_darf))
        await asyncio.gather(*aufgaben)


def main() -> None:
    p = argparse.ArgumentParser(prog="spukpi")
    p.add_argument("--konfig", help="Pfad zur config.toml")
    p.add_argument("-v", "--ausfuehrlich", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.ausfuehrlich else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    k = laden(a.konfig)
    try:
        os.nice(-10)
    except OSError:
        pass
    asyncio.run(Dienst(k).laufen())


if __name__ == "__main__":
    main()
