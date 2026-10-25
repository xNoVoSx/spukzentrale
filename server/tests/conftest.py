"""Gemeinsame Test-Hilfen: jeder Test bekommt eigene, leere Datenordner."""

import subprocess
from pathlib import Path

import pytest

from app import pfade


@pytest.fixture(autouse=True)
def datenordner(tmp_path, monkeypatch):
    monkeypatch.setattr(pfade, "CONFIG", tmp_path / "config")
    monkeypatch.setattr(pfade, "MEDIEN", tmp_path / "medien")
    monkeypatch.setattr(pfade, "MUSIK", tmp_path / "medien" / "musik")
    monkeypatch.setattr(pfade, "FX", tmp_path / "medien" / "fx")
    monkeypatch.setattr(pfade, "TMP", tmp_path / "medien" / "tmp")
    pfade.anlegen()
    return tmp_path


def erzeuge_ton(ziel: Path, quelle: str, dauer: float, *zusatz: str) -> Path:
    """Testton mit ffmpeg erzeugen, z. B. quelle="sine=f=440"."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"{quelle}:d={dauer}", *zusatz, str(ziel)],
        check=True,
    )
    return ziel


@pytest.fixture
def ton(tmp_path):
    def machen(name: str, quelle: str = "sine=f=440", dauer: float = 2.0, *zusatz: str) -> Path:
        return erzeuge_ton(tmp_path / name, quelle, dauer, *zusatz)

    return machen
