import io
import wave

import pytest
from fastapi.testclient import TestClient

from app.ki import KiFehler, KiStimme, satz_bereinigen, wav_dauer_ms
from app.main import app

from .test_api import _anmelden, warte_auf


def _wav(sekunden: float = 1.5) -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\0\0" * int(44100 * sekunden))
    return b.getvalue()


def test_satz_bereinigen():
    assert satz_bereinigen('"Hey du, kleines Skelett!" *flüstert*\nNoch was') == "Hey du, kleines Skelett!"
    assert satz_bereinigen("„Komm näher…“ (leise)") == "Komm näher…"
    assert satz_bereinigen("") == ""
    assert satz_bereinigen("...Komm näher… ich warte.") == "Komm näher… ich warte."


def test_wav_dauer():
    assert wav_dauer_ms(_wav(1.5)) == 1500
    assert wav_dauer_ms(b"kaputt") == 3000


@pytest.fixture
def client(monkeypatch):
    aufrufe = []

    async def standbild(self, c, kamera, breite=None):
        aufrufe.append(("bild", kamera))
        return b"jpeg"

    async def satz(self, c, bild, lage=""):
        aufrufe.append(("satz", lage))
        if self.z.einst.ki.zusatz == "leer":
            return "Leerer Weg mit Fahrradanhänger.", ""
        return "Ein Kind als Skelett mitten auf dem Weg.", "Hey du da, kleines Skelett!"

    async def sprechen(self, c, text):
        aufrufe.append(("sprechen", text))
        if self.z.einst.ki.stimme == "kaputt":
            raise KiFehler("Sprach-Dienst: HTTP 500")
        return _wav(2.0)

    monkeypatch.setattr(KiStimme, "_standbild", standbild)
    monkeypatch.setattr(KiStimme, "_satz", satz)
    monkeypatch.setattr(KiStimme, "_sprechen", sprechen)
    with TestClient(app) as c:
        c.aufrufe = aufrufe
        c.put("/api/einstellungen/ki", json={"llm_url": "http://ki:8091", "tts_url": "http://ki:8092"})
        yield c


def _pi(client):
    ws = client.websocket_connect("/ws/geraet?rolle=sound&fw=1.4.0")
    ws.__enter__()
    ws.send_json({"t": "hallo", "rolle": "sound", "fw": "1.4.0", "plattform": "pi", "kann_sagen": True})
    ws.send_json({"t": "status", "uart": True})
    warte_auf(ws, "fx_sync")
    return ws


def test_ki_ausloeser_spricht_ueber_den_pi(client):
    liste = client.get("/api/status").json()["einstellungen"]["ausloeser"]
    liste.append({"name": "KI Haustür", "aktiv": True, "entity_id": "binary_sensor.haustuer_person",
                  "typ": "ki", "ziel": "camera.haustuer_tele", "sperre_s": 60})
    assert client.put("/api/ausloeser", json=liste).status_code == 200
    aid = client.get("/api/status").json()["einstellungen"]["ausloeser"][-1]["id"]
    ws = _pi(client)
    licht = _anmelden(client, "licht")
    try:
        assert client.post(f"/api/ausloeser/{aid}").json()["ausgeloest"] is True
        m = warte_auf(ws, "sag")
        assert m["pfad"].startswith("/api/geraet/sprache/") and m["vol"] == 100
        assert m["licht"]["name"] == "env_puls" and m["licht"]["dauer_ms"] == 2300
        r = client.get(m["pfad"])
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and len(r.content) > 100000
        assert ("bild", "camera.haustuer_tele") in client.aufrufe
        assert client.post(f"/api/ausloeser/{aid}").json()["ausgeloest"] is False   # Sperrzeit
        texte = [e["text"] for e in client.get("/api/status").json()["protokoll"]]
        assert any("Hey du da, kleines Skelett!" in t for t in texte)
    finally:
        ws.__exit__(None, None, None)
        licht.__exit__(None, None, None)
    assert client.get("/api/geraet/sprache/gibtsnicht").status_code == 404


def test_ki_fehler_spielt_ersatz(client):
    ws = _pi(client)
    try:
        client.put("/api/einstellungen/ki", json={"stimme": "kaputt"})
        r = client.post("/api/ki/test", json={"kamera": "camera.haustuer_tele"})
        assert r.json() == {"ergebnis": "fehler"}
        texte = [e["text"] for e in client.get("/api/status").json()["protokoll"]]
        assert any("KI nicht erreichbar (Sprach-Dienst: HTTP 500)" in t for t in texte)
        client.put("/api/einstellungen/ki", json={"stimme": "maedchen", "zusatz": "leer"})
        assert client.post("/api/ki/test", json={"kamera": "camera.haustuer_tele"}).json() == {"ergebnis": "niemand"}
        texte = [e["text"] for e in client.get("/api/status").json()["protokoll"]]
        assert any("kein Mensch zu erkennen" in t for t in texte)
        st = client.get("/api/ki/status").json()
        assert [e["ergebnis"] for e in st["verlauf"]][:2] == ["niemand", "fehler"]
        assert st["verlauf"][0]["beschreibung"] == "Leerer Weg mit Fahrradanhänger."
        assert st["llm"]["ok"] is False and st["tts"]["ok"] is False      # http://ki:… gibt es im Test nicht
        assert client.get("/api/ki/bild").content == b"jpeg"
    finally:
        ws.__exit__(None, None, None)
    assert client.post("/api/ki/test", json={"kamera": "kein-kamera"}).status_code == 422


def test_ki_ohne_neue_pi_software(client):
    ws = client.websocket_connect("/ws/geraet?rolle=sound&fw=1.3.6")
    ws.__enter__()
    try:
        ws.send_json({"t": "hallo", "rolle": "sound", "fw": "1.3.6", "plattform": "pi"})
        warte_auf(ws, "fx_sync")
        assert client.post("/api/ki/test", json={"kamera": "camera.x_y"}).json() == {"ergebnis": "fehler"}
        texte = [e["text"] for e in client.get("/api/status").json()["protokoll"]]
        assert any("Pi-Software aktualisieren" in t for t in texte)
    finally:
        ws.__exit__(None, None, None)


def test_ki_aus_spielt_wie_bisher(client, ton):
    from .test_api import _fx
    _fx(client, ton, "fluestern.wav", "stimme")
    liste = client.get("/api/status").json()["einstellungen"]["ausloeser"]
    liste.append({"name": "TrackMix", "aktiv": True, "entity_id": "binary_sensor.trackmix_person", "typ": "ki",
                  "ziel": "camera.trackmix_tele", "hinweis": "Noch weit weg – locke sie näher.", "sperre_s": 0})
    client.put("/api/ausloeser", json=liste)
    aid = client.get("/api/status").json()["einstellungen"]["ausloeser"][-1]["id"]
    assert client.post("/api/ki", json={"an": False}).status_code == 200
    assert client.get("/api/status").json()["zustand"]["ki_an"] is False
    ws = _pi(client)
    try:
        assert client.post(f"/api/ausloeser/{aid}").json()["ausgeloest"] is True
        assert warte_auf(ws, "fx")["id"]                     # normaler Stimmen-Sound statt KI
        assert not [a for a in client.aufrufe if a[0] == "bild"]
        client.post("/api/ki", json={"an": True})
        assert client.post(f"/api/ausloeser/{aid}").json()["ausgeloest"] is True
        warte_auf(ws, "sag")
        assert ("bild", "camera.trackmix_tele") in client.aufrufe
        assert ("satz", "Noch weit weg – locke sie näher.") in client.aufrufe
    finally:
        ws.__exit__(None, None, None)


def test_ki_ersatz_szene_je_ausloeser(client):
    liste = client.get("/api/status").json()["einstellungen"]["ausloeser"]
    liste.append({"name": "KI nah", "aktiv": True, "entity_id": "binary_sensor.klingel_person", "typ": "ki",
                  "ziel": "camera.klingel", "ersatz_typ": "szene", "ersatz_ziel": "begruessung", "sperre_s": 0})
    client.put("/api/ausloeser", json=liste)
    aid = client.get("/api/status").json()["einstellungen"]["ausloeser"][-1]["id"]
    client.post("/api/ki", json={"an": False})
    assert client.post(f"/api/ausloeser/{aid}").json()["ausgeloest"] is True
    st = client.get("/api/status").json()
    assert st["szene"] is not None and st["szene"]["id"] == "begruessung"
    assert client.get("/api/ki/live/kein_kamera").status_code == 404
    assert client.get("/api/ki/live/camera.klingel").content == b"jpeg"
