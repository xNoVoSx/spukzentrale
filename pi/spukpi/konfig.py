"""Einstellungen aus /etc/spukpi/config.toml (Pfad über SPUKPI_KONFIG änderbar)."""

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

STANDARD_PFAD = "/etc/spukpi/config.toml"


@dataclass
class Konfig:
    server_host: str = ""
    server_port: int = 8095
    # Rollen am Server: „sound“ = der Pi spielt den Ton, „licht“ = USB-Brücke zum Licht-ESP.
    # Aus: der Pi meldet sich nicht (z. B. solange noch der Sound-ESP im Einsatz ist).
    sound: bool = False
    licht: bool = False
    audio_geraet: str = "plughw:CARD=sndrpihifiberry,DEV=0"
    usb_port: str = "/dev/ttyUSB0"
    daten: str = "/var/lib/spukpi"
    puffer_s: float = 15.0        # so viel Ambiente hält der Pi vor (Server liefert es beim Verbinden auf einmal)
    notbetrieb: bool = True       # Server weg: Ambiente aus dem Mitschnitt + Zufallseffekte weiter
    analyse: bool = True          # Spektrum/Beat für die Licht-Effekte „spektrum“ und „beat“

    @property
    def basis_url(self) -> str:
        return f"http://{self.server_host}:{self.server_port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.server_host}:{self.server_port}"


def laden(pfad: str | None = None) -> Konfig:
    pfad = pfad or os.environ.get("SPUKPI_KONFIG", STANDARD_PFAD)
    k = Konfig()
    p = Path(pfad)
    if p.exists():
        daten = tomllib.loads(p.read_text())
        bekannt = {f.name: f.type for f in fields(Konfig)}
        for name, wert in daten.items():
            if name in bekannt:
                setattr(k, name, wert)
    Path(k.daten).mkdir(parents=True, exist_ok=True)
    return k
