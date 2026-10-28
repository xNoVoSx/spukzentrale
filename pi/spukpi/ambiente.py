"""Ambiente: Endlos-Stream vom Server mit großem Vorrat, Mitschnitt auf der SD-Karte, lückenloser Ersatz.

    Server ──HTTP (MP3)──► Netz-Thread ──► ffmpeg (Dekoder) ──► PCM-Vorrat (bis puffer_s + Reserve)
                               └──► Mitschnitt (mitschnitt-N.mp3, je 5 min, die letzten 3 bleiben)

Der Mischer holt alle 10 ms einen Block über lesen(). Bricht der Stream ab (WLAN, Server weg) und der Vorrat
geht zur Neige, blendet lesen() auf den Mitschnitt über (läuft in Schleife) und zurück, sobald der Stream wieder
genug Vorrat hat. So gibt es keine hörbare Lücke – auch im Notbetrieb ohne Server.
"""

import collections
import contextlib
import logging
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

RATE = 44100
KANAELE = 2
START_S = 1.0             # so viel Vorrat, bevor der Stream (wieder) hörbar wird
ZURUECK_S = 3.0           # Vorrat, ab dem vom Ersatz zurück auf den Stream geblendet wird
KNAPP_S = 0.4             # darunter (und keine frischen Daten) -> auf den Ersatz überblenden
BLENDE_S = 0.4
SPRUNG_AUS_S = 0.4        # Sprung (Stück direkt gestartet): so schnell klingt das alte aus
SPRUNG_EIN_S = 0.3        # … und so weich kommt das neue
SPRUNG_KEIN_ERSATZ_S = 8  # so lange nach einem Sprung nicht auf den Mitschnitt ausweichen (alte Musik)
SEGMENT_S = 300           # Mitschnitt-Dateien à 5 min
SEGMENTE = 3
MP3_BYTES_S = 16000       # 128 kbit/s


class PcmVorrat:
    """Thread-sicherer Vorrat an float32-Blöcken (n, 2)."""

    def __init__(self, max_s: float) -> None:
        self.max_frames = int(max_s * RATE)
        self._bloecke: collections.deque[np.ndarray] = collections.deque()
        self._frames = 0
        self._sperre = threading.Lock()

    @property
    def frames(self) -> int:
        return self._frames

    @property
    def sekunden(self) -> float:
        return self._frames / RATE

    def schreiben(self, block: np.ndarray) -> None:
        with self._sperre:
            self._bloecke.append(block)
            self._frames += len(block)
            while self._frames > self.max_frames and self._bloecke:   # zu viel -> Ältestes verwerfen
                self._frames -= len(self._bloecke.popleft())

    def lesen(self, n: int) -> np.ndarray | None:
        """Genau n Frames oder None (dann bleibt alles im Vorrat)."""
        with self._sperre:
            if self._frames < n:
                return None
            teile, rest = [], n
            while rest:
                b = self._bloecke[0]
                if len(b) <= rest:
                    teile.append(self._bloecke.popleft())
                    rest -= len(b)
                else:
                    teile.append(b[:rest])
                    self._bloecke[0] = b[rest:]
                    rest = 0
            self._frames -= n
            return teile[0] if len(teile) == 1 else np.concatenate(teile)

    def leeren(self) -> None:
        with self._sperre:
            self._bloecke.clear()
            self._frames = 0


def _dekoder(eingabe: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *eingabe,
         "-f", "s16le", "-ar", str(RATE), "-ac", str(KANAELE), "pipe:1"],
        stdin=subprocess.PIPE if "pipe:0" in eingabe else subprocess.DEVNULL,
        stdout=subprocess.PIPE, bufsize=0,
    )


def _pcm_lesen(proc: subprocess.Popen, ziel: PcmVorrat, laeuft, voll_s: float | None = None) -> None:
    """Liest PCM aus ffmpeg in den Vorrat. voll_s: Vorrat nicht über so viele Sekunden füllen (warten)."""
    rest = b""
    while laeuft():
        if voll_s is not None and ziel.sekunden > voll_s:
            time.sleep(0.05)
            continue
        daten = proc.stdout.read(16384)
        if not daten:
            return
        daten = rest + daten
        nutz = len(daten) - len(daten) % (KANAELE * 2)
        rest = daten[nutz:]
        if nutz:
            ziel.schreiben(np.frombuffer(daten[:nutz], np.int16).reshape(-1, KANAELE).astype(np.float32) / 32768.0)


class Mitschnitt:
    """MP3-Daten des Streams in Segmenten auf die SD-Karte; das neueste fertige Segment dient als Ersatz."""

    def __init__(self, ordner: Path) -> None:
        self.ordner = ordner
        self.ordner.mkdir(parents=True, exist_ok=True)
        self._datei = None
        self._bytes = 0

    def _segmente(self) -> list[Path]:
        return sorted(self.ordner.glob("mitschnitt-*.mp3"), key=lambda p: p.stat().st_mtime)

    def schreiben(self, daten: bytes) -> None:
        if self._datei is None or self._bytes >= SEGMENT_S * MP3_BYTES_S:
            self.schliessen()
            nr = int(time.time())
            self._datei = open(self.ordner / f"mitschnitt-{nr}.mp3.teil", "wb")
            self._bytes = 0
        self._datei.write(daten)
        self._bytes += len(daten)

    def schliessen(self) -> None:
        if self._datei is None:
            return
        pfad = Path(self._datei.name)
        self._datei.close()
        self._datei = None
        if self._bytes >= 30 * MP3_BYTES_S:          # unter 30 s lohnt sich nicht als Ersatz
            pfad.rename(pfad.with_suffix(""))
        else:
            pfad.unlink(missing_ok=True)
        for alt in self._segmente()[:-SEGMENTE]:
            alt.unlink(missing_ok=True)

    def neuestes(self) -> Path | None:
        s = self._segmente()
        return s[-1] if s else None


class Ambiente:
    def __init__(self, basis_url: str, daten: Path, puffer_s: float) -> None:
        self.basis_url = basis_url
        self.puffer_s = puffer_s
        self.vorrat = PcmVorrat(puffer_s + 10)
        self.ersatz = PcmVorrat(6)
        self.mitschnitt = Mitschnitt(daten / "mitschnitt")
        self.soll = False
        self.pfad = "/stream/ambiente"
        self.verbunden = False
        self.letzte_daten = 0.0
        # Zustand für lesen() (nur vom Mischer-Thread benutzt)
        self.quelle = "aus"           # aus | puffert | stream | ersatz
        self._blende = 1.0            # 1 = ganz auf der aktuellen Quelle
        self._von: str | None = None
        # Zähler für den Status
        self.unterlaeufe = 0
        self.ersatz_wechsel = 0
        self.neuverbindungen = 0
        self._gen = 0
        self._thread: threading.Thread | None = None
        # Sprung: altes Stück aus self._alt ausblenden, neuer Stream läuft schon in den (neuen) Vorrat
        self._alt: PcmVorrat | None = None
        self._alt_pos = 0
        self._sprung_zeit = 0.0
        self._ein = 1.0               # Einblendung nach einem Sprung (1 = voll)
        self._ersatz_thread: threading.Thread | None = None
        self._ersatz_proc: subprocess.Popen | None = None

    # ------------------------------------------------------------------ Steuerung (aus jedem Thread)
    def play(self, pfad: str) -> None:
        self.pfad = pfad or "/stream/ambiente"
        if self.soll:
            return
        self.soll = True
        self._gen += 1
        self._thread = threading.Thread(target=self._netz, args=(self._gen,), name="ambiente", daemon=True)
        self._thread.start()

    def sprung(self) -> None:
        """Der Server hat den Stream mit einem anderen Stück neu begonnen: Vorrat (altes Stück) ausblenden und
        verwerfen, sofort neu verbinden – sonst wäre der Wechsel erst nach puffer_s zu hören."""
        if not self.soll:
            return
        self._gen += 1
        self._alt, self._alt_pos = self.vorrat, 0
        self.vorrat = PcmVorrat(self.puffer_s + 10)
        self._sprung_zeit = time.monotonic()
        self.letzte_daten = 0.0
        self._thread = threading.Thread(target=self._netz, args=(self._gen,), name="ambiente", daemon=True)
        self._thread.start()
        log.info("Ambiente: Sprung – neues Stück")

    def stop(self) -> None:
        self.soll = False
        self._alt = None
        self._gen += 1
        self.vorrat.leeren()
        self._ersatz_stoppen()
        self.quelle = "aus"

    # ------------------------------------------------------------------ Netz-Thread
    def _netz(self, gen: int) -> None:
        laeuft = lambda: self.soll and self._gen == gen  # noqa: E731
        pause = 1.0
        while laeuft():
            url = f"{self.basis_url}{self.pfad}?puffer_s={self.puffer_s:g}"
            proc = None
            try:
                antwort = urllib.request.urlopen(url, timeout=10)
                proc = _dekoder(["-f", "mp3", "-i", "pipe:0"])
                leser = threading.Thread(target=_pcm_lesen, args=(proc, self.vorrat, laeuft), daemon=True)  # Vorrat dieses Laufs
                leser.start()
                self.verbunden = True
                log.info("Ambiente-Stream verbunden")
                pause = 1.0
                while laeuft():
                    daten = antwort.read(4096)
                    if not daten:
                        raise ConnectionError("Stream beendet")
                    self.letzte_daten = time.monotonic()
                    proc.stdin.write(daten)
                    self.mitschnitt.schreiben(daten)
            except Exception as e:
                if laeuft():
                    log.warning("Ambiente-Stream: %s – neuer Versuch in %.0f s", e, pause)
            finally:
                self.verbunden = False
                if proc:
                    with contextlib.suppress(Exception):
                        proc.stdin.close()
                    with contextlib.suppress(Exception):
                        proc.kill()
                    proc.wait()
                self.mitschnitt.schliessen()
            if laeuft():
                self.neuverbindungen += 1
                time.sleep(pause)
                pause = min(pause * 2, 10.0)

    # ------------------------------------------------------------------ Ersatz aus dem Mitschnitt
    def _ersatz_starten(self) -> bool:
        if self._ersatz_thread and self._ersatz_thread.is_alive():
            return True
        datei = self.mitschnitt.neuestes()
        if not datei:
            return False
        self.ersatz.leeren()
        self._ersatz_proc = _dekoder(["-stream_loop", "-1", "-i", str(datei)])
        proc = self._ersatz_proc
        self._ersatz_thread = threading.Thread(
            target=_pcm_lesen, args=(proc, self.ersatz, lambda: self._ersatz_proc is proc, 4.0), daemon=True)
        self._ersatz_thread.start()
        return True

    def _ersatz_stoppen(self) -> None:
        proc, self._ersatz_proc = self._ersatz_proc, None
        if proc:
            with contextlib.suppress(Exception):
                proc.kill()
            proc.wait()
        self.ersatz.leeren()

    # ------------------------------------------------------------------ Mischer-Thread
    def _block(self, quelle: str, n: int) -> np.ndarray | None:
        if quelle == "stream":
            return self.vorrat.lesen(n)
        if quelle == "ersatz":
            return self.ersatz.lesen(n)
        return None

    def _wechseln(self, neu: str) -> None:
        if neu == self.quelle:
            return
        if self.quelle in ("stream", "ersatz") and neu in ("stream", "ersatz"):
            self._von, self._blende = self.quelle, 0.0
        if neu == "ersatz":
            self.ersatz_wechsel += 1
        log.info("Ambiente-Quelle: %s -> %s", self.quelle, neu)
        self.quelle = neu

    def lesen(self, n: int) -> np.ndarray | None:
        """Nächster Block (n, 2) oder None (Stille). Nur aus dem Mischer-Thread aufrufen."""
        if not self.soll:
            self.quelle = "aus"
            return None
        if self._alt is not None:                            # Sprung: altes Stück ausklingen lassen
            alt, gesamt = self._alt, int(SPRUNG_AUS_S * RATE)
            block = alt.lesen(n) if self.quelle in ("stream", "puffert") else self._block(self.quelle, n)
            if block is None or self._alt_pos >= gesamt:
                self._alt = None
                self._ersatz_stoppen()
                self._von = None
                self.quelle = "puffert"
                self._ein = 0.0
            else:
                g = 1.0 - (self._alt_pos + np.arange(n, dtype=np.float32)[:, None]) / gesamt
                self._alt_pos += n
                return block * np.clip(g, 0, 1)
        block = self._lesen_normal(n)
        if block is not None and self._ein < 1.0:            # nach einem Sprung weich einblenden
            schritt = n / (SPRUNG_EIN_S * RATE)
            g = np.clip(self._ein + np.arange(n, dtype=np.float32)[:, None] * schritt / n, 0, 1)
            self._ein = min(1.0, self._ein + schritt)
            block = block * g
        return block

    def _lesen_normal(self, n: int) -> np.ndarray | None:
        frisch = time.monotonic() - self.letzte_daten < 1.0
        vorrat = self.vorrat.sekunden
        q = self.quelle
        if q == "aus":
            q = self.quelle = "puffert"
        # direkt nach einem Sprung kein Ersatz: der Mitschnitt wäre das alte Stück, lieber kurz still
        if time.monotonic() - self._sprung_zeit < SPRUNG_KEIN_ERSATZ_S:
            frisch = True
        # Ersatz vorbereiten, sobald der Stream stockt (Dekoder braucht einen Moment)
        if not frisch and vorrat < KNAPP_S + 2.0:
            self._ersatz_starten()
        if q == "puffert" and vorrat >= START_S:
            self._wechseln("stream")
        elif q == "puffert" and not frisch and self.ersatz.sekunden > 0.5:
            self._wechseln("ersatz")                         # Server weg (Notbetrieb)
        elif q == "stream" and vorrat < KNAPP_S and not frisch and self.ersatz.sekunden > 0.5:
            self._wechseln("ersatz")
        elif q == "ersatz" and vorrat >= ZURUECK_S:
            self._wechseln("stream")
        elif q == "ersatz" and frisch is False and self._ersatz_proc is None and not self._ersatz_starten():
            self._wechseln("puffert")

        block = self._block(self.quelle, n)
        if block is None:
            if self.quelle == "stream":
                self.unterlaeufe += 1
                log.warning("Ambiente-Unterlauf (Vorrat leer, Nr. %d)", self.unterlaeufe)
                self._wechseln("puffert")
            return None
        if self._von and self._blende < 1.0:
            alt = self._block(self._von, n)
            schritt = n / (BLENDE_S * RATE)
            t = np.clip(self._blende + np.arange(n, dtype=np.float32)[:, None] * schritt / n, 0, 1)
            if alt is not None:
                block = alt * np.cos(t * np.pi / 2) + block * np.sin(t * np.pi / 2)
            self._blende = min(1.0, self._blende + schritt)
            if self._blende >= 1.0:
                if self._von == "ersatz":
                    self._ersatz_stoppen()
                self._von = None
        elif self.quelle == "stream" and self._ersatz_proc is not None and vorrat >= ZURUECK_S:
            self._ersatz_stoppen()
        return block
