"""Musik: doppelte Stücke erkennen, Stille abschneiden, passende Reihenfolge – und trotzdem kommt jedes Stück dran."""
import subprocess
from collections import Counter

from fastapi.testclient import TestClient

from app import musikabgleich, pfade
from app.main import app
from app.modelle import MusikAnalyse, Musikstueck


def _stueck(pfad, d=30, vorn=0.0, hinten=0.0, laut=1.0, fmt=(), seed=7):
    """Musikähnliche Tonfolge (zwei Stimmen, seed bestimmt die „Melodie“), optional mit Stille vorn/hinten."""
    ton = (f"{laut}*0.3*(sin(2*PI*t*(200+80*mod(floor(t*2)*{seed},13)))"
           f"+sin(2*PI*t*(300+60*mod(floor(t*3)*{seed + 2},17))))*(0.6+0.4*sin(t*{seed % 5 + 1}))")
    filt = f"aevalsrc='{ton}':d={d}:s=22050,adelay={int(vorn * 1000)}:all=1,apad=pad_dur={hinten}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", filt, "-ac", "2", *fmt, str(pfad)], check=True)
    return pfad


def test_gleiches_stueck_wird_erkannt_anderes_nicht(tmp_path):
    a = musikabgleich.abdruck(_stueck(tmp_path / "a.wav"))
    leiser_mp3 = musikabgleich.abdruck(_stueck(tmp_path / "b.mp3", vorn=3, laut=0.4, fmt=("-b:a", "96k")))
    anders = musikabgleich.abdruck(_stueck(tmp_path / "c.wav", seed=8))
    assert musikabgleich.abweichung(a, leiser_mp3) <= musikabgleich.GLEICH_BIS
    assert musikabgleich.abweichung(a, anders) > musikabgleich.GLEICH_BIS + 0.1   # echte Stücke: ≥ 0,47


def test_stille_grenzen():
    text = ("[silencedetect] silence_start: 0\n[silencedetect] silence_end: 2.5 | silence_duration: 2.5\n"
            "[silencedetect] silence_start: 61.2\n[silencedetect] silence_end: 61.9 | silence_duration: 0.7\n"
            "[silencedetect] silence_start: 180.4\n")
    assert musikabgleich.stille_grenzen(text, 190.0) == (2.5, 180.4)       # Pause in der Mitte bleibt
    assert musikabgleich.stille_grenzen("", 100.0) == (0.0, None)


def test_upload_schneidet_stille_und_lehnt_doppelte_ab(tmp_path):
    with TestClient(app) as c:
        z = app.state.zentrale
        q = _stueck(tmp_path / "Grabesruhe.wav", d=20, vorn=4, hinten=6)
        r = c.post("/api/musik", files={"datei": ("Grabesruhe.wav", q.read_bytes(), "audio/wav")})
        assert r.status_code == 200 and 19.5 <= r.json()["dauer_s"] <= 21.5      # 30 s -> ~20,6 s
        mid = r.json()["id"]
        assert (pfade.MUSIK / f"{mid}.abdruck.npy").exists()
        # dieselbe Musik als andere Datei unter anderem Namen
        q2 = _stueck(tmp_path / "x.mp3", d=20, laut=0.5, fmt=("-b:a", "128k"))
        r = c.post("/api/musik", files={"datei": ("track07.mp3", q2.read_bytes(), "audio/mpeg")})
        assert r.status_code == 409 and "Grabesruhe" in r.json()["detail"]
        # gleicher Name, andere Datei
        q3 = _stueck(tmp_path / "y.wav", seed=8, d=20)
        assert c.post("/api/musik", files={"datei": ("grabesruhe.wav", q3.read_bytes(), "audio/wav")}).status_code == 409
        r = c.post("/api/musik", files={"datei": ("Anderes.wav", q3.read_bytes(), "audio/wav")})
        assert r.status_code == 200
        assert len(z.medien.daten.musik) == 2 and len(list(pfade.MUSIK.glob("*.mp3"))) == 2
        for m in list(z.medien.daten.musik):
            z.medien.musik_loeschen(m.id)
        assert not list(pfade.MUSIK.glob("*.abdruck.npy"))


def _m(i, bpm, stimmung):
    return Musikstueck(id=f"m{i}", name=f"M{i}", analyse=MusikAnalyse(bpm=bpm, stimmung=stimmung, pegel=[50] * 60))


def test_reihenfolge_passend_und_alle_kommen_dran():
    stuecke = [_m(0, 80, "duester"), _m(1, 82, "duester"), _m(2, 160, "duester"), _m(3, 140, "dramatisch"),
               _m(4, 141, "dramatisch"), _m(5, 100, "ruhig"), Musikstueck(id="neu", name="ohne Analyse")]
    zaehler, vorher = Counter(), None
    for _ in range(200):
        runde = musikabgleich.reihenfolge(stuecke, vorher)
        assert sorted(runde) == sorted(s.id for s in stuecke)                   # jede Runde jedes Stück genau einmal
        assert vorher is None or runde[0] != vorher.id                          # kein Stück zweimal hintereinander
        zaehler[(runde[0], runde[1])] += 1
        vorher = next(s for s in stuecke if s.id == runde[-1])
    a, b = stuecke[3], stuecke[4]
    assert musikabgleich.abstand(a, b) < musikabgleich.abstand(a, stuecke[5])  # gleiches Tempo/Stimmung passt besser
    assert musikabgleich.abstand(stuecke[0], stuecke[2]) < 0.5 + 0.1            # doppeltes Tempo zählt als passend


def test_loudnorm_messwerte():
    from app.medien import loudnorm_messwerte
    text = ('[Parsed_loudnorm_0 @ 0x1]\n{\n\t"input_i" : "-26.66",\n\t"input_tp" : "-14.67",\n\t"input_lra" : "8.40",\n'
            '\t"input_thresh" : "-37.10",\n\t"output_i" : "-18.0",\n\t"target_offset" : "0.42"\n}\n')
    assert loudnorm_messwerte(text) == {"input_i": "-26.66", "input_tp": "-14.67", "input_lra": "8.40",
                                        "input_thresh": "-37.10", "target_offset": "0.42"}
    assert loudnorm_messwerte(text.replace("-26.66", "-inf")) is None             # still: einstufig wie bisher
    assert loudnorm_messwerte("kaputt") is None
