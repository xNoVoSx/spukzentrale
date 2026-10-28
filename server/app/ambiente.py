"""Ambiente-Endlos-Stream.

Die Playlist wird mit ffmpeg zu PCM dekodiert, Stücke werden ineinander übergeblendet
und das Ergebnis läuft durch einen einzigen, dauerhaft laufenden MP3-Encoder. So gibt es
beim Wiederholen keine Lücken (MP3-Dateien haben am Anfang/Ende Stille) und der Sound-ESP
muss nur einen einzigen Stream abspielen – wie ein Internetradio.

Der Encoder wird in Echtzeit (mit kleinem Vorlauf) gefüttert. Neue Zuhörer bekommen
zuerst die letzten Sekunden als Puffer, damit der ESP sofort loslegen kann. Der Raspberry Pi fragt mit
puffer_s nach mehr (bis RUECKBLICK_MAX_S) und hält damit einen großen Vorrat gegen WLAN-Aussetzer.
"""

import asyncio
import collections
import contextlib
import logging
import math
import random
import time
from collections.abc import AsyncIterator, Callable

import numpy as np

from .medien import MedienVerwaltung

log = logging.getLogger(__name__)

RATE = 44100
KANAELE = 2
STUECK_FRAMES = 4410          # 100 ms pro Verarbeitungsschritt
VORLAUF_S = 1.5               # so weit darf der Encoder der Echtzeit voraus sein
RUECKBLICK_BYTES = 40 * 1024  # ca. 2,5 s MP3 für neue Zuhörer (Standard, passt in den Puffer des Sound-ESP)
MP3_BYTES_S = 128_000 // 8
RUECKBLICK_MAX_S = 20         # so viel hält der Server vor (Zuhörer mit puffer_s, z. B. der Pi)
LEERLAUF_S = 20               # ohne Zuhörer wird der Encoder danach beendet
ZUHOERER_PUFFER = 4096        # Blöcke (meist einzelne MP3-Frames ~400 B) pro Zuhörer, danach wird er getrennt


class Ambiente:
    def __init__(self, medien: MedienVerwaltung, bei_wechsel: Callable[[str | None], None] | None = None) -> None:
        self.medien = medien
        self.bei_wechsel = bei_wechsel
        self.jetzt: str | None = None        # Name des laufenden Stücks
        self.jetzt_id: str | None = None     # dessen ID (wie jetzt: erst, wenn es zu hören ist)
        self._sprung: str | None = None      # dieses Stück als Nächstes, sofort (springen)
        self._zuhoerer: set[asyncio.Queue] = set()
        self._rueckblick: collections.deque[bytes] = collections.deque()
        self._rueckblick_bytes = 0
        self._task: asyncio.Task | None = None
        self._weiter = asyncio.Event()
        self._wunsch: dict[asyncio.Queue, float] = {}     # puffer_s je Zuhörer (Pi)
        self._anzeige: asyncio.TimerHandle | None = None   # verzögerter Titelwechsel

    @property
    def vorlauf_s(self) -> float:
        """So weit läuft der Encoder der Echtzeit voraus: Standard 1,5 s; fragt ein Zuhörer (der Pi) nach mehr
        Vorrat, entsprechend mehr – sonst könnte sein Vorrat nach einem Start nie wachsen."""
        return min(max([VORLAUF_S, *self._wunsch.values()]), RUECKBLICK_MAX_S - 2)

    # ------------------------------------------------------------------
    @property
    def laeuft(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def zuhoerer(self) -> int:
        return len(self._zuhoerer)

    def weiter(self) -> None:
        """Im Encoder zum nächsten Stück überblenden (zu hören erst nach vorlauf_s – für sofort: springen)."""
        self._weiter.set()

    def naechstes_nach(self, mid: str | None) -> str | None:
        """Stück nach mid in der Playlist (bei Zufall ein anderes zufälliges)."""
        pl = self.medien.daten.playlist
        ids = [i for i in pl.reihenfolge if self.medien.musik(i)]
        if not ids:
            return None
        if pl.zufall:
            andere = [i for i in ids if i != mid] or ids
            return random.choice(andere)
        return ids[(ids.index(mid) + 1) % len(ids)] if mid in ids else ids[0]

    async def springen(self, mid: str) -> None:
        """Stück mid sofort spielen: Der Stream beginnt neu mit diesem Stück (Rückblick und Vorlauf mit dem alten
        werden verworfen), danach geht die Playlist ab dort weiter. Zuhörer mit Vorrat (Pi) bekommen das Ende des
        alten Streams und verbinden neu – der Pi verwirft dazu auf „musik sprung“ seinen Vorrat."""
        self._sprung = mid
        if self._anzeige:
            self._anzeige.cancel()
            self._anzeige = None
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = asyncio.create_task(self._sender(), name="ambiente")
        stueck = self.medien.musik(mid)
        self.jetzt_id = mid
        self._setze_jetzt(stueck.name if stueck else None)

    async def stoppen(self) -> None:
        if self._anzeige:
            self._anzeige.cancel()
            self._anzeige = None
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self.jetzt_id = None
        self._setze_jetzt(None)

    async def hoeren(self, puffer_s: float = 0) -> AsyncIterator[bytes]:
        """MP3-Daten für einen Zuhörer (Sound-ESP, Pi oder Browser). puffer_s: so viele Sekunden aus der
        Vergangenheit vorweg (Standard ~2,5 s)."""
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=ZUHOERER_PUFFER)
        gewuenscht = RUECKBLICK_BYTES if puffer_s <= 0 else int(min(puffer_s, RUECKBLICK_MAX_S) * MP3_BYTES_S)
        vorweg: list[bytes] = []
        summe = 0
        for block in reversed(self._rueckblick):
            if summe >= gewuenscht and vorweg:
                break
            vorweg.append(block)
            summe += len(block)
        # Der Encoder liefert einzelne MP3-Frames (~400 Byte) – für die Warteschlange zu großen Stücken bündeln
        daten = b"".join(reversed(vorweg))
        for i in range(0, len(daten), 65536):
            q.put_nowait(daten[i:i + 65536])
        self._zuhoerer.add(q)
        if puffer_s > 0:
            self._wunsch[q] = float(puffer_s)
        if not self.laeuft:
            self._task = asyncio.create_task(self._sender(), name="ambiente")
        try:
            while True:
                block = await q.get()
                if block is None:
                    return
                yield block
        finally:
            self._zuhoerer.discard(q)
            self._wunsch.pop(q, None)

    # ------------------------------------------------------------------
    def _setze_jetzt(self, name: str | None) -> None:
        if name != self.jetzt:
            self.jetzt = name
            if self.bei_wechsel:
                self.bei_wechsel(name)

    def _jetzt_beim_hoeren(self, mid: str, name: str) -> None:
        """Titel erst anzeigen, wenn er zu hören ist (der Encoder läuft bis zu vorlauf_s voraus)."""
        if self._anzeige:
            self._anzeige.cancel()
            self._anzeige = None

        def setzen() -> None:
            self.jetzt_id = mid
            self._setze_jetzt(name)

        if self.vorlauf_s <= 3:
            setzen()
        else:
            self._anzeige = asyncio.get_running_loop().call_later(self.vorlauf_s, setzen)

    def _verteilen(self, block: bytes) -> None:
        self._rueckblick.append(block)
        self._rueckblick_bytes += len(block)
        while self._rueckblick_bytes > RUECKBLICK_MAX_S * MP3_BYTES_S and len(self._rueckblick) > 1:
            self._rueckblick_bytes -= len(self._rueckblick.popleft())
        for q in list(self._zuhoerer):
            try:
                q.put_nowait(block)
            except asyncio.QueueFull:
                log.warning("Ambiente: Zuhörer kommt nicht hinterher – Verbindung wird getrennt")
                self._zuhoerer.discard(q)
                self._wunsch.pop(q, None)
                with contextlib.suppress(asyncio.QueueFull):
                    q.get_nowait()
                    q.put_nowait(None)

    async def _sender(self) -> None:
        enc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "s16le", "-ar", str(RATE), "-ac", str(KANAELE), "-i", "pipe:0",
            "-c:a", "libmp3lame", "-b:a", "128k", "-write_xing", "0", "-f", "mp3", "-flush_packets", "1", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )
        assert enc.stdin and enc.stdout

        async def lesen() -> None:
            while block := await enc.stdout.read(4096):
                self._verteilen(block)

        leser = asyncio.create_task(lesen())
        log.info("Ambiente-Stream gestartet")
        start = time.monotonic()
        gesendet = 0
        leer_seit: float | None = None
        try:
            async for pcm in self._pcm():
                enc.stdin.write(pcm.tobytes())
                await enc.stdin.drain()
                gesendet += len(pcm)
                # Echtzeit halten
                voraus = gesendet / RATE - (time.monotonic() - start)
                vorlauf = self.vorlauf_s
                if voraus > vorlauf:
                    await asyncio.sleep(voraus - vorlauf)
                elif voraus < -2:  # z. B. nach Hänger: Uhr nachziehen statt aufholen
                    start = time.monotonic() - gesendet / RATE
                # ohne Zuhörer irgendwann aufhören
                if self._zuhoerer:
                    leer_seit = None
                elif leer_seit is None:
                    leer_seit = time.monotonic()
                elif time.monotonic() - leer_seit > LEERLAUF_S:
                    break
        finally:
            log.info("Ambiente-Stream beendet")
            leser.cancel()
            with contextlib.suppress(Exception):
                enc.stdin.close()
            with contextlib.suppress(ProcessLookupError):
                enc.kill()
            await enc.wait()
            self._rueckblick.clear()
            self._rueckblick_bytes = 0
            for q in list(self._zuhoerer):
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(None)
            self._setze_jetzt(None)

    # ------------------------------------------------------------------
    def _naechste_runde(self) -> list[str]:
        pl = self.medien.daten.playlist
        ids = [i for i in pl.reihenfolge if self.medien.musik(i)]
        if pl.zufall:
            random.shuffle(ids)
        return ids

    async def _pcm(self) -> AsyncIterator[np.ndarray]:
        """Endloser PCM-Strom (int16, Form (n, 2)) mit Überblendungen."""
        rest: np.ndarray | None = None  # zurückgehaltenes Ende des vorigen Stücks (float32)
        while True:
            ids = self._naechste_runde()
            if not ids:
                self._setze_jetzt(None)
                if rest is not None:  # letztes Stück sauber ausklingen lassen
                    yield _ausblenden(rest).astype(np.int16)
                    rest = None
                yield np.zeros((STUECK_FRAMES, KANAELE), np.int16)
                continue
            i = 0
            while i < len(ids):
                if self._sprung:
                    mid, self._sprung = self._sprung, None
                    if mid in ids and not self.medien.daten.playlist.zufall:
                        i = ids.index(mid) + 1          # danach in der Reihenfolge weiter
                else:
                    mid = ids[i]
                    i += 1
                stueck = self.medien.musik(mid)
                if not stueck:
                    continue
                self._jetzt_beim_hoeren(mid, stueck.name)
                async for block, rest in self._stueck(mid, rest):
                    yield block
                    if self._sprung:                      # (Sicherheit; springen startet den Stream ohnehin neu)
                        break

    async def _stueck(self, mid: str, rest: np.ndarray | None):
        """Dekodiert ein Stück. Liefert (Block, neuer Rest) – der Rest (die letzten
        Überblend-Sekunden) wird erst mit dem Anfang des nächsten Stücks gemischt."""
        ueber = int(self.medien.daten.playlist.ueberblend_s * RATE)
        pfad = self.medien.musik_pfad(mid)
        dec = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(pfad),
            "-f", "s16le", "-ar", str(RATE), "-ac", str(KANAELE), "pipe:1",
            stdout=asyncio.subprocess.PIPE,
        )
        assert dec.stdout
        self._weiter.clear()
        gehalten = np.zeros((0, KANAELE), np.float32)
        bytes_pro_stueck = STUECK_FRAMES * KANAELE * 2
        try:
            while True:
                daten = await dec.stdout.read(bytes_pro_stueck)
                if not daten:
                    break
                daten = daten[: len(daten) - len(daten) % (KANAELE * 2)]
                neu = np.frombuffer(daten, np.int16).reshape(-1, KANAELE).astype(np.float32)
                gehalten = np.concatenate([gehalten, neu])
                if rest is not None and len(gehalten) >= len(rest):
                    gehalten[: len(rest)] = _ueberblenden(rest, gehalten[: len(rest)])
                    rest = None
                if rest is None and len(gehalten) > ueber:
                    raus = len(gehalten) - ueber
                    yield _int16(gehalten[:raus]), None
                    gehalten = gehalten[raus:]
                if self._weiter.is_set():
                    self._weiter.clear()
                    if rest is not None:  # Stück war noch in der Einblendung
                        gehalten = _ueberblenden(rest[: len(gehalten)], gehalten) if len(gehalten) else rest
                        rest = None
                    break
        finally:
            with contextlib.suppress(ProcessLookupError):
                dec.kill()
            await dec.wait()
        if rest is not None:  # Stück kürzer als die Überblendung
            n = min(len(rest), len(gehalten))
            gemischt = _ueberblenden(rest[:n], gehalten[:n]) if n else np.zeros((0, KANAELE), np.float32)
            gehalten = np.concatenate([gemischt, gehalten[n:]])
            if len(gehalten):
                yield _int16(gehalten), None
            gehalten = np.zeros((0, KANAELE), np.float32)
        if ueber == 0:
            if len(gehalten):
                yield _int16(gehalten), None
            yield np.zeros((0, KANAELE), np.int16), None
            return
        # Ende zurückhalten -> wird mit dem nächsten Stück gemischt
        yield np.zeros((0, KANAELE), np.int16), gehalten if len(gehalten) else None


def _int16(x: np.ndarray) -> np.ndarray:
    return np.clip(x, -32768, 32767).astype(np.int16)


def _ueberblenden(aus: np.ndarray, ein: np.ndarray) -> np.ndarray:
    """Gleich-Leistungs-Überblendung (keine Lautstärkedelle in der Mitte)."""
    n = len(aus)
    if n == 0:
        return ein
    t = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
    return aus * np.cos(t * math.pi / 2) + ein * np.sin(t * math.pi / 2)


def _ausblenden(x: np.ndarray) -> np.ndarray:
    t = np.linspace(1.0, 0.0, len(x), dtype=np.float32)[:, None]
    return np.clip(x * t, -32768, 32767)
