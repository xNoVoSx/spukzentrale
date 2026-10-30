"""Mischer: Ambiente + Effekte -> ALSA (PCM5102A), dazu Hüllkurve/Spektrum/Aktionen ans Licht – im 10-ms-Takt.

Gleiche Klangregeln wie der Sound-ESP (Ducking ~30 ms runter / ~700 ms hoch, weicher Limiter ab 0,8, Ambiente
1 s ein- / 250 ms ausblenden). Neu: Die Ausgabe-Latenz misst der Pi selbst (Füllstand des ALSA-Puffers) – Licht
und Ton sind damit ohne Einmessen synchron. latenz_ms aus der Oberfläche kommt nur noch obendrauf (z. B. für
eine Bluetooth-Box).
"""

import collections
import logging
import queue
import threading
import time

import numpy as np

from . import protokoll
from .analyse import Analyse
from .ambiente import Ambiente
from .effekte import Effekte
from .licht import LichtBruecke

log = logging.getLogger(__name__)

RATE = 44100
BLOCK = 441                 # 10 ms
PERIODEN = 10               # ALSA-Puffer 100 ms: genug Reserve gegen Python-Ruckler, Latenz wird ausgeglichen
MAX_STIMMEN = 8


def begrenze(x: np.ndarray) -> np.ndarray:
    """Weicher Limiter: bis 0,8 linear, darüber per tanh sanft gegen 1,0."""
    s = 0.8
    a = np.abs(x)
    ueber = a > s
    if not ueber.any():
        return x
    y = x.copy()
    y[ueber] = np.sign(x[ueber]) * (s + (1 - s) * np.tanh((a[ueber] - s) / (1 - s)))
    return y


class Stimme:
    def __init__(self, fid: str, klang: np.ndarray, vol: float, aktion: dict | None) -> None:
        self.fid = fid
        self.klang = klang
        self.pos = 0
        self.vol = vol
        self.aktion = aktion

    def block(self, n: int) -> np.ndarray:
        teil = np.asarray(self.klang[self.pos:self.pos + n], np.float32)
        if self.pos == 0 and len(teil):                       # 2 ms einblenden gegen Knackser
            k = min(len(teil), 88)
            teil = teil.copy()
            teil[:k] *= np.linspace(0, 1, k, dtype=np.float32)
        self.pos += n
        if len(teil) < n:
            teil = np.concatenate([teil, np.zeros(n - len(teil), np.float32)])
        return teil * self.vol

    @property
    def fertig(self) -> bool:
        return self.pos >= len(self.klang)


class Mischer(threading.Thread):
    def __init__(self, geraet: str, ambiente: Ambiente, effekte: Effekte, licht: LichtBruecke | None,
                 analyse: bool = True) -> None:
        super().__init__(name="mischer", daemon=True)
        self.geraet = geraet
        self.ambiente = ambiente
        self.effekte = effekte
        self.licht = licht
        self.analyse = Analyse(BLOCK) if analyse else None
        self.cfg = {"master": 80, "musik_vol": 60, "fx_vol": 100, "duck": 40, "latenz_ms": 0}
        self.befehle: queue.Queue = queue.Queue()
        self.stimmen: list[Stimme] = []
        self.server_verbunden = False
        self._laeuft = True
        # Zustand
        self.musik_fade = 0.0
        self.musik_stop_anfrage = False
        self.duck_ist = 1.0
        self.latenz_ms = PERIODEN * 10.0
        self._verz: collections.deque[bytes] = collections.deque()
        # Status
        self.alsa_ok = False
        self.alsa_fehler = 0
        self.xruns = 0
        self.musik_spielt = False
        self.takt_max_ms = 0.0
        self.effekte_gespielt = 0

    # ------------------------------------------------------------------ Steuerung (aus jedem Thread)
    def konfigurieren(self, c: dict) -> None:
        for k in self.cfg:
            if k in c:
                try:
                    self.cfg[k] = max(0, min(600 if k == "latenz_ms" else 100, int(c[k])))
                except (TypeError, ValueError):
                    pass

    def musik_play(self, pfad: str) -> None:
        self.befehle.put(("play", pfad))

    def musik_stop(self) -> None:
        self.befehle.put(("stop", None))

    def musik_sprung(self) -> None:
        self.befehle.put(("sprung", None))

    def fx_start(self, fid: str, vol: int, licht: dict | None) -> bool:
        if not self.effekte.hat(fid):
            return False
        self.befehle.put(("fx", (fid, vol, licht)))
        return True

    def klang_start(self, name: str, klang: np.ndarray, vol: int, licht: dict | None) -> None:
        """Einmaliger Klang (KI-Ansage), schon dekodiert: mono float32, 44,1 kHz."""
        self.befehle.put(("klang", (name, klang, vol, licht)))

    def fx_stop_alle(self) -> None:
        self.befehle.put(("fx_stop", None))

    def beenden(self) -> None:
        self._laeuft = False

    def status(self) -> dict:
        a = self.ambiente
        return {
            "musik": a.soll and not self.musik_stop_anfrage,
            "musik_verbunden": a.verbunden,
            "musik_spielt": self.musik_spielt,
            "musik_quelle": a.quelle,
            "format_fehler": False,
            "puffer": min(100, int(a.vorrat.sekunden * 100 / max(a.puffer_s, 1))),
            "puffer_s": round(a.vorrat.sekunden, 1),
            "stimmen": len(self.stimmen),
            "unterlaeufe": a.unterlaeufe,
            "ersatz_wechsel": a.ersatz_wechsel,
            "neuverbindungen": a.neuverbindungen,
            "alsa": self.alsa_ok,
            "alsa_xruns": self.xruns,
            "latenz_gemessen_ms": round(self.latenz_ms),
            "takt_max_ms": round(self.takt_max_ms, 1),
        }

    # ------------------------------------------------------------------ Mischer-Thread
    def _oeffnen(self):
        import alsaaudio
        pcm = alsaaudio.PCM(alsaaudio.PCM_PLAYBACK, device=self.geraet, channels=2, rate=RATE,
                            format=alsaaudio.PCM_FORMAT_S16_LE, periodsize=BLOCK, periods=PERIODEN)
        info = pcm.info()
        log.info("Audio: %s, %d Hz, Periode %d, Puffer %d Frames", info.get("name"), info.get("rate"),
                 info.get("period_size"), info.get("buffer_size"))
        return pcm, int(info.get("buffer_size", BLOCK * PERIODEN))

    def _befehle(self) -> None:
        while True:
            try:
                art, arg = self.befehle.get_nowait()
            except queue.Empty:
                return
            if art == "play":
                self.musik_stop_anfrage = False
                self.ambiente.play(arg)
            elif art == "stop":
                if self.ambiente.soll:
                    self.musik_stop_anfrage = True
            elif art == "sprung":
                self.musik_stop_anfrage = False
                if self.ambiente.soll:
                    self.ambiente.sprung()
                else:
                    self.ambiente.play("/stream/ambiente")
            elif art == "fx":
                fid, vol, licht = arg
                klang = self.effekte.klang(fid)
                if klang is None:
                    continue
                if len(self.stimmen) >= MAX_STIMMEN:
                    self.stimmen.pop(0)
                self.stimmen.append(Stimme(fid, klang, max(0, min(100, vol)) / 100, licht))
                self.effekte_gespielt += 1
            elif art == "klang":
                name, klang, vol, licht = arg
                if len(self.stimmen) >= MAX_STIMMEN:
                    self.stimmen.pop(0)
                self.stimmen.append(Stimme(name, klang, max(0, min(100, vol)) / 100, licht))
                self.effekte_gespielt += 1
            elif art == "fx_stop":
                self.stimmen.clear()

    def _licht(self, daten: bytes) -> None:
        if self.licht is not None and self.licht.aktiv:
            self.licht.schreiben(daten)

    def run(self) -> None:
        pcm = None
        puffer_frames = BLOCK * PERIODEN
        while self._laeuft:
            if pcm is None:
                try:
                    pcm, puffer_frames = self._oeffnen()
                    self.alsa_ok = True
                except Exception as e:
                    self.alsa_ok = False
                    self.alsa_fehler += 1
                    if self.alsa_fehler in (1, 10) or self.alsa_fehler % 100 == 0:
                        log.error("Audio-Gerät %s lässt sich nicht öffnen: %s", self.geraet, e)
                    time.sleep(2)
                    continue
            t0 = time.monotonic()
            try:
                daten = self._block()
                n = pcm.write(daten)
                if n is not None and n < 0:
                    self.xruns += 1
                try:
                    _s, _ns, frei = pcm.htimestamp()[:3]
                    gemessen = (puffer_frames - frei) * 1000.0 / RATE
                    self.latenz_ms = self.latenz_ms * 0.9 + gemessen * 0.1
                except Exception:
                    pass
            except Exception as e:
                log.error("Audio-Ausgabe: %s – Gerät wird neu geöffnet", e)
                self.alsa_ok = False
                try:
                    pcm.close()
                except Exception:
                    pass
                pcm = None
                time.sleep(0.5)
                continue
            # Zeit, die ein Durchlauf ohne das Warten in write() bräuchte, lässt sich nicht trennen –
            # der Maximalwert zeigt aber Ruckler (> 100 ms = ALSA-Puffer leer gelaufen).
            self.takt_max_ms = max(self.takt_max_ms * 0.999, (time.monotonic() - t0) * 1000)

    def _block(self) -> bytes:
        self._befehle()
        c = self.cfg
        n = BLOCK

        # --- Ambiente mit weichem Ein-/Ausblenden ---
        musik = self.ambiente.lesen(n) if self.ambiente.soll else None
        self.musik_spielt = musik is not None
        ziel = 1.0 if (self.ambiente.soll and not self.musik_stop_anfrage) else 0.0
        alt = self.musik_fade
        if ziel != alt:
            schritt = 0.01 if ziel > alt else 0.04          # 1 s ein, 250 ms aus
            self.musik_fade = min(ziel, alt + schritt) if ziel > alt else max(ziel, alt - schritt)
        if self.musik_stop_anfrage and self.musik_fade <= 0:
            self.musik_stop_anfrage = False
            self.ambiente.stop()

        # --- Effekte (mono) – nur rechnen, wenn welche laufen ---
        verz_ms = int(self.latenz_ms + c["latenz_ms"])
        fx = None
        fx_spitze = 0.0
        if self.stimmen:
            fx = np.zeros(n, np.float32)
            for s in self.stimmen:
                if s.pos == 0 and s.aktion:
                    rahmen = protokoll.aktion(s.aktion, verz_ms)
                    if rahmen:
                        self._licht(rahmen)
                fx += s.block(n)
            self.stimmen = [s for s in self.stimmen if not s.fertig]
            fx *= c["fx_vol"] / 100
            fx_spitze = float(np.abs(fx).max())

        # --- Ducking ---
        duck_ziel = 1.0 - c["duck"] / 100 if self.stimmen else 1.0
        self.duck_ist += (duck_ziel - self.duck_ist) * (0.30 if duck_ziel < self.duck_ist else 0.015)

        # --- Mischen ---
        master = c["master"] / 100
        if musik is not None and (alt > 0 or self.musik_fade > 0):
            g = c["musik_vol"] / 100 * self.duck_ist
            if alt == self.musik_fade:
                m = musik * (g * alt)
            else:
                m = musik * (g * np.linspace(alt, self.musik_fade, n, dtype=np.float32))[:, None]
        else:
            m = None
        if m is None and fx is None:
            aus = None
        elif fx is None:
            aus = m * master
        else:
            aus = (np.column_stack((fx, fx)) if m is None else m + fx[:, None]) * master   # immer stereo
        if aus is None:
            pcm = bytes(n * 4)
        else:
            if aus.max() > 0.8 or aus.min() < -0.8:
                aus = begrenze(aus)
            pcm = (aus * 32767).astype(np.int16).tobytes()

        # --- Licht: Hüllkurve + Spektrum, um die Ausgabe-Latenz verzögert ---
        # Grundlage ist die Musik VOR Ducking und Lautstärke: Unter einem Effekt (Donner …) wird die Musik leiser,
        # das Lied ist aber dasselbe – das Musiklicht soll nicht mit absacken.
        if self.licht is not None and self.licht.aktiv:
            if m is not None:
                mono = (musik[:, 0] + musik[:, 1]) * (0.5 * max(alt, self.musik_fade))
                rms = float(np.sqrt(np.dot(mono, mono) / n))
            else:
                mono, rms = None, 0.0
            flags = (protokoll.FLAG_MUSIK_AKTIV if self.musik_spielt else 0) | \
                    (protokoll.FLAG_FX_AKTIV if self.stimmen else 0) | \
                    (protokoll.FLAG_SERVER if self.server_verbunden else 0)
            rahmen = protokoll.huelle(rms * 2 * 255, fx_spitze * 255, flags)
            if self.analyse is not None:
                werte = self.analyse.verarbeiten(mono) if mono is not None else self.analyse.still()
                rahmen += protokoll.spektrum(*werte)
            self._verz.append(rahmen)
            soll = max(0, round(verz_ms / 10))
            while len(self._verz) > soll:
                self._licht(self._verz.popleft())
        return pcm
