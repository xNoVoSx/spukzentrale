"""Datenmodelle. Alles, was gespeichert oder über die API geschickt wird."""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Muss zur Firmware passen (firmware/licht/src/effekte.cpp, firmware/common/protokoll.h)
EFFEKTE = [
    "aus", "an", "flackern", "feuer", "atmen", "herzschlag", "strobo",
    "gewitter", "lauflicht", "wandern", "pingpong", "neon", "zufall", "musik",
    "spektrum", "beat",   # ab 1.3: Analyse vom Pi (ohne Pi folgen sie der Hüllkurve)
]
AKTIONEN = ["blitz", "env_blitz", "schreck", "blackout", "welle", "alle_an", "puls", "test", "flackern", "env_puls"]

KATEGORIEN = {
    "donner": "Donner",
    "schrei": "Schrei",
    "lachen": "Lachen",
    "herz": "Herz",
    "wind": "Wind",
    "kette": "Kette",
    "kettensaege": "Kettensäge",
    "werwolf": "Werwolf",
    "tuer": "Knarrende Tür",
    "schritte": "Schritte",
    "kraehe": "Krähen",
    "glocke": "Totenglocke",
    "spieluhr": "Spieluhr",
    "zombie": "Zombie",
    "fluestern": "Geisterflüstern",
    "klopfen": "Klopfen/Poltern",
    "tiere": "Eule/Fledermäuse",
    "stimme": "Stimme",
    "reaktion": "KI-Reaktion",
    "sonstiges": "Sonstiges",
}

MAX_ZONEN = 6
UHRZEIT = r"^([01]\d|2[0-3]):[0-5]\d$"

EffektName = Literal[
    "aus", "an", "flackern", "feuer", "atmen", "herzschlag", "strobo",
    "gewitter", "lauflicht", "wandern", "pingpong", "neon", "zufall", "musik", "spektrum", "beat",
]
AktionName = Literal[
    "keine", "blitz", "env_blitz", "schreck", "blackout", "welle", "alle_an", "puls", "test", "flackern", "env_puls",
]
Kategorie = Literal[
    "donner", "schrei", "lachen", "herz", "wind", "kette", "kettensaege", "werwolf", "tuer", "schritte", "kraehe",
    "glocke", "spieluhr", "zombie", "fluestern", "klopfen", "tiere", "stimme", "reaktion", "sonstiges", "system",
]


def neue_id(name: str, vorhandene: set[str]) -> str:
    """Lesbare, eindeutige ID aus einem Namen ("Große Gewitterfront" -> "grosse-gewitterfront")."""
    s = name.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:32] or "eintrag"
    kandidat, n = s, 2
    while kandidat in vorhandene:
        kandidat = f"{s}-{n}"
        n += 1
    return kandidat


# ---------------------------------------------------------------------------
# Licht
# ---------------------------------------------------------------------------
class Zone(BaseModel):
    e: EffektName = "flackern"
    tempo: int = Field(50, ge=0, le=100)
    staerke: int = Field(80, ge=0, le=100)
    basis: int = Field(5, ge=0, le=100)


def _standard_zonen() -> list[Zone]:
    return [Zone() for _ in range(5)]


class LichtZustand(BaseModel):
    an: bool = True
    hell: int = Field(80, ge=0, le=100)
    musik_reaktiv: int = Field(0, ge=0, le=100)
    zonen: list[Zone] = Field(default_factory=_standard_zonen, max_length=MAX_ZONEN)


class LichtAktion(BaseModel):
    name: AktionName = "blitz"
    zonen: list[int] = Field(default_factory=list)  # leer = alle
    dauer_ms: int = Field(1000, ge=0, le=60000)    # 0 = so lang wie der Sound
    staerke: int = Field(100, ge=0, le=100)
    param: int = Field(0, ge=0, le=65535)

    @field_validator("zonen")
    @classmethod
    def _zonen(cls, v: list[int]) -> list[int]:
        return sorted({z for z in v if 0 <= z < MAX_ZONEN})


class Zustand(BaseModel):
    """Was gerade läuft. Wird gespeichert, damit nach einem Neustart alles weiterläuft."""
    licht: LichtZustand = Field(default_factory=LichtZustand)
    ambiente_an: bool = False
    ereignisse_an: bool = False
    ki_an: bool = True             # KI-Stimme; aus = KI-Auslöser spielen ihren Ersatz-Sound
    regie_an: bool = False         # KI-Regisseur wählt während eines Besuchs die Effekte (regie.py)
    gruselstufe: int = Field(2, ge=1, le=3)
    stimmung_id: str | None = None


class Stimmung(BaseModel):
    """Grundbeleuchtung zum Abrufen (Favorit)."""
    id: str = ""
    name: str = Field("Neue Stimmung", min_length=1, max_length=40)
    zonen: list[Zone] = Field(default_factory=_standard_zonen, max_length=MAX_ZONEN)
    musik_reaktiv: int = Field(0, ge=0, le=100)
    eingebaut: bool = False


class Stimmungen(BaseModel):
    liste: list[Stimmung] = Field(default_factory=list)
    # welche eingebauten Stimmungen schon angelegt wurden (neue Versionen ergänzen nur neue)
    vorgaben_version: int = 0


# ---------------------------------------------------------------------------
# Medien
# ---------------------------------------------------------------------------
Abschnitt = tuple[float, float]   # (von_s, bis_s)
Stimmungsart = Literal["duester", "ruhig", "dramatisch", "verspielt"]


class MusikAnalyse(BaseModel):
    """Ergebnis von musikanalyse.analysieren() – wird beim Hochladen bzw. Start einmal berechnet."""
    version: int = 0
    modell: bool = False                                       # Gesang/Stimmung per YAMNet (sonst leer/geraten)
    stimme: list[Abschnitt] = Field(default_factory=list)      # erkannter Gesang/Sprache
    ruhig: list[Abschnitt] = Field(default_factory=list)       # deutlich leisere Stellen
    bpm: float | None = None                                   # None = kein klarer Takt
    beat0_s: float = 0.0                                       # erster Schlag (Phase des Takts)
    stimmung: Stimmungsart = "duester"
    gewichte: dict[str, float] = Field(default_factory=dict)   # Kategorie -> Faktor für die Häufigkeit
    pegel: list[int] = Field(default_factory=list)             # 0..100 je Sekunde (Anzeige)
    stimme_kurve: list[int] = Field(default_factory=list)      # 0..100 je Sekunde (Anzeige)


class Musikstueck(BaseModel):
    id: str
    name: str
    dauer_s: float = 0
    groesse: int = 0
    analyse: MusikAnalyse | None = None
    stimme_manuell: list[Abschnitt] | None = None              # von Hand korrigiert (None = Erkennung gilt)

    @property
    def stimme(self) -> list[Abschnitt]:
        if self.stimme_manuell is not None:
            return self.stimme_manuell
        return self.analyse.stimme if self.analyse else []


class Playlist(BaseModel):
    reihenfolge: list[str] = Field(default_factory=list)
    zufall: bool = False
    ueberblend_s: float = Field(4.0, ge=0, le=15)


class FxAnalyse(BaseModel):
    """Ergebnis von fxanalyse.analysieren() – für den Regisseur und die Kategorie-Prüfung."""
    version: int = 0
    hoerbar_ms: int = 0                 # ohne Stille am Ende
    spitze_s: float = 0                 # lautester Moment
    verlauf: str = ""                   # „setzt schlagartig ein“, „schwillt langsam an“, „in 3 Stößen“ …
    klassen: list[str] = Field(default_factory=list)   # was YAMNet hört (AudioSet, englisch)
    vorschlag: str | None = None        # passt eher in diese Kategorie
    text: str = ""                      # automatische Beschreibung


class Effekt(BaseModel):
    """Effekt-Sound. Die ID ist ein Hash des konvertierten Inhalts."""
    id: str
    name: str = Field(min_length=1, max_length=60)
    kategorie: Kategorie = "sonstiges"
    vol: int = Field(100, ge=0, le=100)
    licht: LichtAktion | None = None
    dauer_ms: int = 0
    groesse: int = 0
    beschreibung: str = Field("", max_length=300)     # eigene Beschreibung (hat Vorrang vor der automatischen)
    analyse: FxAnalyse | None = None

    @property
    def text(self) -> str:
        """Was der Regisseur über den Sound erfährt."""
        return self.beschreibung.strip() or (self.analyse.text if self.analyse else self.name)

    @property
    def hoerbar_ms(self) -> int:
        return self.analyse.hoerbar_ms if self.analyse and self.analyse.hoerbar_ms else self.dauer_ms


class Medien(BaseModel):
    musik: list[Musikstueck] = Field(default_factory=list)
    playlist: Playlist = Field(default_factory=Playlist)
    fx: list[Effekt] = Field(default_factory=list)
    # zuletzt gemeldetes Sound-Gerät – gilt weiter, wenn es gerade aus ist (sonst wäre der Speicher „voll“)
    sound_plattform: str = ""
    sound_speicher_kb: int = 0


# ---------------------------------------------------------------------------
# Szenen (Abläufe auf einer Zeitleiste)
# ---------------------------------------------------------------------------
class Schritt(BaseModel):
    t_ms: int = Field(0, ge=0, le=600000)
    typ: Literal["fx", "aktion", "stimmung", "musik"] = "fx"
    # fx: entweder ein bestimmter Sound oder ein zufälliger aus einer Kategorie
    fx_id: str | None = None
    kategorie: Kategorie | None = None
    vol: int = Field(100, ge=0, le=100)
    licht: Literal["auto", "keine", "eigen"] = "auto"
    # aktion (oder Licht bei fx mit licht="eigen")
    aktion: LichtAktion | None = None
    # stimmung: Grundbeleuchtung für dauer_ms wechseln, danach zurück
    stimmung_id: str | None = None
    # musik: Hintergrundmusik für dauer_ms aus („stille“) bzw. leise, danach wieder normal
    musik: Literal["stille", "leise"] = "stille"
    dauer_ms: int = Field(10000, ge=100, le=600000)


class Szene(BaseModel):
    id: str = ""
    name: str = Field("Neue Szene", min_length=1, max_length=40)
    gruselstufe: int = Field(1, ge=1, le=3)
    schritte: list[Schritt] = Field(default_factory=list, max_length=100)
    eingebaut: bool = False


class Szenen(BaseModel):
    liste: list[Szene] = Field(default_factory=list)
    # welche eingebauten Szenen schon angelegt wurden (neue Versionen ergänzen nur neue)
    vorgaben_version: int = 0


# ---------------------------------------------------------------------------
# Ereignis-Generator
# ---------------------------------------------------------------------------
class Regel(BaseModel):
    id: str = ""
    aktiv: bool = True
    typ: Literal["kategorie", "szene"] = "kategorie"
    ziel: str = "donner"          # Kategorie oder Szenen-ID
    min_s: int = Field(60, ge=5, le=7200)
    max_s: int = Field(240, ge=5, le=7200)
    gruselstufe_min: int = Field(1, ge=1, le=3)
    vol: int = Field(100, ge=0, le=100)


class Ereignisse(BaseModel):
    min_abstand_s: int = Field(75, ge=0, le=600)
    # ohne Besucher nur Atmosphäre – Schrecks, Szenen mit Schrei usw. nur, wenn jemand da ist (Regisseur/Klingel)
    schreck_nur_bei_besuch: bool = True
    regeln: list[Regel] = Field(default_factory=list)
    vorgaben_version: int = 0


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------
class LichtEinst(BaseModel):
    zonen: int = Field(5, ge=1, le=MAX_ZONEN)
    zonen_namen: list[str] = Field(default_factory=lambda: [f"Zone {i + 1}" for i in range(MAX_ZONEN)])
    # Farbe der Zone für die Live-Vorschau (z. B. wenn Farbfolie drauf ist)
    zonen_farben: list[str] = Field(default_factory=lambda: ["#fff1de"] * MAX_ZONEN)
    zone_max: list[int] = Field(default_factory=lambda: [100] * MAX_ZONEN)
    gamma: float = Field(2.2, ge=1.0, le=3.0)
    strobo_max_hz: float = Field(3.0, ge=0.5, le=15.0)
    # Grundlicht (Stimmung/Musik) höchstens so hell – Platz nach oben für Effekte (Blitze, Szenen-Licht)
    grund_max: int = Field(55, ge=10, le=100)
    choreografie: bool = True    # zur Hintergrundmusik wechselnde Zonen-Muster (choreografie.py)

    @field_validator("zonen_namen", "zonen_farben", "zone_max")
    @classmethod
    def _sechs(cls, v: list, info):
        standard = {"zonen_namen": None, "zonen_farben": "#fff1de", "zone_max": 100}[info.field_name]
        v = list(v)[:MAX_ZONEN]
        while len(v) < MAX_ZONEN:
            v.append(standard if standard is not None else f"Zone {len(v) + 1}")
        if info.field_name == "zone_max":
            v = [max(0, min(100, int(x))) for x in v]
        if info.field_name == "zonen_farben":
            v = [x if re.fullmatch(r"#[0-9a-fA-F]{6}", str(x)) else "#fff1de" for x in v]
        return v


class TonEinst(BaseModel):
    master: int = Field(80, ge=0, le=100)
    musik_vol: int = Field(60, ge=0, le=100)
    fx_vol: int = Field(100, ge=0, le=100)
    duck: int = Field(40, ge=0, le=100)
    latenz_ms: int = Field(60, ge=0, le=600)


class Standort(BaseModel):
    breite: float = Field(51.16, ge=-90, le=90)
    laenge: float = Field(10.45, ge=-180, le=180)


class Zeitplan(BaseModel):
    aktiv: bool = False
    start_modus: Literal["uhrzeit", "sonnenuntergang"] = "sonnenuntergang"
    start: str = Field("18:00", pattern=UHRZEIT)
    versatz_min: int = Field(0, ge=-180, le=180)
    ende: str = Field("22:00", pattern=UHRZEIT)
    ambiente: bool = True
    ereignisse: bool = True


class Ruhezeit(BaseModel):
    aktiv: bool = False
    ab: str = Field("21:00", pattern=UHRZEIT)
    bis: str = Field("07:00", pattern=UHRZEIT)
    modus: Literal["leiser", "stumm"] = "leiser"
    lautst: int = Field(30, ge=0, le=100)  # Gesamtlautstärke in der Ruhezeit (modus leiser)


class Grusel(BaseModel):
    auto: bool = False
    stufe2_ab: str = Field("19:00", pattern=UHRZEIT)
    stufe3_ab: str = Field("20:30", pattern=UHRZEIT)


class Ausloeser(BaseModel):
    """Sensor aus Home Assistant (Klingel, Personenerkennung …) löst Sound oder Szene aus."""
    id: str = ""
    name: str = Field("Auslöser", min_length=1, max_length=40)
    aktiv: bool = False
    entity_id: str = ""
    typ: Literal["szene", "kategorie", "ki"] = "szene"
    ziel: str = "schreck"          # Szenen-ID, Kategorie bzw. bei "ki" die Kamera (camera.…)
    hinweis: str = Field("", max_length=300)  # nur "ki": Lage für das Modell (z. B. „noch weit weg“)
    # nur "ki": was stattdessen passiert, wenn die KI aus ist, keinen Menschen sieht oder nicht antwortet
    ersatz_typ: Literal["kategorie", "szene"] = "kategorie"
    # Rolle im Besuchs-Ablauf (KI): fern = Weg (lockt), nah = Haustür (fordert zum Klingeln auf), klingel = Taster
    rolle: Literal["", "fern", "nah", "klingel"] = ""
    ersatz_ziel: str = ""          # leer = Ersatz-Kategorie aus den KI-Einstellungen
    vol: int = Field(100, ge=0, le=100)
    sperre_s: int = Field(60, ge=0, le=3600)
    gruselstufe_min: int = Field(1, ge=1, le=3)
    nur_wenn_an: bool = True


def _standard_ausloeser() -> list[Ausloeser]:
    return [
        Ausloeser(id="klingel", name="Klingel gedrückt", typ="szene", ziel="schreck", sperre_s=30),
        Ausloeser(id="person", name="Person erkannt", typ="szene", ziel="kettensaege", sperre_s=180),
    ]


class Aufbau(BaseModel):
    """Wie die LED-Streifen an der Wand hängen – für Montageplan und Live-Vorschau."""
    wand_m: float = Field(5.0, gt=0, le=50)
    streifen: int = Field(10, ge=1, le=60)            # senkrecht hängende Streifen, gleichmäßig verteilt
    streifen_m: float = Field(1.0, gt=0, le=10)       # Länge je Streifen
    # Zone je Streifen (von links); leer = gleichmäßig auf die Zonen verteilt
    zuordnung: list[int] = Field(default_factory=list, max_length=60)
    kasten_m: float = Field(2.5, ge=0, le=50)         # Position des Kastens von links
    zuleitung_extra_m: float = Field(0.5, ge=0, le=20)  # Weg vom Kasten zum Anschluss des Streifens (Höhe)
    einspeisung: Literal["oben", "unten"] = "oben"
    watt_pro_m: float = Field(10.0, gt=0, le=50)
    querschnitt_mm2: float = Field(0.75, gt=0, le=10)
    netzteil_w: int = Field(200, ge=10, le=2000)

    @field_validator("zuordnung")
    @classmethod
    def _zonen(cls, v: list[int]) -> list[int]:
        return [max(0, min(MAX_ZONEN - 1, int(z))) for z in v]


class HomeAssistant(BaseModel):
    url: str = ""     # z. B. http://192.168.1.20:8123
    token: str = ""   # Langzeit-Zugriffstoken


class KiEinst(BaseModel):
    """KI-Stimme: Bild-Sprachmodell (OpenAI-kompatibel, z. B. llama-server) + Sprach-Dienst (ki/stimme.py)."""
    llm_url: str = ""           # z. B. http://192.168.1.50:8091
    modell: str = ""            # leer = was der Server geladen hat
    tts_url: str = ""           # z. B. http://192.168.1.50:8092
    stimme: str = "maedchen"
    vol: int = Field(100, ge=0, le=100)
    zusatz: str = Field("", max_length=500)   # eigene Vorgaben fürs Modell
    zeitlimit_s: float = Field(10.0, ge=2, le=60)
    bildbreite: int = Field(896, ge=256, le=2048)
    ersatz_kategorie: str = "stimme"          # leer = bei Fehlern nichts abspielen
    reaktion_kategorie: str = "reaktion"      # sofort beim Auslösen (kurzes Flüstern), leer = aus
    # Besuchs-Ablauf (Auslöser mit Rolle fern/nah/klingel)
    locken_max: int = Field(2, ge=1, le=5)            # so oft höchstens locken pro Besuch
    locken_wiederholen_s: int = Field(30, ge=10, le=300)
    abschied: bool = True                             # beim Weggehen verabschieden (nie zurücklocken)
    tuer_ruhe_s: int = Field(180, ge=0, le=1800)      # nach dem Klingeln still (ihr redet an der Tür)
    besuch_ende_s: int = Field(60, ge=10, le=600)     # so lange keine Person -> Besuch vorbei
    nachlauf_s: int = Field(90, ge=0, le=600)         # danach Nachlauf: Leute auf dem Rückweg nicht zurücklocken
    neu_pruefen_s: int = Field(15, ge=10, le=300)     # geht/Nachlauf: frühestens dann prüfen, ob Neue kommen
    # Regisseur (Zustand.regie_an)
    regie_abstand_s: int = Field(30, ge=5, le=300)    # in Pausen frühestens so oft ein Effekt
    regie_max_s: int = Field(15, ge=2, le=60)         # längere Sounds nimmt der Regisseur nicht
    regie_szene_max_s: int = Field(15, ge=2, le=120)  # längere Szenen nimmt der Regisseur nicht
    regie_modell: str = Field("claude-opus-5-5", max_length=60)   # Claude-Modell (leer = nur lokal); Schlüssel: Umgebungsvariable ANTHROPIC_API_KEY
    regie_zeitlimit_s: float = Field(6.0, ge=1, le=30)  # länger braucht Claude nicht – sonst entscheidet das lokale Modell


class Mqtt(BaseModel):
    aktiv: bool = False
    host: str = ""
    port: int = Field(1883, ge=1, le=65535)
    benutzer: str = ""
    passwort: str = ""
    praefix: str = Field("spukzentrale", pattern=r"^[a-zA-Z0-9_\-/]+$")
    discovery: str = Field("homeassistant", pattern=r"^[a-zA-Z0-9_\-/]+$")


class MusikAbstimmung(BaseModel):
    """Zufalls-Effekte passend zum laufenden Ambiente-Stück (Szenen und Auslöser sind davon nicht betroffen)."""
    aktiv: bool = True
    ueber_gesang: list[Kategorie] = Field(default_factory=lambda: ["donner", "wind"])   # dürfen in Gesang
    pausen: bool = True          # auf eine ruhige Stelle warten, wenn bald eine kommt
    takt: bool = True            # auf den nächsten Schlag legen
    stimmung: bool = True        # Kategorien je nach Stimmung des Stücks häufiger/seltener
    max_warten_s: int = Field(45, ge=5, le=300)


class Einstellungen(BaseModel):
    licht: LichtEinst = Field(default_factory=LichtEinst)
    ton: TonEinst = Field(default_factory=TonEinst)
    standort: Standort = Field(default_factory=Standort)
    zeitplan: Zeitplan = Field(default_factory=Zeitplan)
    ruhezeit: Ruhezeit = Field(default_factory=Ruhezeit)
    grusel: Grusel = Field(default_factory=Grusel)
    ausloeser: list[Ausloeser] = Field(default_factory=_standard_ausloeser, max_length=20)
    aufbau: Aufbau = Field(default_factory=Aufbau)
    homeassistant: HomeAssistant = Field(default_factory=HomeAssistant)
    ki: KiEinst = Field(default_factory=KiEinst)
    mqtt: Mqtt = Field(default_factory=Mqtt)
    musik: MusikAbstimmung = Field(default_factory=MusikAbstimmung)

    @model_validator(mode="before")
    @classmethod
    def _klingel_uebernehmen(cls, d):
        """Version 1.0 hatte genau einen Auslöser "klingel" – in die Liste übernehmen."""
        if isinstance(d, dict) and "klingel" in d and "ausloeser" not in d:
            k = d.pop("klingel") or {}
            liste = [a.model_dump() for a in _standard_ausloeser()]
            liste[0].update({
                "aktiv": k.get("aktiv", False), "entity_id": k.get("entity_id", ""),
                "ziel": k.get("szene_id", "schreck"), "sperre_s": k.get("sperre_s", 30),
                "gruselstufe_min": k.get("gruselstufe_min", 1), "nur_wenn_an": k.get("nur_wenn_an", True),
            })
            d["ausloeser"] = liste
        return d

    @field_validator("ausloeser")
    @classmethod
    def _ids(cls, v: list[Ausloeser]) -> list[Ausloeser]:
        vorhanden: set[str] = set()
        for a in v:
            if not a.id or a.id in vorhanden:
                a.id = neue_id(a.name, vorhanden)
            vorhanden.add(a.id)
        return v


GEHEIME_FELDER = {("homeassistant", "token"), ("mqtt", "passwort")}
GEHEIM_PLATZHALTER = "••••••••"
