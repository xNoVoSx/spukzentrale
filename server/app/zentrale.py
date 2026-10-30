"""Die Zentrale: hält den Zustand und verbindet Oberfläche, Geräte, Szenen und Automatik.

Alle Bedienwege (Weboberfläche, REST, MQTT, Home Assistant, Zeitplan, Ereignis-Generator)
rufen die Methoden hier auf. Änderungen gehen an die Geräte und per WebSocket an alle
offenen Oberflächen.
"""

import asyncio
import collections
import concurrent.futures
import json
import logging
import random
import time
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket

from . import VERSION, pfade
from .ambiente import Ambiente
from .geraete import Geraet, Geraete
from .besuch import LOCKEN, TUER, Besuch
from .ki import KiStimme
from .regie import Regie
from .simulation import Simulation
from .licht import EspLicht, LichtAusgang
from .medien import FX_KAPAZITAET_STANDARD_KB, FX_RESERVE_KB, MedienVerwaltung
from . import fxanalyse, musikanalyse
from . import medien as medien_mod
from .modelle import (
    GEHEIM_PLATZHALTER, GEHEIME_FELDER, KATEGORIEN, AKTIONEN, EFFEKTE, Ausloeser, Effekt, Einstellungen, Ereignisse,
    LichtAktion, LichtZustand, Musikstueck, Regel, Stimmung, Stimmungen, Szene, Szenen, TonEinst, Zone, Zustand,
)
from .speicher import lade, speichere
from . import vorgaben

log = logging.getLogger(__name__)

# Ersatz, wenn kein Ton läuft: Aktionen, die der Effekt-Lautstärke folgen, hätten sonst nichts zu tun
OHNE_TON = {"env_blitz": "blitz", "env_puls": "puls"}


class Zentrale:
    def __init__(self) -> None:
        pfade.anlegen()
        self.einst: Einstellungen = lade(pfade.CONFIG / "einstellungen.json", Einstellungen)
        self.zustand: Zustand = lade(pfade.CONFIG / "zustand.json", Zustand)
        self.stimmungen: Stimmungen = lade(pfade.CONFIG / "stimmungen.json", Stimmungen)
        self.szenen: Szenen = lade(pfade.CONFIG / "szenen.json", Szenen)
        self.ereignisse: Ereignisse = lade(pfade.CONFIG / "ereignisse.json", Ereignisse)
        self._vorgaben_ergaenzen()
        self._zonen_anpassen()

        self.medien = MedienVerwaltung()
        self.geraete = Geraete()
        self.licht: LichtAusgang = EspLicht(self.geraete)
        self.ambiente = Ambiente(self.medien, bei_wechsel=lambda _: self._zustand_geaendert())

        self.ui: set[WebSocket] = set()
        self.protokoll: collections.deque[dict] = collections.deque(maxlen=60)
        self.overlay: Stimmung | None = None     # kurzzeitige Stimmung aus einer Szene
        self.ruhezeit_aktiv = False
        self.show_fenster: dict[str, Any] = {}    # vom Zeitplan: nächster Start/Ende
        self.ha_status = "aus"
        self.mqtt_status = "aus"
        self._letzter_fx_sync = 0.0
        self._letzte_ausloesung: dict[str, float] = {}
        self._zuletzt_gespielt: dict[str, str] = {}
        # Musikanalyse: ein Arbeiter (rechenintensiv, nacheinander), wartende IDs
        self._analyse_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="analyse")
        self.analyse_laeuft: set[str] = set()
        self.beobachter: list[Callable[[str], None]] = []  # z. B. MQTT: bei Änderungen benachrichtigen
        self.ki = KiStimme(self)
        self.besuch = Besuch(self)
        self.regie = Regie(self)
        self.simulation = Simulation(self)
        self._ki_tasks: set[asyncio.Task] = set()
        # Hintergrundmusik kurz gedämpft (Szene „Totenstille“, Regisseur): ("stille"|"leise", Ende monotonic)
        self.musik_daempfung: tuple[str, float] | None = None
        self._daempfung_task: asyncio.Task | None = None

        # werden in main.py gesetzt (brauchen die Zentrale selbst)
        self.spieler = None    # ablauf.SzenenSpieler
        self.generator = None  # ereignisse.Generator

    # ------------------------------------------------------------------
    # Laden / Speichern
    # ------------------------------------------------------------------
    def _vorgaben_ergaenzen(self) -> None:
        """Eingebaute Stimmungen/Szenen/Regeln anlegen, wenn sie (noch) fehlen."""
        if self.stimmungen.liste and self.stimmungen.vorgaben_version == 0:
            self.stimmungen.vorgaben_version = 1
        if self.stimmungen.vorgaben_version < vorgaben.STIMMUNGEN_VERSION:
            vorhanden = {s.id for s in self.stimmungen.liste}
            for version, st in vorgaben.STIMMUNGEN:
                if version > self.stimmungen.vorgaben_version and st.id not in vorhanden:
                    self.stimmungen.liste.append(st.model_copy(deep=True))
            self.stimmungen.vorgaben_version = vorgaben.STIMMUNGEN_VERSION
            speichere(pfade.CONFIG / "stimmungen.json", self.stimmungen)
        if self.szenen.liste and self.szenen.vorgaben_version == 0:
            self.szenen.vorgaben_version = 1  # Stand von 1.0 (kannte die Versionsnummer noch nicht)
        if self.szenen.vorgaben_version < vorgaben.SZENEN_VERSION:
            # nur Szenen ergänzen, die in neueren Versionen dazugekommen sind
            vorhanden = {s.id for s in self.szenen.liste}
            for version, szene in vorgaben.SZENEN:
                if version > self.szenen.vorgaben_version and szene.id not in vorhanden:
                    self.szenen.liste.append(szene.model_copy(deep=True))
            self.szenen.vorgaben_version = vorgaben.SZENEN_VERSION
            speichere(pfade.CONFIG / "szenen.json", self.szenen)
        if self.ereignisse.regeln and self.ereignisse.vorgaben_version == 0:
            self.ereignisse.vorgaben_version = 1
        if self.ereignisse.vorgaben_version < vorgaben.REGELN_VERSION:
            vorhanden = {r.id for r in self.ereignisse.regeln}
            for version, r in vorgaben.REGELN:
                if version > self.ereignisse.vorgaben_version and r.id not in vorhanden:
                    self.ereignisse.regeln.append(r.model_copy(deep=True))
            self.ereignisse.vorgaben_version = vorgaben.REGELN_VERSION
            speichere(pfade.CONFIG / "ereignisse.json", self.ereignisse)

    def _zonen_anpassen(self) -> None:
        n = self.einst.licht.zonen
        z = self.zustand.licht.zonen
        while len(z) < n:
            z.append(z[-1].model_copy() if z else Zone())
        del z[n:]

    def speichern(self, *was: str) -> None:
        dateien = {
            "einst": ("einstellungen.json", self.einst),
            "zustand": ("zustand.json", self.zustand),
            "stimmungen": ("stimmungen.json", self.stimmungen),
            "szenen": ("szenen.json", self.szenen),
            "ereignisse": ("ereignisse.json", self.ereignisse),
        }
        for w in was:
            name, modell = dateien[w]
            speichere(pfade.CONFIG / name, modell)

    # ------------------------------------------------------------------
    # Abgeleitete Werte
    # ------------------------------------------------------------------
    def licht_effektiv(self) -> LichtZustand:
        z = self.zustand.licht.model_copy(deep=True)
        if self.overlay:
            n = len(z.zonen)
            z.zonen = [self.overlay.zonen[i % len(self.overlay.zonen)].model_copy() for i in range(n)]
            z.musik_reaktiv = self.overlay.musik_reaktiv
        return z

    def ton_effektiv(self) -> TonEinst:
        t = self.einst.ton.model_copy()
        if self.musik_daempfung:
            t.musik_vol = 0 if self.musik_daempfung[0] == "stille" else round(t.musik_vol * 0.3)
        if self.ruhezeit_aktiv:
            if self.einst.ruhezeit.modus == "stumm":
                t.master = 0
            else:
                t.master = min(t.master, self.einst.ruhezeit.lautst)
        return t

    @property
    def stumm(self) -> bool:
        return self.ruhezeit_aktiv and self.einst.ruhezeit.modus == "stumm"

    def fx_kapazitaet_kb(self) -> int:
        g = self.geraete.get("sound")
        gesamt = ((g.hallo.get("fx_gesamt_kb") if g else None) or self.medien.daten.sound_speicher_kb
                  or FX_KAPAZITAET_STANDARD_KB)
        if not gesamt:
            return 0                                   # unbekannt: Hochladen nicht blockieren
        return max(0, int(gesamt) - FX_RESERVE_KB)

    # ------------------------------------------------------------------
    # Oberfläche
    # ------------------------------------------------------------------
    def einstellungen_oeffentlich(self) -> dict:
        d = self.einst.model_dump()
        for teil, feld in GEHEIME_FELDER:
            if d[teil][feld]:
                d[teil][feld] = GEHEIM_PLATZHALTER
        return d

    def zustand_info(self) -> dict:
        spieler = self.spieler
        return {
            "zustand": self.zustand.model_dump(),
            "licht_effektiv": self.licht_effektiv().model_dump(),
            "overlay": self.overlay.name if self.overlay else None,
            "szene": spieler.laufend if spieler else None,
            "ambiente_jetzt": self.ambiente.jetzt,
            "ambiente_pos": self._ambiente_pos(),
            "ruhezeit": self.ruhezeit_aktiv,
            "show_fenster": self.show_fenster,
            "ton": self.ton_effektiv().model_dump(),
            "ha": self.ha_status,
            "mqtt": self.mqtt_status,
        }

    def medien_info(self) -> dict:
        return {
            "musik": [m.model_dump() for m in self.medien.daten.musik],
            "playlist": self.medien.daten.playlist.model_dump(),
            "fx": [f.model_dump() for f in self.medien.daten.fx],
            "fx_belegt_kb": self.medien.fx_belegt_kb(),
            "fx_kapazitaet_kb": self.fx_kapazitaet_kb(),
            "sound_plattform": self.medien.daten.sound_plattform,
            "analyse_laeuft": sorted(self.analyse_laeuft),
        }

    def alles(self) -> dict:
        return {
            "t": "alles",
            "version": VERSION,
            "effekte": EFFEKTE,
            "aktionen": AKTIONEN,
            "kategorien": KATEGORIEN,
            **self.zustand_info(),
            "geraete": self.geraete.info(),
            "einstellungen": self.einstellungen_oeffentlich(),
            "stimmungen": [s.model_dump() for s in self.stimmungen.liste],
            "szenen": [s.model_dump() for s in self.szenen.liste],
            "ereignisse": self.ereignisse.model_dump(),
            "medien": self.medien_info(),
            "protokoll": list(self.protokoll),
        }

    async def ui_anmelden(self, ws: WebSocket) -> None:
        self.ui.add(ws)
        await ws.send_text(json.dumps(self.alles()))
        if len(self.ui) == 1:
            await self.licht.vorschau(True)

    async def ui_abmelden(self, ws: WebSocket) -> None:
        self.ui.discard(ws)
        if not self.ui:
            await self.licht.vorschau(False)

    def an_ui(self, nachricht: dict) -> None:
        if not self.ui:
            return
        text = json.dumps(nachricht, separators=(",", ":"))
        for ws in list(self.ui):
            asyncio.create_task(self._ui_senden(ws, text))

    async def _ui_senden(self, ws: WebSocket, text: str) -> None:
        try:
            await ws.send_text(text)
        except Exception:
            self.ui.discard(ws)

    def _zustand_geaendert(self) -> None:
        self.an_ui({"t": "zustand", **self.zustand_info()})
        self._melden("zustand")

    def daten_geaendert(self) -> None:
        """Nach seltenen Änderungen (Einstellungen, Szenen, Medien ...): alles neu schicken."""
        self.an_ui(self.alles())
        self._melden("daten")

    def _melden(self, art: str) -> None:
        for b in self.beobachter:
            try:
                b(art)
            except Exception:
                log.exception("Beobachter")

    def protokollieren(self, text: str, art: str = "info") -> None:
        eintrag = {"zeit": time.time(), "text": text, "art": art}
        self.protokoll.appendleft(eintrag)
        log.info("%s", text)
        self.an_ui({"t": "log", **eintrag})

    # ------------------------------------------------------------------
    # Geräte
    # ------------------------------------------------------------------
    async def geraet_verbunden(self, g: Geraet) -> None:
        alt = self.geraete.anmelden(g)
        if alt:
            try:
                await alt.ws.close()
            except Exception:
                pass
        # „verbunden“ kommt ins Protokoll, sobald das Hallo da ist – erst dann ist klar, ob Pi oder ESP
        self.an_ui({"t": "geraete", "geraete": self.geraete.info()})
        self._melden("geraete")

    @staticmethod
    def geraet_name(g: Geraet | None, rolle: str) -> str:
        """Anzeigename: Seit 1.3 kann hinter „sound“ der Raspberry Pi stehen und der Licht-ESP am USB des Pi hängen."""
        h = g.hallo if g else {}
        if h.get("plattform") == "browser":
            return f"PC-Test im Browser ({'Sound' if rolle == 'sound' else 'Licht'})"
        if rolle == "sound":
            return "Sound (Raspberry Pi)" if h.get("plattform") == "pi" else "Sound-ESP"
        return "Licht-ESP (USB am Pi)" if h.get("verbindung") == "usb" else "Licht-ESP"

    async def geraet_getrennt(self, g: Geraet) -> None:
        if self.geraete.abmelden(g):
            self.protokollieren(f"{self.geraet_name(g, g.rolle)} getrennt", "geraet")
            self.an_ui({"t": "geraete", "geraete": self.geraete.info()})
            self._melden("geraete")

    async def geraet_nachricht(self, g: Geraet, msg: dict) -> None:
        t = msg.get("t")
        if t == "hallo":
            erstes = not g.hallo
            g.hallo = msg
            name = self.geraet_name(g, g.rolle)
            if erstes:
                self.protokollieren(f"{name} verbunden ({g.ip}, FW {msg.get('fw', g.fw)})", "geraet")
            if msg.get("neustart_normal") is False:
                self.protokollieren(f"{name} ist neu gestartet: {msg.get('neustart_grund', '?')}", "fehler")
            if g.rolle == "licht":
                await self.licht.konfigurieren(self.einst.licht)
                await self.licht.zustand(self.licht_effektiv(), temp=self.overlay is not None)
                await self.licht.vorschau(bool(self.ui))
            else:
                d = self.medien.daten
                plattform, speicher = str(msg.get("plattform", "esp")), int(msg.get("fx_gesamt_kb") or 0)
                # der Browser-Test meldet keinen echten Speicher – den Pi nicht damit überschreiben
                if plattform != "browser" and speicher and (d.sound_plattform, d.sound_speicher_kb) != (plattform, speicher):
                    d.sound_plattform, d.sound_speicher_kb = plattform, speicher
                    self.medien.speichern()
                await self.sound_konfigurieren()
                await self.fx_sync()
                if self.zustand.ambiente_an:
                    await g.senden({"t": "musik", "aktion": "play", "pfad": "/stream/ambiente"})
            self.an_ui({"t": "geraete", "geraete": self.geraete.info()})
            if g.rolle == "sound":
                self.an_ui({"t": "medien", **self.medien_info()})
        elif t == "status":
            g.status = {k: v for k, v in msg.items() if k != "t"}
            self.an_ui({"t": "geraet_status", "rolle": g.rolle, "status": g.status})
        elif t == "log":
            self.protokollieren(f"{self.geraet_name(g, g.rolle)}: {str(msg.get('text', ''))[:200]}", "geraet")
        elif t == "werte":
            self.an_ui({"t": "werte", "v": msg.get("v", [])})
        elif t == "fx_fehlt":
            log.info("%s kennt Effekt %s nicht – Abgleich", self.geraet_name(g, g.rolle), msg.get("id"))
            if time.monotonic() - self._letzter_fx_sync > 10:
                await self.fx_sync()
        elif t == "fx_sync_fertig":
            if msg.get("geladen") or msg.get("geloescht") or msg.get("fehler"):
                text = (f"Effekte abgeglichen ({self.geraet_name(g, g.rolle)}): {msg.get('geladen', 0)} geladen, "
                        f"{msg.get('geloescht', 0)} gelöscht")
                if msg.get("fehler"):
                    text += f", {msg['fehler']} Fehler ({msg.get('meldung', '')})"
                self.protokollieren(text, "fehler" if msg.get("fehler") else "geraet")
            g.hallo["fx_frei_kb"] = msg.get("fx_frei_kb", g.hallo.get("fx_frei_kb"))

    async def sound_konfigurieren(self) -> None:
        t = self.ton_effektiv()
        await self.geraete.senden("sound", {"t": "config", **t.model_dump()})

    async def fx_sync(self) -> None:
        self._letzter_fx_sync = time.monotonic()
        await self.geraete.senden("sound", {"t": "fx_sync", "basis": "/api/geraet/fx/", "liste": self.medien.fx_sync_liste()})

    async def geraet_neustart(self, rolle: str) -> bool:
        return await self.geraete.senden(rolle, {"t": "neustart"})

    # ------------------------------------------------------------------
    # Licht
    # ------------------------------------------------------------------
    async def _licht_senden(self) -> None:
        await self.licht.zustand(self.licht_effektiv(), temp=self.overlay is not None)
        self._zustand_geaendert()

    async def licht_aendern(self, *, an: bool | None = None, hell: int | None = None,
                            musik_reaktiv: int | None = None, zonen: list[Zone] | None = None) -> None:
        z = self.zustand.licht
        if an is not None:
            z.an = an
        if hell is not None:
            z.hell = max(0, min(100, hell))
        if musik_reaktiv is not None:
            z.musik_reaktiv = max(0, min(100, musik_reaktiv))
            self.zustand.stimmung_id = None
        if zonen is not None:
            z.zonen = [zz.model_copy() for zz in zonen[: self.einst.licht.zonen]]
            self._zonen_anpassen()
            self.zustand.stimmung_id = None
            self.overlay = None  # Handbedienung hat Vorrang vor der Szene
        self.speichern("zustand")
        await self._licht_senden()

    async def zone_setzen(self, i: int, zone: Zone) -> None:
        if not 0 <= i < len(self.zustand.licht.zonen):
            raise ValueError("Zone gibt es nicht")
        zonen = list(self.zustand.licht.zonen)
        zonen[i] = zone
        await self.licht_aendern(zonen=zonen)

    async def stimmung_anwenden(self, sid: str) -> Stimmung:
        s = next((s for s in self.stimmungen.liste if s.id == sid), None)
        if not s:
            raise KeyError(sid)
        n = self.einst.licht.zonen
        self.zustand.licht.zonen = [s.zonen[i % len(s.zonen)].model_copy() for i in range(n)]
        self.zustand.licht.musik_reaktiv = s.musik_reaktiv
        self.zustand.stimmung_id = s.id
        self.overlay = None
        self.speichern("zustand")
        await self._licht_senden()
        return s

    async def overlay_setzen(self, s: Stimmung | None) -> None:
        self.overlay = s
        await self._licht_senden()

    async def aktion(self, a: LichtAktion, verz_ms: int = 0, dauer_ms: int | None = None, quelle: str = "") -> None:
        dauer = dauer_ms if dauer_ms is not None else (a.dauer_ms or 1000)
        await self.licht.aktion(a, verz_ms, dauer)
        if quelle:
            self.protokollieren(f"{quelle}: Licht „{a.name}“", "aktion")

    async def alles_stoppen(self) -> None:
        if self.spieler:
            await self.spieler.stoppen()
        await self.licht.stopp()
        await self.geraete.senden("sound", {"t": "fx_stop"})
        self.protokollieren("Alles gestoppt", "info")

    # ------------------------------------------------------------------
    # Sound
    # ------------------------------------------------------------------
    def fx_waehlen(self, kategorie: str) -> Effekt | None:
        """Zufälliger Sound der Kategorie – nicht zweimal hintereinander derselbe."""
        auswahl = self.medien.fx_der_kategorie(kategorie)
        if not auswahl:
            return None
        if len(auswahl) > 1:
            auswahl = [f for f in auswahl if f.id != self._zuletzt_gespielt.get(kategorie)] or auswahl
        f = random.choice(auswahl)
        self._zuletzt_gespielt[kategorie] = f.id
        return f

    async def fx_spielen(self, f: Effekt, vol: int = 100, licht: LichtAktion | None | str = "auto",
                         quelle: str = "") -> None:
        """Effekt-Sound mit passendem Licht.

        Hängt das Licht per Kabel am Sound-Gerät (Licht-ESP per USB am Pi bzw. per UART am Sound-ESP), geht die
        Licht-Aktion mit dem Sound dorthin und von dort im selben Moment wie der erste Ton
        weiter. Sonst schickt der Server sie direkt, um die Ausgabe-Latenz verzögert.
        """
        if licht == "auto":
            licht = f.licht
        elif licht == "keine":
            licht = None
        assert licht is None or isinstance(licht, LichtAktion)
        if licht is not None and licht.name == "keine":
            licht = None
        dauer = (licht.dauer_ms if licht and licht.dauer_ms else f.dauer_ms + 300) if licht else 0

        sound = self.geraete.get("sound")
        mit_ton = sound is not None and not self.stumm
        if mit_ton:
            lg = self.geraete.get("licht")
            per_kabel = bool(sound.status.get("uart")) and bool(lg and lg.status.get("uart"))
            nachricht: dict[str, Any] = {"t": "fx", "id": f.id, "vol": round(f.vol * vol / 100)}
            if licht and per_kabel:
                nachricht["licht"] = {**licht.model_dump(), "dauer_ms": dauer}
            await sound.senden(nachricht)
            if licht and not per_kabel:
                await self.licht.aktion(licht, self.ton_effektiv().latenz_ms, dauer)
        elif licht:
            ersatz = licht.model_copy(update={"name": OHNE_TON.get(licht.name, licht.name)})
            await self.licht.aktion(ersatz, 0, dauer)
        if quelle:
            zusatz = "" if mit_ton else " (nur Licht)"
            self.protokollieren(f"{quelle}: {f.name}{zusatz}", "fx")

    async def fx_kategorie(self, kategorie: str, vol: int = 100, quelle: str = "") -> Effekt | None:
        f = self.fx_waehlen(kategorie)
        if f:
            await self.fx_spielen(f, vol, quelle=quelle)
        return f

    async def latenz_test(self) -> None:
        """Klick + kurzer Blitz auf allen Zonen: Latenz so einstellen, dass beides zusammenfällt."""
        klick = next((f for f in self.medien.daten.fx if f.kategorie == "system"), None)
        blitz = LichtAktion(name="alle_an", dauer_ms=150, staerke=100)
        if klick:
            await self.fx_spielen(klick, 100, blitz, quelle="Latenz-Test")
        else:
            await self.aktion(blitz, quelle="Latenz-Test")

    async def ambiente_schalten(self, an: bool) -> None:
        if an == self.zustand.ambiente_an:
            return  # nichts ändern: ein erneutes "play" würde den laufenden Stream neu starten (hörbare Lücke)
        self.zustand.ambiente_an = an
        self.speichern("zustand")
        await self.geraete.senden("sound", {"t": "musik", "aktion": "play" if an else "stop", "pfad": "/stream/ambiente"})
        self._zustand_geaendert()

    async def ambiente_spielen(self, mid: str) -> None:
        """Ein Stück sofort spielen (Ambiente geht dafür an). Der Stream beginnt neu mit diesem Stück; ein Pi
        verwirft dazu seinen Vorrat (sonst wäre der Wechsel erst nach puffer_s zu hören)."""
        if not self.medien.musik(mid):
            raise ValueError("Dieses Stück gibt es nicht")
        await self.ambiente.springen(mid)
        if not self.zustand.ambiente_an:
            await self.ambiente_schalten(True)
        else:
            g = self.geraete.get("sound")
            if g and g.hallo.get("plattform") in ("pi", "browser"):
                await g.senden({"t": "musik", "aktion": "sprung", "pfad": "/stream/ambiente"})
        self._zustand_geaendert()

    async def musik_analysieren(self, mid: str) -> None:
        """Stück analysieren (Gesang, Ruhe, Takt, Stimmung) und speichern. Läuft im Hintergrund-Thread."""
        if mid in self.analyse_laeuft:
            return
        self.analyse_laeuft.add(mid)
        self.daten_geaendert()
        try:
            pfad = self.medien.musik_pfad(mid)
            a = await asyncio.get_running_loop().run_in_executor(self._analyse_pool, musikanalyse.analysieren, pfad)
            s = self.medien.musik(mid)
            if s:
                s.analyse = a
                self.medien.speichern()
                log.info("Analyse %s: %s, %s BPM, Gesang %d Abschnitte", s.name, a.stimmung, a.bpm, len(a.stimme))
        except Exception:
            log.exception("Musikanalyse %s fehlgeschlagen", mid)
        finally:
            self.analyse_laeuft.discard(mid)
            self.daten_geaendert()

    async def analysen_nachholen(self) -> None:
        """Beim Start: Stücke und Effekte ohne (aktuelle) Analyse nacheinander analysieren."""
        for s in list(self.medien.daten.musik):
            if s.analyse is None or s.analyse.version < musikanalyse.ANALYSE_VERSION:
                await self.musik_analysieren(s.id)
        for f in list(self.medien.daten.fx):
            if f.kategorie != "system" and (f.analyse is None or f.analyse.version < fxanalyse.FX_ANALYSE_VERSION):
                await self.fx_analysieren(f.id)

    async def fx_analysieren(self, fid: str) -> None:
        f = self.medien.fx(fid)
        if not f:
            return
        try:
            a = await asyncio.get_running_loop().run_in_executor(
                self._analyse_pool, fxanalyse.analysieren, self.medien.fx_pfad(fid), f.name, f.kategorie)
        except Exception:  # noqa: BLE001 – ohne Analyse geht es auch (Regisseur nimmt dann den Namen)
            log.exception("Effekt-Analyse %s", fid)
            return
        f = self.medien.fx(fid)
        if f:
            f.analyse = a
            self.medien.speichern()
            self.daten_geaendert()

    async def fx_ersetzen(self, alt_id: str, neu: Effekt) -> None:
        """Effekt durch eine neue Datei ersetzen (neue ID): Eintrag an derselben Stelle, alte Dateien weg,
        Szenen-Verweise angepasst, neu analysiert. fx_sync macht der Aufrufer."""
        liste = self.medien.daten.fx
        liste[:] = [neu if x.id == alt_id else x for x in liste if x.id != neu.id or x.id == alt_id]
        if neu.id != alt_id:
            self.medien.fx_pfad(alt_id).unlink(missing_ok=True)
            self.medien.fx_vorhoer_pfad(alt_id).unlink(missing_ok=True)
            geaendert = False
            for s in self.szenen.liste:
                for sch in s.schritte:
                    if sch.fx_id == alt_id:
                        sch.fx_id, geaendert = neu.id, True
            if geaendert:
                self.speichern("szenen")
        self.medien.speichern()
        await self.fx_analysieren(neu.id)

    async def fx_nachbearbeiten(self) -> dict:
        """Vorhandene Effekte mit langer Stille am Ende neu umwandeln (gekürzt). Neue Datei = neue ID: Verweise in
        Szenen werden angepasst, der Pi gleicht danach ab."""
        gekuerzt, gespart = 0, 0
        for alt in list(self.medien.daten.fx):
            if alt.kategorie == "system" or not alt.analyse or alt.dauer_ms - alt.hoerbar_ms < 500:
                continue
            try:
                fid, dauer_ms, groesse = await medien_mod.fx_umwandeln(self.medien.fx_pfad(alt.id))
            except Exception:  # noqa: BLE001
                log.exception("Nachbearbeiten %s", alt.id)
                continue
            if fid == alt.id or dauer_ms >= alt.dauer_ms:
                continue
            gespart += alt.dauer_ms - dauer_ms
            gekuerzt += 1
            await self.fx_ersetzen(alt.id, alt.model_copy(update={"id": fid, "dauer_ms": dauer_ms, "groesse": groesse}))
        if gekuerzt:
            await self.fx_sync()
            self.daten_geaendert()
            self.protokollieren(f"Effekte nachbearbeitet: {gekuerzt} gekürzt, {gespart / 1000:.1f} s Stille weg", "info")
        return {"gekuerzt": gekuerzt, "gespart_s": round(gespart / 1000, 1)}

    def _ambiente_pos(self) -> dict | None:
        """Für die Fortschrittsanzeige: Stück, Sekunde (draußen zu hören) und Länge; die Oberfläche zählt selbst weiter."""
        p = self.ambiente.position() if self.zustand.ambiente_an else None
        if p is None:
            return None
        s = self.medien.musik(p[0])
        return {"id": p[0], "pos": round(p[1], 2), "dauer": s.dauer_s if s else 0}

    def musik_lage(self) -> tuple[Musikstueck | None, float | None]:
        """Welches Ambiente-Stück ist draußen gerade an welcher Sekunde zu hören? (None, None) ohne Ambiente."""
        if not self.zustand.ambiente_an or not self.geraete.verbunden("sound"):
            return None, None
        p = self.ambiente.position()
        if p is None:
            return None, None
        return self.medien.musik(p[0]), p[1]

    async def ambiente_weiter(self) -> None:
        """Nächstes Stück – sofort zu hören (wie ambiente_spielen mit dem Nachfolger des laufenden Stücks)."""
        mid = self.ambiente.naechstes_nach(self.ambiente.jetzt_id)
        if mid and self.zustand.ambiente_an:
            await self.ambiente_spielen(mid)

    async def musik_daempfen(self, art: str, dauer_ms: int) -> None:
        """Hintergrundmusik für dauer_ms aus („stille“) bzw. leise – danach von selbst wieder normal."""
        self.musik_daempfung = (art, time.monotonic() + dauer_ms / 1000)
        if self._daempfung_task:
            self._daempfung_task.cancel()
        self._daempfung_task = asyncio.create_task(self._daempfung_ende(dauer_ms / 1000), name="musik-daempfung")
        await self.sound_konfigurieren()

    async def _daempfung_ende(self, s: float) -> None:
        await asyncio.sleep(s)
        await self.musik_normal()

    async def musik_normal(self) -> None:
        if self.musik_daempfung is None:
            return
        self.musik_daempfung = None
        await self.sound_konfigurieren()

    async def musik_duester(self, quelle: str = "") -> bool:
        """Zu einem düsteren/dramatischen Stück wechseln (Musikanalyse), sofort zu hören."""
        if not self.zustand.ambiente_an:
            return False
        jetzt = self.ambiente.jetzt_id
        auswahl = [m for m in self.medien.daten.musik if m.id != jetzt and m.analyse
                   and m.analyse.stimmung in ("duester", "dramatisch")]
        if not auswahl:
            return False
        m = random.choice(auswahl)
        await self.ambiente_spielen(m.id)
        if quelle:
            self.protokollieren(f"{quelle}: Musik wechselt zu „{m.name}“", "fx")
        return True

    async def ton_geaendert(self) -> None:
        await self.sound_konfigurieren()
        self._zustand_geaendert()

    # ------------------------------------------------------------------
    # Show, Automatik
    # ------------------------------------------------------------------
    async def show(self, an: bool, ambiente: bool = True, ereignisse: bool = True, quelle: str = "") -> None:
        """Alles an/aus: Licht, Ambiente, Ereignisse."""
        await self.licht_aendern(an=an)
        if ambiente or not an:
            await self.ambiente_schalten(an)
        if ereignisse or not an:
            self.ereignisse_schalten(an)
        if not an and self.spieler:
            await self.spieler.stoppen()
        self.protokollieren(f"Show {'an' if an else 'aus'}{f' ({quelle})' if quelle else ''}", "info")

    def ereignisse_schalten(self, an: bool) -> None:
        self.zustand.ereignisse_an = an
        self.speichern("zustand")
        if self.generator:
            self.generator.neu_planen()
        self._zustand_geaendert()

    def _hintergrund(self, coro) -> None:
        t = asyncio.create_task(coro, name="ki-stimme")
        self._ki_tasks.add(t)
        t.add_done_callback(self._ki_tasks.discard)

    def ausloeser_aus(self, aid: str) -> None:
        """Sensor eines Auslösers ist wieder aus (nur für den Besuchs-Ablauf interessant)."""
        a = next((x for x in self.einst.ausloeser if x.id == aid), None)
        if a and a.typ == "ki" and a.rolle in ("fern", "nah"):
            self._hintergrund(self.besuch.sensor(a, False, a.name))

    async def _ki_ausloesen(self, a: Ausloeser, quelle: str) -> None:
        lage = a.hinweis or {"fern": LOCKEN, "nah": TUER}.get(a.rolle, "")
        if await self.ki.ansprechen(a.ziel, a.vol, quelle, lage) in ("niemand", "fehler"):
            await self._ki_ersatz(a, f"{quelle} (Ersatz)")

    async def _ki_ersatz(self, a: Ausloeser, quelle: str) -> bool:
        """Ersatz eines KI-Auslösers: eigene Szene/Kategorie oder die Ersatz-Kategorie der KI-Einstellungen."""
        if a.ersatz_typ == "szene" and a.ersatz_ziel:
            try:
                await self.szene_starten(a.ersatz_ziel, quelle)
                return True
            except KeyError:
                self.protokollieren(f"{quelle}: Szene „{a.ersatz_ziel}“ gibt es nicht", "fehler")
                return False
        kat = a.ersatz_ziel or self.einst.ki.ersatz_kategorie
        return bool(kat and await self.fx_kategorie(kat, a.vol, quelle=quelle))

    def ki_schalten(self, an: bool) -> None:
        self.zustand.ki_an = an
        self.speichern("zustand")
        self.protokollieren(f"KI-Stimme {'an' if an else 'aus – KI-Auslöser spielen Ersatz-Sounds'}", "info")
        self._zustand_geaendert()

    async def test_sensor(self, rolle: str, an: bool = True) -> bool:
        """PC-Test: Weg-/Tür-Sensor bzw. Klingel wie aus Home Assistant (gleiche Sperren). KeyError: kein Auslöser."""
        a = next((a for a in self.einst.ausloeser if a.aktiv and a.rolle == rolle), None)
        if a is None:
            raise KeyError(rolle)
        if not an:
            self.ausloeser_aus(a.id)
            return False
        return await self.ausloeser(a.id, f"Test: {a.name}")

    def regie_schalten(self, an: bool) -> None:
        self.zustand.regie_an = an
        self.speichern("zustand")
        self.protokollieren(f"KI-Regisseur {'an – wählt bei Besuchern die Effekte' if an else 'aus'}", "info")
        self._zustand_geaendert()

    def gruselstufe(self, stufe: int) -> None:
        stufe = max(1, min(3, stufe))
        if stufe != self.zustand.gruselstufe:
            self.zustand.gruselstufe = stufe
            self.speichern("zustand")
            self.protokollieren(f"Gruselstufe {stufe}", "info")
            self._zustand_geaendert()

    async def szene_starten(self, sid: str, quelle: str = "") -> Szene:
        s = next((s for s in self.szenen.liste if s.id == sid), None)
        if not s:
            raise KeyError(sid)
        assert self.spieler
        await self.spieler.starten(s, quelle)
        return s

    async def ausloeser(self, aid: str, quelle: str = "", pruefen: bool = True) -> bool:
        """Auslöser von außen (Klingel, Personenerkennung …).

        pruefen=False (Test-Knopf): Sperrzeit, Gruselstufe und Show-Zustand ignorieren.
        """
        a = next((x for x in self.einst.ausloeser if x.id == aid), None)
        if not a:
            raise KeyError(aid)
        quelle = quelle or a.name
        jetzt = time.monotonic()
        if a.rolle == "klingel" and pruefen:
            self.besuch.klingel()
        if a.typ == "ki" and a.rolle in ("fern", "nah") and self.zustand.ki_an and pruefen:
            # Besuchs-Ablauf entscheidet selbst, ob und was gesagt wird (statt Sperrzeit)
            if (a.nur_wenn_an and not self.zustand.licht.an) or self.zustand.gruselstufe < a.gruselstufe_min:
                return False
            self._hintergrund(self.besuch.sensor(a, True, quelle))
            return True
        if pruefen:
            if a.nur_wenn_an and not self.zustand.licht.an:
                return False
            if self.zustand.gruselstufe < a.gruselstufe_min:
                return False
            if jetzt - self._letzte_ausloesung.get(a.id, -1e9) < a.sperre_s:
                return False
        self._letzte_ausloesung[a.id] = jetzt
        if a.typ == "ki" and not self.zustand.ki_an:
            return await self._ki_ersatz(a, quelle)      # KI abgeschaltet: wie ein normaler Auslöser
        if a.typ == "ki":
            if self.ki.laeuft:
                return False
            # läuft im Hintergrund (Bild + Modell + Stimme dauern Sekunden), HA-Verbindung nicht aufhalten
            self._hintergrund(self._ki_ausloesen(a, quelle))
            return True
        if a.typ == "kategorie":
            if not await self.fx_kategorie(a.ziel, a.vol, quelle=quelle):
                self.protokollieren(f"{quelle}: keine Sounds in „{KATEGORIEN.get(a.ziel, a.ziel)}“", "fehler")
                return False
            return True
        try:
            await self.szene_starten(a.ziel, quelle)
        except KeyError:
            self.protokollieren(f"{quelle}: Szene „{a.ziel}“ gibt es nicht", "fehler")
            return False
        return True

    # ------------------------------------------------------------------
    # Einstellungen
    # ------------------------------------------------------------------
    async def einstellungen_aendern(self, teil: str, daten: dict) -> None:
        if teil not in Einstellungen.model_fields or teil == "ausloeser":
            raise KeyError(teil)
        alt = getattr(self.einst, teil)
        neu_daten = {**alt.model_dump(), **daten}
        for t, feld in GEHEIME_FELDER:  # Platzhalter = unverändert lassen
            if t == teil and neu_daten.get(feld) == GEHEIM_PLATZHALTER:
                neu_daten[feld] = getattr(alt, feld)
        neu = type(alt).model_validate(neu_daten)
        setattr(self.einst, teil, neu)
        self.speichern("einst")
        if teil == "licht":
            self._zonen_anpassen()
            self.speichern("zustand")
            await self.licht.konfigurieren(neu)
            await self._licht_senden()
        elif teil == "ton":
            await self.ton_geaendert()
        self._melden(f"einst:{teil}")
        self.daten_geaendert()

    def ausloeser_setzen(self, liste: list[Ausloeser]) -> None:
        self.einst = self.einst.model_copy(update={"ausloeser": Einstellungen.model_validate(
            {**self.einst.model_dump(), "ausloeser": [a.model_dump() for a in liste]}).ausloeser})
        self.speichern("einst")
        self._melden("einst:ausloeser")
        self.daten_geaendert()

    def regeln_setzen(self, e: Ereignisse) -> None:
        vorhanden: set[str] = set()
        for r in e.regeln:
            if not r.id or r.id in vorhanden:
                r.id = f"r{random.randrange(16**6):06x}"
            vorhanden.add(r.id)
            if r.max_s < r.min_s:
                r.min_s, r.max_s = r.max_s, r.min_s
        self.ereignisse = e
        self.speichern("ereignisse")
        if self.generator:
            self.generator.neu_planen()
        self.daten_geaendert()

    def regel_beschreibung(self, r: Regel) -> str:
        if r.typ == "kategorie":
            return KATEGORIEN.get(r.ziel, r.ziel)
        s = next((s for s in self.szenen.liste if s.id == r.ziel), None)
        return f"Szene {s.name if s else r.ziel}"
