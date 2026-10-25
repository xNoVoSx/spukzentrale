"""JSON-Dateien atomar lesen und schreiben (erst Temp-Datei, dann umbenennen)."""

import json
import logging
import os
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)


def lade(pfad: Path, modell: type[M]) -> M:
    """Lädt ein Modell. Fehlt die Datei oder ist sie kaputt, gibt es Standardwerte
    (die kaputte Datei wird als .kaputt aufgehoben)."""
    if not pfad.exists():
        return modell()
    try:
        return modell.model_validate_json(pfad.read_text(encoding="utf-8"))
    except (ValidationError, ValueError) as e:
        sicherung = pfad.with_suffix(pfad.suffix + ".kaputt")
        log.error("%s ist ungültig (%s) – Standardwerte, alte Datei liegt in %s", pfad.name, e, sicherung.name)
        pfad.replace(sicherung)
        return modell()


def speichere(pfad: Path, modell: BaseModel) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    tmp = pfad.with_suffix(pfad.suffix + ".tmp")
    daten = json.dumps(modell.model_dump(mode="json"), ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(daten)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(pfad)
