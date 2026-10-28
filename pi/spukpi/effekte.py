"""Effekt-Sounds auf der SD-Karte: Abgleich mit dem Server und fertig dekodierte Klänge für den Mischer.

Der Server liefert die Effekte als IMA-ADPCM-WAV (22 kHz mono, fürs ESP-Flash gemacht). Der Pi lädt sie einmal,
dekodiert sie mit ffmpeg auf 44,1 kHz float32 und legt sie als .npy daneben. Der Mischer liest sie per mmap –
das Betriebssystem hält sie im Cache, Platz gibt es auf der SD-Karte reichlich.

Außerdem merkt sich der Pi zu jedem Effekt die letzte Licht-Aktion und Lautstärke (meta.json) – damit kann er
im Notbetrieb ohne Server selbst Effekte mit passendem Licht auslösen.
"""

import json
import logging
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

RATE = 44100


class Effekte:
    def __init__(self, ordner: Path) -> None:
        self.ordner = ordner
        self.ordner.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, np.ndarray] = {}
        self._sperre = threading.Lock()
        self.meta_datei = ordner / "meta.json"
        try:
            self.meta: dict[str, dict] = json.loads(self.meta_datei.read_text())
        except (OSError, ValueError):
            self.meta = {}
        self.sync_laeuft = False

    # ------------------------------------------------------------------
    def _npy(self, fid: str) -> Path:
        return self.ordner / f"{fid}.npy"

    def hat(self, fid: str) -> bool:
        return bool(fid) and "/" not in fid and self._npy(fid).exists()

    def ids(self) -> list[str]:
        return sorted(p.stem for p in self.ordner.glob("*.npy"))

    def klang(self, fid: str) -> np.ndarray | None:
        """Mono float32 bei 44,1 kHz (mmap)."""
        with self._sperre:
            k = self._cache.get(fid)
            if k is None and self.hat(fid):
                k = np.load(self._npy(fid), mmap_mode="r")
                self._cache[fid] = k
            return k

    def merken(self, fid: str, vol: int, licht: dict | None) -> None:
        neu = {"vol": vol, "licht": licht}
        if self.meta.get(fid) != neu:
            self.meta[fid] = neu
            try:
                self.meta_datei.write_text(json.dumps(self.meta))
            except OSError as e:
                log.warning("meta.json: %s", e)

    def frei_kb(self) -> int:
        return shutil.disk_usage(self.ordner).free // 1024

    def gesamt_kb(self) -> int:
        return shutil.disk_usage(self.ordner).total // 1024

    # ------------------------------------------------------------------
    def abgleichen(self, basis_url: str, liste: list[dict]) -> dict:
        """Blockiert (im Executor aufrufen). Ergebnis wie fx_sync_fertig des Sound-ESP."""
        self.sync_laeuft = True
        geladen = geloescht = fehler = 0
        meldung = ""
        try:
            soll = {str(e.get("id", "")): int(e.get("groesse", 0)) for e in liste if e.get("id")}
            for p in list(self.ordner.glob("*.npy")) + list(self.ordner.glob("*.wav")):
                fid = p.stem
                wav = self.ordner / f"{fid}.wav"
                if fid not in soll or (wav.exists() and wav.stat().st_size != soll[fid]):
                    with self._sperre:
                        self._cache.pop(fid, None)
                    p.unlink(missing_ok=True)
                    if p.suffix == ".npy":
                        geloescht += 1
            for fid, groesse in soll.items():
                if self.hat(fid) or "/" in fid:
                    continue
                try:
                    self._laden(f"{basis_url}{fid}", fid, groesse)
                    geladen += 1
                except Exception as e:
                    fehler += 1
                    meldung = meldung or f"{fid}: {e}"
                    log.warning("Effekt %s: %s", fid, e)
        finally:
            self.sync_laeuft = False
        return {"geladen": geladen, "geloescht": geloescht, "fehler": fehler, "meldung": meldung}

    def _laden(self, url: str, fid: str, groesse: int) -> None:
        wav = self.ordner / f"{fid}.wav"
        tmp = self.ordner / f"{fid}.wav.tmp"
        with urllib.request.urlopen(url, timeout=15) as antwort, open(tmp, "wb") as f:
            shutil.copyfileobj(antwort, f)
        if groesse and tmp.stat().st_size != groesse:
            tmp.unlink(missing_ok=True)
            raise OSError(f"nur {tmp.stat().st_size if tmp.exists() else 0} von {groesse} Byte")
        tmp.rename(wav)
        roh = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(wav),
             "-f", "f32le", "-ar", str(RATE), "-ac", "1", "pipe:1"],
            capture_output=True, check=True, timeout=60,
        ).stdout
        daten = np.frombuffer(roh, np.float32)
        np.save(self.ordner / f"{fid}.npy.tmp.npy", daten)
        (self.ordner / f"{fid}.npy.tmp.npy").rename(self._npy(fid))
