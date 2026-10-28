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


async def test_rueckblick_puffer_s_fuer_den_pi():
    """Standard-Zuhörer (ESP) bekommen ~2,5 s vorweg, der Pi mit puffer_s deutlich mehr."""
    import asyncio

    from app import ambiente as amb

    a = Ambiente(MedienVerwaltung())
    for _ in range(1000):                      # wie der Encoder: einzelne MP3-Frames, 1000 × 418 B ≈ 26 s
        a._verteilen(bytes(418))
    assert a._rueckblick_bytes <= amb.RUECKBLICK_MAX_S * amb.MP3_BYTES_S
    a._task = asyncio.get_running_loop().create_future()   # „Encoder läuft“ vortäuschen

    async def vorweg(**kw) -> int:
        n = 0
        gen = a.hoeren(**kw)
        n += len(await gen.__anext__())
        for q in a._zuhoerer:                  # alles Vorweg liegt schon in der Warteschlange
            q.put_nowait(None)
        async for block in gen:
            n += len(block)
        return n

    standard = await vorweg()
    pi = await vorweg(puffer_s=15)
    assert amb.RUECKBLICK_BYTES <= standard < amb.RUECKBLICK_BYTES + 418
    assert 15 * amb.MP3_BYTES_S <= pi < 15 * amb.MP3_BYTES_S + 418


def test_vorlauf_folgt_dem_puffer_wunsch():
    import asyncio

    from app import ambiente as amb

    a = Ambiente(MedienVerwaltung())
    assert a.vorlauf_s == amb.VORLAUF_S
    q: asyncio.Queue = asyncio.Queue()
    a._wunsch[q] = 15
    assert a.vorlauf_s == 15
    a._wunsch[q] = 60                       # gedeckelt: der Rückblick muss den Vorlauf noch fassen
    assert a.vorlauf_s == amb.RUECKBLICK_MAX_S - 2


async def test_springen_startet_mit_dem_stueck_und_geht_danach_weiter(ton):
    m = MedienVerwaltung()
    eins = await _stueck(m, ton, "eins", 220, 1.0)
    zwei = await _stueck(m, ton, "zwei", 330, 1.0)
    drei = await _stueck(m, ton, "drei", 440, 1.0)
    m.daten.playlist.ueberblend_s = 0.0
    wechsel: list = []
    a = Ambiente(m, bei_wechsel=wechsel.append)
    await a.springen(zwei)                      # Stream läuft noch nicht: Wunsch gilt für den Start
    assert a.jetzt == "zwei" and a.jetzt_id == zwei
    frames = 0
    async for block in a._pcm():
        frames += len(block)
        if frames > RATE * 3.5:
            break
    # beim Hörbarwerden meldet das Stück sich noch einmal (Fortschrittsanzeige) – Reihenfolge ohne Wiederholungen
    folge = [w for i, w in enumerate(wechsel) if i == 0 or w != wechsel[i - 1]]
    assert folge[:3] == ["zwei", "drei", "eins"]
    assert a.naechstes_nach(drei) == eins and a.naechstes_nach(None) == eins
