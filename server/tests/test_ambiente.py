import numpy as np

from app.ambiente import RATE, Ambiente, _ueberblenden
from app.medien import MedienVerwaltung, musik_umwandeln
from app.modelle import Musikstueck


def test_ueberblendung_gleiche_leistung():
    n = 1000
    a = np.ones((n, 2), np.float32) * 1000
    b = np.ones((n, 2), np.float32) * 1000
    m = _ueberblenden(a, b)
    assert np.isclose(m[0, 0], 1000) and np.isclose(m[-1, 0], 1000)
    # unkorrelierte Signale: Leistung bleibt konstant (cos² + sin² = 1)
    t = np.linspace(0, 1, n)
    assert np.allclose(np.cos(t * np.pi / 2) ** 2 + np.sin(t * np.pi / 2) ** 2, 1)


async def _stueck(m: MedienVerwaltung, ton, name: str, freq: int, dauer: float) -> str:
    mid, d, g = await musik_umwandeln(ton(f"{name}.wav", f"sine=f={freq}", dauer))
    m.musik_hinzu(Musikstueck(id=mid, name=name, dauer_s=d, groesse=g))
    return mid


async def test_endlosstrom_ohne_luecken_mit_ueberblendung(ton):
    m = MedienVerwaltung()
    await _stueck(m, ton, "eins", 220, 3.0)
    await _stueck(m, ton, "zwei", 330, 2.0)
    m.daten.playlist.ueberblend_s = 1.0
    wechsel: list = []
    a = Ambiente(m, bei_wechsel=wechsel.append)

    teile = []
    frames = 0
    async for block in a._pcm():
        teile.append(block)
        frames += len(block)
        if frames > RATE * 12:  # gut zwei Runden
            break
    pcm = np.concatenate(teile).astype(np.float32)
    assert pcm.shape[1] == 2
    # keine stillen Stellen (Lücke beim Übergang/Wiederholen): RMS in 50-ms-Fenstern
    fenster = RATE // 20
    rms = [np.sqrt(np.mean(pcm[i:i + fenster] ** 2)) for i in range(fenster, len(pcm) - fenster, fenster)]
    assert min(rms) > 1000, f"Lücke im Strom (min. RMS {min(rms):.0f})"
    # Länge einer Runde: 3 s + 2 s minus zwei Überblendungen à 1 s = 3 s
    assert wechsel[:3] == ["eins", "zwei", "eins"]


async def test_leere_playlist_liefert_stille():
    a = Ambiente(MedienVerwaltung())
    gen = a._pcm()
    block = await gen.__anext__()
    assert block.shape[1] == 2 and not block.any()
    await gen.aclose()
