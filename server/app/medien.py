"""Musik und Effekt-Sounds: Hochladen, mit ffmpeg umwandeln, verwalten.

Musik (Ambiente): MP3 44,1 kHz Stereo 128 kbit/s CBR, lautheitsnormalisiert.
Effekte: IMA-ADPCM-WAV 22,05 kHz mono (für den Flash des Sound-ESP), Stille am Anfang
entfernt (damit der Blitz auf dem Knall sitzt), auf -1 dBFS Spitze normalisiert.
Dazu je eine kleine MP3 zum Vorhören im Browser.
"""

import asyncio
import hashlib
import logging
import re
import shutil
import uuid
from pathlib import Path

from . import fxanalyse
from . import pfade
from .modelle import Effekt, Medien, Musikstueck
from .speicher import lade, speichere

log = logging.getLogger(__name__)

FX_MAX_S = 60
FX_RATE = 22050
FX_BLOCK = 512
FX_KAPAZITAET_STANDARD_KB = 0     # noch nie ein Sound-Gerät gemeldet: nicht begrenzen (das Gerät prüft beim Abgleich)
FX_RESERVE_KB = 64                # Verwaltungsdaten von LittleFS
ID_MUSTER = re.compile(r"^[0-9a-f]{12}$")


class KonvertierFehler(Exception):
    pass


async def _ffmpeg(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-nostdin", "-y", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    text = err.decode(errors="replace")
    if proc.returncode != 0:
        letzte = " | ".join(text.strip().splitlines()[-3:])
        raise KonvertierFehler(f"ffmpeg: {letzte}")
    return text


async def dauer_s(pfad: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(pfad),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


def _hash(pfad: Path) -> str:
    h = hashlib.sha1()
    with open(pfad, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()[:12]


async def musik_umwandeln(quelle: Path) -> tuple[str, float, int]:
    """Gibt (id, dauer_s, groesse) zurück. Ergebnis liegt in pfade.MUSIK/<id>.mp3."""
    tmp = pfade.TMP / f"{uuid.uuid4().hex}.mp3"
    try:
        await _ffmpeg(
            "-i", str(quelle), "-vn", "-map_metadata", "-1",
            "-af", "loudnorm=I=-18:TP=-1.5:LRA=11",
            "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", str(tmp),
        )
        d = await dauer_s(tmp)
        if d < 1:
            raise KonvertierFehler("Datei enthält keinen verwertbaren Ton")
        mid = _hash(tmp)
        ziel = pfade.MUSIK / f"{mid}.mp3"
        shutil.move(tmp, ziel)
        return mid, d, ziel.stat().st_size
    finally:
        tmp.unlink(missing_ok=True)


async def fx_umwandeln(quelle: Path, ordner: Path | None = None) -> tuple[str, int, int]:
    """Gibt (id, dauer_ms, groesse) zurück. Ergebnis: <ordner>/<id>.wav (+ <id>.mp3 zum Vorhören), Standard pfade.FX."""
    ordner = ordner or pfade.FX
    basis = pfade.TMP / uuid.uuid4().hex
    pcm, wav, mp3 = basis.with_suffix(".pcm.wav"), basis.with_suffix(".wav"), basis.with_suffix(".mp3")
    try:
        await _ffmpeg(
            "-i", str(quelle), "-vn", "-map_metadata", "-1", "-t", str(FX_MAX_S),
            "-af", f"silenceremove=start_periods=1:start_threshold=-50dB,aresample={FX_RATE}",
            "-ac", "1", "-ar", str(FX_RATE), "-c:a", "pcm_s16le", str(pcm),
        )
        analyse = await _ffmpeg("-i", str(pcm), "-af", "volumedetect", "-f", "null", "-")
        m = re.search(r"max_volume:\s*(-?[\d.]+) dB", analyse)
        if not m or float(m.group(1)) < -70:
            raise KonvertierFehler("Datei ist (fast) still")
        verstaerkung = -1.0 - float(m.group(1))
        # Stille am Ende: dieselbe Messung wie die Effekt-Analyse (nach der Verstärkung), 0,3 s Nachhall bleiben
        ende_s = await asyncio.get_running_loop().run_in_executor(None, fxanalyse.hoerbar_s, pcm, verstaerkung)
        filt = f"volume={verstaerkung:.2f}dB" + (f",atrim=end={ende_s + 0.3:.3f}" if ende_s else "")
        await _ffmpeg("-i", str(pcm), "-af", filt, "-c:a", "adpcm_ima_wav", "-block_size", str(FX_BLOCK), str(wav))
        await _ffmpeg("-i", str(pcm), "-af", filt, "-c:a", "libmp3lame", "-b:a", "64k", str(mp3))
        d = await dauer_s(wav)
        if d < 0.05:
            raise KonvertierFehler("Sound ist zu kurz")
        fid = _hash(wav)
        shutil.move(wav, ordner / f"{fid}.wav")
        shutil.move(mp3, ordner / f"{fid}.mp3")
        return fid, int(d * 1000), (ordner / f"{fid}.wav").stat().st_size
    finally:
        for p in (pcm, wav, mp3):
            p.unlink(missing_ok=True)


async def test_klick_erzeugen() -> Path:
    """Kurzer Klick für den Ton-Licht-Abgleich."""
    ziel = pfade.TMP / "test-klick.wav"
    await _ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=1500:duration=0.06",
        "-af", "afade=t=out:st=0.01:d=0.05", "-ac", "1", "-ar", str(FX_RATE), str(ziel),
    )
    return ziel


class MedienVerwaltung:
    def __init__(self) -> None:
        self.datei = pfade.CONFIG / "medien.json"
        self.daten: Medien = lade(self.datei, Medien)
        self._aufraeumen()

    def speichern(self) -> None:
        speichere(self.datei, self.daten)

    def _aufraeumen(self) -> None:
        """Einträge ohne Datei entfernen (z. B. wenn der Medienordner gewechselt wurde)."""
        vorher = (len(self.daten.musik), len(self.daten.fx))
        self.daten.musik = [m for m in self.daten.musik if self.musik_pfad(m.id).exists()]
        self.daten.fx = [f for f in self.daten.fx if self.fx_pfad(f.id).exists()]
        ids = {m.id for m in self.daten.musik}
        self.daten.playlist.reihenfolge = [i for i in self.daten.playlist.reihenfolge if i in ids]
        if vorher != (len(self.daten.musik), len(self.daten.fx)):
            log.warning("Medien ohne Datei entfernt: vorher %s Musik/%s Effekte", *vorher)
            self.speichern()

    # --- Pfade ---
    @staticmethod
    def musik_pfad(mid: str) -> Path:
        return pfade.MUSIK / f"{mid}.mp3"

    @staticmethod
    def fx_pfad(fid: str) -> Path:
        return pfade.FX / f"{fid}.wav"

    @staticmethod
    def fx_vorhoer_pfad(fid: str) -> Path:
        return pfade.FX / f"{fid}.mp3"

    # --- Musik ---
    def musik(self, mid: str) -> Musikstueck | None:
        return next((m for m in self.daten.musik if m.id == mid), None)

    def musik_hinzu(self, stueck: Musikstueck) -> Musikstueck:
        vorhanden = self.musik(stueck.id)
        if vorhanden:
            return vorhanden
        self.daten.musik.append(stueck)
        self.daten.playlist.reihenfolge.append(stueck.id)
        self.speichern()
        return stueck

    def musik_loeschen(self, mid: str) -> bool:
        if not self.musik(mid):
            return False
        self.daten.musik = [m for m in self.daten.musik if m.id != mid]
        self.daten.playlist.reihenfolge = [i for i in self.daten.playlist.reihenfolge if i != mid]
        self.musik_pfad(mid).unlink(missing_ok=True)
        self.speichern()
        return True

    # --- Effekte ---
    def fx(self, fid: str) -> Effekt | None:
        return next((f for f in self.daten.fx if f.id == fid), None)

    def fx_der_kategorie(self, kategorie: str) -> list[Effekt]:
        return [f for f in self.daten.fx if f.kategorie == kategorie]

    def fx_belegt_kb(self) -> int:
        # LittleFS belegt ganze 4-KB-Blöcke
        return sum(((f.groesse + 4095) // 4096) * 4 for f in self.daten.fx)

    def fx_hinzu(self, effekt: Effekt) -> Effekt:
        vorhanden = self.fx(effekt.id)
        if vorhanden:
            return vorhanden
        self.daten.fx.append(effekt)
        self.speichern()
        return effekt

    def fx_loeschen(self, fid: str) -> bool:
        if not self.fx(fid):
            return False
        self.daten.fx = [f for f in self.daten.fx if f.id != fid]
        self.fx_pfad(fid).unlink(missing_ok=True)
        self.fx_vorhoer_pfad(fid).unlink(missing_ok=True)
        self.speichern()
        return True

    def fx_sync_liste(self) -> list[dict]:
        return [{"id": f.id, "groesse": f.groesse} for f in self.daten.fx]
