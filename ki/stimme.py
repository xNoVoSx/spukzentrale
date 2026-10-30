"""Sprach-Dienst für die KI-Stimme: Text -> Gruselstimme (WAV), läuft auf einem PC mit NVIDIA-GPU.

Chatterbox (mehrsprachig, Resemble AI) spricht den Text, ffmpeg legt ggf. Effekte drüber (Tonhöhe, Hall).
Das Modell bleibt geladen, eine Anfrage dauert je nach Satzlänge 1–4 s.

  POST /sprechen  {"text": "...", "stimme": "alter_mann"} ->  audio/wav (44,1 kHz mono), Kopf X-Stimme
  GET  /stimmen                                          ->  {"stimmen": [...]}
  GET  /gesund                                           ->  {"ok": true}

Stimmen: STIMMEN unten. Eine Stimme mit Vorlage klont die WAV-Datei <vorlagen>/<name>.wav (10–15 s Sprache,
ohne Musik). Ordner per Umgebungsvariable SPUK_VORLAGEN (Standard: ./vorlagen).
Fehlt eine Stimme oder scheitert sie, spricht die ERSATZ-Stimme (die Show soll nie stumm bleiben).
"""

import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

log = logging.getLogger("stimme")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

VORLAGEN = Path(os.environ.get("SPUK_VORLAGEN", Path(__file__).parent / "vorlagen"))

# name: (Vorlage oder None = Standardstimme, exaggeration, cfg_weight, ffmpeg-Effekte)
# cfg_weight 0: laut Chatterbox-Doku gegen den Akzent einer fremdsprachigen (hier englischen) Vorlage
STIMMEN: dict[str, tuple[str | None, float, float, str]] = {
    "alter_mann": ("alter_mann", 0.8, 0.0, ""),       # Vorlage: freesound sabotovat/414496 (CC BY 3.0)
    "maedchen": ("maedchen", 0.6, 0.4, "aecho=0.8:0.6:50|110:0.25|0.15"),
    "daemon": (None, 0.7, 0.3,
               "rubberband=pitch=0.7:formant=shifted,aecho=0.8:0.7:70|150|240:0.3|0.2|0.12,bass=g=4"),
}
ERSATZ = "maedchen"
ENDE = "highpass=f=70,loudnorm=I=-16:TP=-1.5,apad=pad_dur=0.3"

app = FastAPI(title="Spukzentrale – Stimme")
_modell = None
_conds: dict[str, object] = {}
_sperre = threading.Lock()  # die GPU spricht immer nur einen Satz


class Anfrage(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    stimme: str = "alter_mann"


def _laden() -> None:
    global _modell
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    t0 = time.monotonic()
    _modell = ChatterboxMultilingualTTS.from_pretrained(device="cuda" if torch.cuda.is_available() else "cpu")
    _conds[""] = _modell.conds  # eingebaute Standardstimme
    for name, (vorlage, ex, _, _) in STIMMEN.items():
        if vorlage:
            pfad = VORLAGEN / f"{vorlage}.wav"
            if not pfad.exists():
                log.warning("Stimme %s: Vorlage %s fehlt – wird übersprungen", name, pfad)
                continue
            _modell.prepare_conditionals(str(pfad), exaggeration=ex)
            _conds[vorlage] = _modell.conds
    log.info("Chatterbox geladen in %.1f s, Stimmen: %s", time.monotonic() - t0, ", ".join(verfuegbar()))


def verfuegbar() -> list[str]:
    return [n for n, (v, *_) in STIMMEN.items() if (v or "") in _conds]


@app.on_event("startup")
def _start() -> None:
    _laden()
    with _sperre:  # einmal aufwärmen, damit die erste echte Anfrage nicht langsamer ist
        _sprechen("Hallo.", verfuegbar()[0])


def _sprechen(text: str, stimme: str) -> bytes:
    vorlage, ex, cfg, fx = STIMMEN[stimme]
    _modell.conds = _conds[vorlage or ""]
    wav = _modell.generate(text, language_id="de", exaggeration=ex, cfg_weight=cfg)
    roh = wav.squeeze().cpu().numpy().astype(np.float32)
    with tempfile.NamedTemporaryFile(suffix=".f32") as f:
        f.write(roh.tobytes())
        f.flush()
        return subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-f", "f32le", "-ar", str(_modell.sr), "-ac", "1", "-i", f.name,
             "-af", f"{fx},{ENDE}" if fx else ENDE, "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"],
            capture_output=True, check=True, timeout=30,
        ).stdout


@app.get("/gesund")
def gesund() -> dict:
    return {"ok": _modell is not None, "stimmen": verfuegbar()}


@app.get("/stimmen")
def stimmen() -> dict:
    return {"stimmen": verfuegbar()}


@app.post("/sprechen")
def sprechen(a: Anfrage) -> Response:
    da = verfuegbar()
    stimme = a.stimme
    if stimme not in da:
        if ERSATZ not in da:
            raise HTTPException(404, f"Stimme {a.stimme!r} gibt es nicht (da: {', '.join(da)})")
        log.warning("Stimme %s fehlt – spreche mit %s", a.stimme, ERSATZ)
        stimme = ERSATZ
    t0 = time.monotonic()
    with _sperre:
        try:
            daten = _sprechen(a.text.strip(), stimme)
        except Exception:
            if stimme == ERSATZ or ERSATZ not in da:
                raise
            log.exception("Stimme %s gescheitert – spreche mit %s", stimme, ERSATZ)
            stimme = ERSATZ
            daten = _sprechen(a.text.strip(), stimme)
    log.info("%s (%.1f s): %s", stimme, time.monotonic() - t0, a.text)
    return Response(daten, media_type="audio/wav", headers={"X-Stimme": stimme})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("SPUK_HOST", "0.0.0.0"), port=int(os.environ.get("SPUK_PORT", "8092")))
