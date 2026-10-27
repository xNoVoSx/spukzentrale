"""Ende-zu-Ende über die REST-API mit simulierten ESPs (WebSocket)."""

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app


def warte_auf(ws, typ: str, versuche: int = 30) -> dict:
    for _ in range(versuche):
        m = ws.receive_json()
        if m.get("t") == typ:
            return m
    raise AssertionError(f"keine Nachricht {typ}")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _fx(client, ton, name="donner.wav", kategorie="donner"):
    pfad = ton(name, "anoisesrc=c=brown:a=0.8", 1.5)
    with open(pfad, "rb") as f:
        r = client.post("/api/fx", files={"datei": (name, f)}, data={"kategorie": kategorie})
    assert r.status_code == 200, r.text
    return r.json()


def _anmelden(client, rolle, uart=True):
    ws = client.websocket_connect(f"/ws/geraet?rolle={rolle}&fw=1.0.0")
    ws.__enter__()
    ws.send_json({"t": "hallo", "rolle": rolle, "fw": "1.0.0", "ip": "127.0.0.1", "fx_gesamt_kb": 2240})
    ws.send_json({"t": "status", "uart": uart})
    return ws


def test_status_und_vorgaben(client):
    d = client.get("/api/status").json()
    from app import VERSION
    assert d["version"] == VERSION
    assert {s["id"] for s in d["szenen"]} >= {"gewitterfront", "schreck", "geisterwanderung", "herzschlag", "hexenauftritt"}
    assert any(f["kategorie"] == "system" for f in d["medien"]["fx"])  # Test-Klick
    assert len(d["zustand"]["licht"]["zonen"]) == 5


def test_fx_upload_sync_und_abspielen_per_kabel(client, ton):
    f = _fx(client, ton)
    assert f["licht"]["name"] == "env_blitz"
    licht = _anmelden(client, "licht")
    sound = _anmelden(client, "sound")
    try:
        assert warte_auf(sound, "config")["latenz_ms"] == 60
        sync = warte_auf(sound, "fx_sync")
        assert f["id"] in {e["id"] for e in sync["liste"]}
        # Datei für den ESP abrufbar und im richtigen Format
        r = client.get(sync["basis"] + f["id"])
        assert r.status_code == 200 and r.content[:4] == b"RIFF" and len(r.content) == f["groesse"]
        time.sleep(0.2)  # Status verarbeiten lassen
        client.post(f"/api/fx/{f['id']}/abspielen", json={})
        m = warte_auf(sound, "fx")
        assert m["id"] == f["id"]
        # Licht geht mit dem Sound über das Kabel, Dauer = Sound + Nachklang
        assert m["licht"]["name"] == "env_blitz" and m["licht"]["dauer_ms"] == f["dauer_ms"] + 300
    finally:
        sound.__exit__(None, None, None)
        licht.__exit__(None, None, None)


def test_ohne_kabel_schickt_server_licht_mit_latenz(client, ton):
    f = _fx(client, ton)
    licht = _anmelden(client, "licht", uart=False)
    sound = _anmelden(client, "sound", uart=False)
    try:
        warte_auf(sound, "fx_sync")
        time.sleep(0.2)
        client.put("/api/einstellungen/ton", json={"latenz_ms": 90})
        client.post(f"/api/fx/{f['id']}/abspielen", json={})
        assert "licht" not in warte_auf(sound, "fx")
        a = warte_auf(licht, "aktion")
        assert a["name"] == "env_blitz" and a["verz_ms"] == 90
    finally:
        sound.__exit__(None, None, None)
        licht.__exit__(None, None, None)


def test_ohne_sound_esp_nur_licht_mit_ersatz(client, ton):
    f = _fx(client, ton)
    licht = _anmelden(client, "licht")
    try:
        client.post(f"/api/fx/{f['id']}/abspielen", json={})
        a = warte_auf(licht, "aktion")
        assert a["name"] == "blitz"  # env_blitz hätte ohne Ton nichts zu tun
    finally:
        licht.__exit__(None, None, None)


def test_licht_zustand_und_stimmung(client):
    licht = _anmelden(client, "licht")
    try:
        warte_auf(licht, "zustand")
        client.put("/api/zonen/1", json={"e": "neon", "tempo": 60, "staerke": 90, "basis": 5})
        z = warte_auf(licht, "zustand")
        assert z["zonen"][1]["e"] == "neon" and "temp" not in z
        client.post("/api/stimmungen/geister/anwenden")
        z = warte_auf(licht, "zustand")
        assert all(zz["e"] == "wandern" for zz in z["zonen"])
        r = client.post("/api/stimmungen", json={"name": "Meine Grusel-Ecke"})
        assert r.json()["id"] == "meine-grusel-ecke"
        assert client.put("/api/zonen/7", json={"e": "an"}).status_code == 404
    finally:
        licht.__exit__(None, None, None)


def test_szene_mit_stimmung_ist_temporaer(client, ton):
    _fx(client, ton, "schrei.wav", "schrei")
    licht = _anmelden(client, "licht")
    try:
        warte_auf(licht, "zustand")
        szene = {"name": "Kurz", "schritte": [
            {"t_ms": 0, "typ": "stimmung", "stimmung_id": "gewitternacht", "dauer_ms": 300},
            {"t_ms": 50, "typ": "aktion", "aktion": {"name": "welle", "dauer_ms": 200}},
            {"t_ms": 100, "typ": "fx", "kategorie": "schrei"},
        ]}
        sid = client.post("/api/szenen", json=szene).json()["id"]
        client.post(f"/api/szenen/{sid}/start")
        z = warte_auf(licht, "zustand")
        assert z.get("temp") is True and z["zonen"][0]["e"] == "gewitter"
        a = warte_auf(licht, "aktion")
        assert a["name"] == "welle" and a["verz_ms"] == 60  # gleiche Zeitleiste wie der Ton
        z = warte_auf(licht, "zustand")
        assert "temp" not in z and z["zonen"][0]["e"] == "flackern"  # zurück zur Grundstimmung
    finally:
        licht.__exit__(None, None, None)


def test_ausloeser_sperrzeit_und_gruselstufe(client):
    liste = client.get("/api/status").json()["einstellungen"]["ausloeser"]
    assert [a["id"] for a in liste] == ["klingel", "person"]
    liste[0].update({"ziel": "hexenauftritt", "sperre_s": 60, "gruselstufe_min": 2})
    assert client.put("/api/ausloeser", json=liste).status_code == 200
    client.post("/api/gruselstufe", json={"stufe": 1})
    assert client.post("/api/ausloeser/klingel").json()["ausgeloest"] is False
    client.post("/api/gruselstufe", json={"stufe": 2})
    assert client.post("/api/ausloeser/klingel").json()["ausgeloest"] is True
    assert client.post("/api/ausloeser/klingel").json()["ausgeloest"] is False  # Sperrzeit
    assert client.post("/api/ausloeser/klingel?test=true").json()["ausgeloest"] is True  # Test-Knopf
    assert client.post("/api/ausloeser/gibtsnicht").status_code == 404


def test_neue_szenen_und_kategorien(client, ton):
    d = client.get("/api/status").json()
    assert {"kettensaege", "begruessung"} <= {s["id"] for s in d["szenen"]}
    assert {"kettensaege", "stimme"} <= set(d["kategorien"])
    f = _fx(client, ton, "saege.wav", "kettensaege")
    assert f["licht"]["name"] == "flackern"


def test_aufbau_einstellbar(client):
    r = client.put("/api/einstellungen/aufbau", json={"streifen": 12, "streifen_m": 0.8, "zuordnung": [0, 0, 1, 9]})
    assert r.status_code == 200
    a = client.get("/api/status").json()["einstellungen"]["aufbau"]
    assert a["streifen"] == 12 and a["zuordnung"] == [0, 0, 1, 5] and a["wand_m"] == 5.0


def test_geheimnisse_werden_nicht_ausgeliefert(client):
    r = client.put("/api/einstellungen/mqtt", json={"host": "broker", "passwort": "streng-geheim", "aktiv": False})
    assert r.json()["passwort"] != "streng-geheim"
    client.put("/api/einstellungen/mqtt", json={"passwort": r.json()["passwort"], "port": 1884})
    assert client.app.state.zentrale.einst.mqtt.passwort == "streng-geheim"
    assert "streng-geheim" not in client.get("/api/status").text


def test_ungueltige_eingaben(client, ton):
    assert client.put("/api/einstellungen/zeitplan", json={"start": "25:00"}).status_code == 422
    assert client.put("/api/einstellungen/gibtsnicht", json={}).status_code == 404
    assert client.get("/api/geraet/fx/../../etc/passwd").status_code == 404
    leer = ton("still.wav", "anullsrc=r=44100", 1.0)
    with open(leer, "rb") as f:
        r = client.post("/api/fx", files={"datei": ("still.wav", f)}, data={"kategorie": "wind"})
    assert r.status_code == 400 and "still" in r.json()["detail"]


def test_mqtt_discovery_eindeutig(client):
    from app.mqtt import MqttBruecke
    b = MqttBruecke(client.app.state.zentrale)
    e = b._entitaeten()
    ids = [c["unique_id"] for _, _, c in e]
    assert len(ids) == len(set(ids))
    assert any(k == "button" and "gewitterfront" in c["command_topic"] for k, _, c in e)


def test_neustart_nach_absturz_wird_protokolliert(client):
    ws = client.websocket_connect("/ws/geraet?rolle=licht&fw=1.2.0")
    ws.__enter__()
    try:
        ws.send_json({"t": "hallo", "rolle": "licht", "fw": "1.2.0", "neustart_grund": "Watchdog (Task)", "neustart_normal": False})
        warte_auf(ws, "zustand")
        texte = [e["text"] for e in client.get("/api/status").json()["protokoll"]]
        assert "Licht-ESP ist neu gestartet: Watchdog (Task)" in texte
    finally:
        ws.__exit__(None, None, None)


def test_ambiente_doppelt_einschalten_startet_stream_nicht_neu(client):
    sound = _anmelden(client, "sound")
    try:
        warte_auf(sound, "fx_sync")
        client.post("/api/ambiente", json={"an": True})
        assert warte_auf(sound, "musik")["aktion"] == "play"
        client.post("/api/ambiente", json={"an": True})   # schon an -> kein zweites "play"
        client.post("/api/ambiente", json={"an": False})
        assert warte_auf(sound, "musik")["aktion"] == "stop"   # die nächste musik-Nachricht ist das Stopp
    finally:
        sound.__exit__(None, None, None)
