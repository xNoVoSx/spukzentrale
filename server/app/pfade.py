"""Speicherorte. Im Container: /data/config (Einstellungen) und /data/medien (Sounds)."""

import os
from pathlib import Path

CONFIG = Path(os.environ.get("SPUK_CONFIG", "/data/config"))
MEDIEN = Path(os.environ.get("SPUK_MEDIEN", "/data/medien"))

MUSIK = MEDIEN / "musik"
FX = MEDIEN / "fx"
TMP = MEDIEN / "tmp"


def anlegen() -> None:
    for p in (CONFIG, MUSIK, FX, TMP):
        p.mkdir(parents=True, exist_ok=True)
