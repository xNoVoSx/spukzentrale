"""Effekt-Analyse: hörbare Länge, Verlauf, Kategorie-Prüfung, Stille am Ende abschneiden."""
import asyncio
import subprocess

from fastapi.testclient import TestClient

from app import fxanalyse, pfade
from app.main import app
from app.medien import fx_umwandeln
from app.modelle import Effekt, Schritt, Szene


def _ton(pfad, filter_):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", filter_, "-ac", "1", "-ar", "22050",
                    str(pfad)], check=True)


def test_verlauf_und_hoerbare_laenge(tmp_path):
    knall = tmp_path / "knall.wav"        # 0,5 s laut, dann 3 s Stille
    _ton(knall, "sine=f=200:d=0.5,apad=pad_dur=3")
    a = fxanalyse.analysieren(knall, "knall", "donner")
    assert 400 <= a.hoerbar_ms <= 650 and "schlagartig" in a.verlauf
    anschwellen = tmp_path / "an.wav"
    _ton(anschwellen, "sine=f=300:d=4,afade=t=in:d=3.6")
    a = fxanalyse.analysieren(anschwellen, "an", "wind")
    assert a.verlauf == "schwillt langsam an" and a.spitze_s > 2.5
    stoesse = tmp_path / "klopf.wav"
    _ton(stoesse, "sine=f=150:d=4,volume='if(lt(mod(t,1),0.2),1,0.001)':eval=frame")
    assert fxanalyse.analysieren(stoesse, "klopf", "klopfen").verlauf.startswith("in ")


def test_kategorie_vorschlag():
    v = fxanalyse.kategorie_vorschlag
    assert v("dragon-studio-zombie-sound-2-357976", "spieluhr", ["Snoring", "Groan"]) == "zombie"
    assert v("xomxomski-dark-church-bells-423028", "donner", ["Gong"]) == "glocke"
    assert v("dragon-studio-zombie-sfx-450450", "spieluhr", ["Burping, eructation", "Grunt"]) == "zombie"
    assert v("samc44-witch-laugh-189108", "lachen", ["Wind"]) is None              # Dateiname passt: kein Hinweis
    assert v("lazychillzone-wolves-howling-1", "werwolf", ["Howl"]) is None
    assert v("aufnahme-17", "donner", ["Thunder"]) is None
    assert v("aufnahme-17", "wind", ["Crow", "Bird"]) == "kraehe"                  # nur der Klang zeigt woandershin


def test_stille_am_ende_wird_beim_hochladen_abgeschnitten(tmp_path, monkeypatch):
    monkeypatch.setattr(pfade, "FX", tmp_path)
    monkeypatch.setattr(pfade, "TMP", tmp_path)
    q = tmp_path / "schrei.wav"
    _ton(q, "anullsrc=r=22050:cl=mono:d=0.5[a];sine=f=500:d=1.5[b];[a][b]concat=n=2:v=0:a=1,apad=pad_dur=5")
    fid, dauer_ms, _ = asyncio.run(fx_umwandeln(q))
    assert 1500 <= dauer_ms <= 2000                     # 0,5 s vorn und 5 s hinten weg, 0,3 s Nachhall bleibt


def test_nachbearbeiten_kuerzt_alte_sounds_und_passt_szenen_an(monkeypatch):
    with TestClient(app) as c:
        z = app.state.zentrale
        alt = pfade.TMP / "alt.wav"
        _ton(alt, "sine=f=400:d=1,apad=pad_dur=4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(alt), "-c:a", "adpcm_ima_wav",
                        str(pfade.FX / "aaaaaaaaaaaa.wav")], check=True)
        z.medien.fx_hinzu(Effekt(id="aaaaaaaaaaaa", name="Wolf", kategorie="werwolf", dauer_ms=5000, groesse=1))
        z.szenen.liste.append(Szene(id="t", name="T", schritte=[Schritt(typ="fx", fx_id="aaaaaaaaaaaa")]))
        asyncio.run(z.fx_analysieren("aaaaaaaaaaaa"))
        assert z.medien.fx("aaaaaaaaaaaa").hoerbar_ms < 1500
        r = c.post("/api/fx/nachbearbeiten").json()
        assert r["gekuerzt"] == 1 and r["gespart_s"] > 3
        neu = next(f for f in z.medien.daten.fx if f.name == "Wolf")
        assert neu.id != "aaaaaaaaaaaa" and neu.dauer_ms < 1600 and neu.kategorie == "werwolf" and neu.analyse
        assert not (pfade.FX / "aaaaaaaaaaaa.wav").exists()
        assert z.szenen.liste[-1].schritte[0].fx_id == neu.id
        assert c.post("/api/fx/nachbearbeiten").json()["gekuerzt"] == 0
        c.put(f"/api/fx/{neu.id}", json={"name": "Wolf", "kategorie": "werwolf", "vol": 100, "licht": None,
                                          "beschreibung": "  Wolfsrudel in der Ferne  "})
        assert z.medien.fx(neu.id).text == "Wolfsrudel in der Ferne"
        z.medien.fx_loeschen(neu.id)
        z.szenen.liste.pop()


def test_gleicher_name_wird_nicht_doppelt_hochgeladen(tmp_path):
    q = tmp_path / "gleich.wav"
    _ton(q, "sine=f=300:d=1")
    with TestClient(app) as c:
        z = app.state.zentrale
        a = c.post("/api/fx", files={"datei": ("eule-ruft.wav", q.read_bytes(), "audio/wav")}, data={"kategorie": "tiere"}).json()
        _ton(q, "sine=f=500:d=2")          # anderer Inhalt, gleicher Dateiname
        b = c.post("/api/fx", files={"datei": ("eule-ruft.wav", q.read_bytes(), "audio/wav")}, data={"kategorie": "tiere"}).json()
        assert a["id"] == b["id"] and sum(f.name == a["name"] for f in z.medien.daten.fx) == 1
        z.medien.fx_loeschen(a["id"])


def test_kategorie_automatisch():
    k = fxanalyse.kategorie_auto
    assert k("lazychillzone-wolves-howling-1-225304", ["Howl"]) == ("werwolf", True, ["werwolf"])
    assert k("universfield-creaking-door-03", ["Tools"])[:2] == ("tuer", True)
    kat, sicher, kand = k("chrysalyn-footsteps-and-knocking-with-mysterious", ["Thunk"])
    assert not sicher and set(kand) == {"schritte", "klopfen"}                 # zwei Kandidaten: nachfragen
    assert k("aufnahme-17", ["Crow"])[:2] == ("kraehe", False)                 # nur der Klang: nachfragen
    assert k("aufnahme-18", ["Silence"])[:2] == ("sonstiges", False)
    assert k("creepy-voice-line", ["Speech"])[0] == "stimme"


def test_pruefen_und_ersetzen(tmp_path):
    q = tmp_path / "wolf.wav"
    _ton(q, "sine=f=300:d=2")
    with TestClient(app) as c:
        z = app.state.zentrale
        vorher = sorted(p.name for p in pfade.FX.iterdir())
        p = c.post("/api/fx/pruefen", files={"datei": ("wolves-howling.wav", q.read_bytes(), "audio/wav")}).json()
        assert p["kategorie"] == "werwolf" and p["sicher"] and p["vorhanden"] is None and 1900 < p["dauer_ms"] < 2400
        assert sorted(x.name for x in pfade.FX.iterdir()) == vorher                       # nichts abgelegt
        alt = c.post("/api/fx", files={"datei": ("wolves-howling.wav", q.read_bytes(), "audio/wav")},
                     data={"kategorie": "werwolf"}).json()
        z.szenen.liste.append(Szene(id="w", name="W", schritte=[Schritt(typ="fx", fx_id=alt["id"])]))
        p = c.post("/api/fx/pruefen", files={"datei": ("wolves-howling.wav", q.read_bytes(), "audio/wav")}).json()
        assert p["vorhanden"]["id"] == alt["id"] and p["vorhanden"]["gleich"]
        _ton(q, "sine=f=300:d=5")                                                         # neue, längere Fassung
        p = c.post("/api/fx/pruefen", files={"datei": ("wolves-howling.wav", q.read_bytes(), "audio/wav")}).json()
        assert not p["vorhanden"]["gleich"]
        neu = c.post("/api/fx", files={"datei": ("wolves-howling.wav", q.read_bytes(), "audio/wav")},
                     data={"kategorie": "werwolf", "ersetzen": alt["id"]}).json()
        assert neu["id"] != alt["id"] and neu["dauer_ms"] > 4500 and neu["name"] == alt["name"]
        assert z.medien.fx(alt["id"]) is None and z.szenen.liste[-1].schritte[0].fx_id == neu["id"]
        assert sum(f.name == alt["name"] for f in z.medien.daten.fx) == 1
        z.medien.fx_loeschen(neu["id"])
        z.szenen.liste.pop()
