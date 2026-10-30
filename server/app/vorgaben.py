"""Eingebaute Stimmungen, Szenen und Ereignis-Regeln (beim ersten Start angelegt).

Die Szenen verweisen auf Kategorien statt auf bestimmte Dateien – sie funktionieren,
sobald passende Sounds hochgeladen sind. Fehlt eine Kategorie, wird der Schritt
übersprungen.
"""

from .modelle import LichtAktion, Regel, Schritt, Stimmung, Szene, Zone

# Licht, das ein neu hochgeladener Sound je Kategorie bekommt (in der Oberfläche änderbar)
LICHT_JE_KATEGORIE: dict[str, LichtAktion | None] = {
    "donner": LichtAktion(name="env_blitz", dauer_ms=0, staerke=100),
    "schrei": LichtAktion(name="schreck", dauer_ms=2500, staerke=100, param=80),
    "lachen": LichtAktion(name="flackern", dauer_ms=0, staerke=90),
    "herz": LichtAktion(name="env_puls", dauer_ms=0, staerke=80),
    "kette": LichtAktion(name="flackern", dauer_ms=0, staerke=60),
    "kettensaege": LichtAktion(name="flackern", dauer_ms=0, staerke=100),
    "stimme": LichtAktion(name="env_puls", dauer_ms=0, staerke=60),
    "reaktion": LichtAktion(name="flackern", dauer_ms=900, staerke=80),
    "werwolf": LichtAktion(name="env_puls", dauer_ms=0, staerke=90),
    "tuer": LichtAktion(name="flackern", dauer_ms=0, staerke=50),
    "schritte": LichtAktion(name="welle", dauer_ms=1500, staerke=70),
    "kraehe": LichtAktion(name="puls", dauer_ms=600, staerke=50),
    "glocke": LichtAktion(name="env_puls", dauer_ms=0, staerke=90),
    "spieluhr": None,
    "zombie": LichtAktion(name="flackern", dauer_ms=0, staerke=70),
    "fluestern": LichtAktion(name="env_puls", dauer_ms=0, staerke=40),
    "klopfen": LichtAktion(name="env_blitz", dauer_ms=0, staerke=80),
    "tiere": LichtAktion(name="flackern", dauer_ms=800, staerke=40),
    "wind": None,
    "sonstiges": None,
    "system": None,
}


def _alle(e: str, tempo=50, staerke=80, basis=5) -> list[Zone]:
    return [Zone(e=e, tempo=tempo, staerke=staerke, basis=basis) for _ in range(5)]


# (Version, in der die Stimmung dazukam, Stimmung) – siehe Zentrale._vorgaben_ergaenzen
STIMMUNGEN_VERSION = 2
STIMMUNGEN = [(1, s) for s in [
    Stimmung(id="gruselig", name="Gruselig ruhig", eingebaut=True, zonen=[
        Zone(e="flackern", tempo=40, staerke=55, basis=10),
        Zone(e="atmen", tempo=30, staerke=50, basis=8),
        Zone(e="flackern", tempo=35, staerke=65, basis=12),
        Zone(e="atmen", tempo=30, staerke=50, basis=8),
        Zone(e="flackern", tempo=40, staerke=55, basis=10),
    ]),
    Stimmung(id="geister", name="Geister", eingebaut=True, zonen=_alle("wandern", 35, 90, 4)),
    Stimmung(id="gewitternacht", name="Gewitternacht", eingebaut=True, zonen=_alle("gewitter", 50, 100, 6)),
    Stimmung(id="lagerfeuer", name="Lagerfeuer", eingebaut=True, zonen=_alle("feuer", 50, 80, 20)),
    Stimmung(id="kaputte-lampe", name="Kaputte Lampe", eingebaut=True, zonen=[
        Zone(e="flackern", tempo=40, staerke=50, basis=10),
        Zone(e="neon", tempo=50, staerke=90, basis=5),
        Zone(e="flackern", tempo=40, staerke=50, basis=10),
        Zone(e="neon", tempo=60, staerke=90, basis=5),
        Zone(e="flackern", tempo=40, staerke=50, basis=10),
    ]),
    Stimmung(id="herzschlag", name="Herzschlag", eingebaut=True, zonen=_alle("herzschlag", 40, 85, 6)),
    Stimmung(id="dunkel", name="Fast dunkel", eingebaut=True, zonen=_alle("atmen", 20, 25, 3)),
    Stimmung(id="musik", name="Zur Musik", eingebaut=True, zonen=_alle("musik", 50, 90, 8), musik_reaktiv=40),
]] + [(2, s) for s in [
    # Bausteine für die Licht-Szenen (ohne Ton)
    Stimmung(id="schwarz", name="Schwarz", eingebaut=True, zonen=_alle("aus", 50, 0, 0)),
    Stimmung(id="unruhig", name="Unruhiges Flackern", eingebaut=True, zonen=_alle("flackern", 95, 80, 4)),
    Stimmung(id="ein-geist", name="Ein Geist", eingebaut=True, zonen=_alle("wandern", 22, 100, 0)),
    Stimmung(id="pingpong-langsam", name="Ping-Pong langsam", eingebaut=True, zonen=_alle("pingpong", 30, 95, 0)),
    Stimmung(id="pingpong-schnell", name="Ping-Pong schnell", eingebaut=True, zonen=_alle("pingpong", 85, 100, 0)),
    Stimmung(id="wetterleuchten", name="Wetterleuchten", eingebaut=True, zonen=_alle("gewitter", 25, 35, 0)),
    Stimmung(id="kaputte-roehre", name="Kaputte Röhre", eingebaut=True, zonen=[
        Zone(e="neon", tempo=60, staerke=95, basis=0), Zone(e="aus", basis=0), Zone(e="neon", tempo=40, staerke=90, basis=0),
        Zone(e="aus", basis=0), Zone(e="neon", tempo=75, staerke=95, basis=0)]),
    Stimmung(id="herz-langsam", name="Herzschlag langsam", eingebaut=True, zonen=_alle("herzschlag", 25, 70, 0)),
    Stimmung(id="herz-schnell", name="Herzschlag schnell", eingebaut=True, zonen=_alle("herzschlag", 85, 90, 0)),
    Stimmung(id="strobo", name="Stroboskop", eingebaut=True, zonen=_alle("strobo", 60, 100, 0)),
]]


def _fx(t, kategorie, vol=100, licht="auto", aktion=None) -> Schritt:
    return Schritt(t_ms=t, typ="fx", kategorie=kategorie, vol=vol, licht=licht, aktion=aktion)


def _st(t, stimmung, dauer) -> Schritt:
    return Schritt(t_ms=t, typ="stimmung", stimmung_id=stimmung, dauer_ms=dauer)


def _ak(t, name, dauer, staerke=100, param=0) -> Schritt:
    return Schritt(t_ms=t, typ="aktion", aktion=LichtAktion(name=name, dauer_ms=dauer, staerke=staerke, param=param))


# (Version, in der die Szene dazukam, Szene) – siehe Zentrale._vorgaben_ergaenzen
SZENEN_VERSION = 3
SZENEN = [(1, s) for s in [
    Szene(id="gewitterfront", name="Gewitterfront", gruselstufe=1, eingebaut=True, schritte=[
        Schritt(t_ms=0, typ="stimmung", stimmung_id="gewitternacht", dauer_ms=36000),
        _fx(0, "wind", vol=70, licht="keine"),
        _fx(3000, "donner"),
        _fx(14000, "donner"),
        _fx(26000, "donner", vol=80),
    ]),
    Szene(id="schreck", name="Schreck", gruselstufe=3, eingebaut=True, schritte=[
        Schritt(t_ms=0, typ="aktion", aktion=LichtAktion(name="blackout", dauer_ms=1600)),
        _fx(1200, "schrei", licht="eigen", aktion=LichtAktion(name="schreck", dauer_ms=2500, param=80)),
    ]),
    Szene(id="geisterwanderung", name="Geisterwanderung", gruselstufe=1, eingebaut=True, schritte=[
        Schritt(t_ms=0, typ="stimmung", stimmung_id="geister", dauer_ms=25000),
        _fx(500, "wind", vol=80, licht="keine"),
        _fx(9000, "lachen", vol=80, licht="keine"),
    ]),
    Szene(id="herzschlag", name="Herzschlag", gruselstufe=2, eingebaut=True, schritte=[
        Schritt(t_ms=0, typ="stimmung", stimmung_id="dunkel", dauer_ms=16000),
        _fx(800, "herz"),
    ]),
    Szene(id="hexenauftritt", name="Hexenauftritt", gruselstufe=2, eingebaut=True, schritte=[
        _fx(0, "wind", vol=70, licht="keine"),
        _fx(4000, "lachen"),
        _fx(11000, "lachen"),
        Schritt(t_ms=15000, typ="aktion", aktion=LichtAktion(name="welle", dauer_ms=1500, staerke=90)),
    ]),
]] + [
    (2, Szene(id="kettensaege", name="Kettensäge", gruselstufe=2, eingebaut=True, schritte=[
        Schritt(t_ms=0, typ="aktion", aktion=LichtAktion(name="blackout", dauer_ms=900)),
        _fx(600, "kettensaege"),
    ])),
    (2, Szene(id="begruessung", name="Begrüßung", gruselstufe=1, eingebaut=True, schritte=[
        Schritt(t_ms=0, typ="stimmung", stimmung_id="dunkel", dauer_ms=8000),
        _fx(300, "stimme"),
    ])),
] + [(3, s) for s in [
    # Licht ohne Ton (1.8) – kurz genug für den Regisseur
    Szene(id="stromausfall", name="Stromausfall", gruselstufe=1, eingebaut=True, schritte=[
        _st(0, "unruhig", 2500), _st(2500, "schwarz", 6000), _ak(8500, "blitz", 300)]),
    Szene(id="totenstille", name="Totenstille", gruselstufe=1, eingebaut=True, schritte=[
        Schritt(t_ms=0, typ="musik", musik="stille", dauer_ms=7000), _st(0, "schwarz", 7000)]),
    Szene(id="geist-zieht-vorbei", name="Geist zieht vorbei", gruselstufe=1, eingebaut=True, schritte=[
        _st(0, "ein-geist", 9000)]),
    Szene(id="pingpong-geist", name="Ping-Pong-Geist", gruselstufe=2, eingebaut=True, schritte=[
        _st(0, "pingpong-langsam", 4000), _st(4000, "pingpong-schnell", 3000), _st(7000, "schwarz", 1500)]),
    Szene(id="wetterleuchten", name="Wetterleuchten", gruselstufe=1, eingebaut=True, schritte=[
        _st(0, "wetterleuchten", 10000)]),
    Szene(id="kaputte-roehre", name="Kaputte Röhre", gruselstufe=1, eingebaut=True, schritte=[
        _st(0, "kaputte-roehre", 8000)]),
    Szene(id="herzschlag-licht", name="Herzschlag-Licht", gruselstufe=2, eingebaut=True, schritte=[
        _st(0, "herz-langsam", 5000), _st(5000, "herz-schnell", 4000), _st(9000, "schwarz", 1500)]),
    Szene(id="haus-atmet", name="Das Haus atmet", gruselstufe=1, eingebaut=True, schritte=[
        Schritt(t_ms=0, typ="musik", musik="leise", dauer_ms=12000), _st(0, "dunkel", 12000)]),
    Szene(id="strobo-salve", name="Strobo-Salve", gruselstufe=3, eingebaut=True, schritte=[
        _ak(0, "blackout", 800), _st(800, "strobo", 1200), _st(2000, "schwarz", 1500)]),
    # mit Ton
    Szene(id="donner-aus-dem-dunkel", name="Donner aus dem Dunkel", gruselstufe=1, eingebaut=True, schritte=[
        _st(0, "schwarz", 1500), _fx(1200, "donner")]),
    Szene(id="werwolf", name="Werwolf", gruselstufe=2, eingebaut=True, schritte=[
        Schritt(t_ms=0, typ="musik", musik="stille", dauer_ms=9000), _st(0, "schwarz", 2000), _fx(1500, "werwolf"),
        _st(2000, "herz-langsam", 7000)]),
]]

# (Version, in der die Regel dazukam, Regel) – siehe Zentrale._vorgaben_ergaenzen
REGELN_VERSION = 2
REGELN = [(1, r) for r in [
    Regel(id="donner", typ="kategorie", ziel="donner", min_s=60, max_s=240, gruselstufe_min=1),
    Regel(id="lachen", typ="kategorie", ziel="lachen", min_s=120, max_s=420, gruselstufe_min=1),
    Regel(id="wind", typ="kategorie", ziel="wind", min_s=90, max_s=300, gruselstufe_min=1, vol=80),
    Regel(id="kette", typ="kategorie", ziel="kette", min_s=180, max_s=600, gruselstufe_min=2),
    Regel(id="schrei", typ="kategorie", ziel="schrei", min_s=300, max_s=900, gruselstufe_min=3),
    Regel(id="sz-gewitterfront", typ="szene", ziel="gewitterfront", min_s=600, max_s=1500, gruselstufe_min=1),
    Regel(id="sz-hexenauftritt", typ="szene", ziel="hexenauftritt", min_s=600, max_s=1200, gruselstufe_min=2),
    Regel(id="sz-herzschlag", typ="szene", ziel="herzschlag", min_s=500, max_s=1000, gruselstufe_min=2),
    Regel(id="sz-schreck", typ="szene", ziel="schreck", min_s=600, max_s=1500, gruselstufe_min=3),
]] + [(2, r) for r in [
    # Licht ohne Ton – bringt Dunkelheit in die Show
    Regel(id="sz-stromausfall", typ="szene", ziel="stromausfall", min_s=300, max_s=900, gruselstufe_min=1),
    Regel(id="sz-totenstille", typ="szene", ziel="totenstille", min_s=400, max_s=1000, gruselstufe_min=1),
    Regel(id="sz-geist", typ="szene", ziel="geist-zieht-vorbei", min_s=240, max_s=600, gruselstufe_min=1),
    Regel(id="sz-pingpong", typ="szene", ziel="pingpong-geist", min_s=400, max_s=900, gruselstufe_min=2),
    Regel(id="sz-wetterleuchten", typ="szene", ziel="wetterleuchten", min_s=300, max_s=800, gruselstufe_min=1),
    Regel(id="sz-roehre", typ="szene", ziel="kaputte-roehre", min_s=400, max_s=900, gruselstufe_min=1),
    Regel(id="sz-herzlicht", typ="szene", ziel="herzschlag-licht", min_s=500, max_s=1000, gruselstufe_min=2),
    Regel(id="sz-atmet", typ="szene", ziel="haus-atmet", min_s=400, max_s=900, gruselstufe_min=1),
    Regel(id="sz-strobo", typ="szene", ziel="strobo-salve", min_s=900, max_s=1800, gruselstufe_min=3),
    Regel(id="sz-donner-dunkel", typ="szene", ziel="donner-aus-dem-dunkel", min_s=400, max_s=900, gruselstufe_min=1),
    Regel(id="sz-werwolf", typ="szene", ziel="werwolf", min_s=400, max_s=1000, gruselstufe_min=2),
    # neue Sound-Kategorien (bleiben still, bis Sounds hochgeladen sind)
    Regel(id="werwolf", typ="kategorie", ziel="werwolf", min_s=300, max_s=700, gruselstufe_min=2),
    Regel(id="tuer", typ="kategorie", ziel="tuer", min_s=200, max_s=500, gruselstufe_min=1),
    Regel(id="schritte", typ="kategorie", ziel="schritte", min_s=200, max_s=500, gruselstufe_min=1),
    Regel(id="kraehe", typ="kategorie", ziel="kraehe", min_s=150, max_s=400, gruselstufe_min=1, vol=80),
    Regel(id="glocke", typ="kategorie", ziel="glocke", min_s=600, max_s=1200, gruselstufe_min=1),
    Regel(id="spieluhr", typ="kategorie", ziel="spieluhr", min_s=500, max_s=1000, gruselstufe_min=1),
    Regel(id="zombie", typ="kategorie", ziel="zombie", min_s=300, max_s=700, gruselstufe_min=2),
    Regel(id="fluestern", typ="kategorie", ziel="fluestern", min_s=200, max_s=500, gruselstufe_min=1),
    Regel(id="klopfen", typ="kategorie", ziel="klopfen", min_s=250, max_s=600, gruselstufe_min=2),
    Regel(id="tiere", typ="kategorie", ziel="tiere", min_s=150, max_s=400, gruselstufe_min=1, vol=80),
]]
