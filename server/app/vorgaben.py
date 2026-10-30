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
    "wind": None,
    "sonstiges": None,
    "system": None,
}


def _alle(e: str, tempo=50, staerke=80, basis=5) -> list[Zone]:
    return [Zone(e=e, tempo=tempo, staerke=staerke, basis=basis) for _ in range(5)]


STIMMUNGEN = [
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
]


def _fx(t, kategorie, vol=100, licht="auto", aktion=None) -> Schritt:
    return Schritt(t_ms=t, typ="fx", kategorie=kategorie, vol=vol, licht=licht, aktion=aktion)


# (Version, in der die Szene dazukam, Szene) – siehe Zentrale._vorgaben_ergaenzen
SZENEN_VERSION = 2
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
]

REGELN = [
    Regel(id="donner", typ="kategorie", ziel="donner", min_s=60, max_s=240, gruselstufe_min=1),
    Regel(id="lachen", typ="kategorie", ziel="lachen", min_s=120, max_s=420, gruselstufe_min=1),
    Regel(id="wind", typ="kategorie", ziel="wind", min_s=90, max_s=300, gruselstufe_min=1, vol=80),
    Regel(id="kette", typ="kategorie", ziel="kette", min_s=180, max_s=600, gruselstufe_min=2),
    Regel(id="schrei", typ="kategorie", ziel="schrei", min_s=300, max_s=900, gruselstufe_min=3),
    Regel(id="sz-gewitterfront", typ="szene", ziel="gewitterfront", min_s=600, max_s=1500, gruselstufe_min=1),
    Regel(id="sz-hexenauftritt", typ="szene", ziel="hexenauftritt", min_s=600, max_s=1200, gruselstufe_min=2),
    Regel(id="sz-herzschlag", typ="szene", ziel="herzschlag", min_s=500, max_s=1000, gruselstufe_min=2),
    Regel(id="sz-schreck", typ="szene", ziel="schreck", min_s=600, max_s=1500, gruselstufe_min=3),
]
