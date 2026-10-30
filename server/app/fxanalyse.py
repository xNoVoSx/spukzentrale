"""Effekt-Analyse: Was ist in einem Sound zu hören und wie verläuft er? Grundlage für den KI-Regisseur.

Einmal pro Effekt (beim Hochladen bzw. nach dem Start für alte), Ergebnis in medien.json (Effekt.analyse):
  - hörbare Länge (ohne Stille am Ende) – der Regisseur plant damit, die KI-Stimme wartet nicht auf Stille
  - Verlauf: schlagartig / schwillt an / in Stößen / gleichmäßig, Zeitpunkt des lautesten Moments
  - was YAMNet hört (AudioSet-Klassen, englisch – Claude versteht sie)
  - Kategorie-Prüfung: Dateiname und YAMNet passen eher zu einer anderen Kategorie -> Hinweis in der Oberfläche
Die automatische Beschreibung fasst das zusammen; eine eigene Beschreibung (Effekt.beschreibung) hat Vorrang.
"""

import logging
import re
import subprocess
from pathlib import Path

import numpy as np

from .modelle import FxAnalyse
from .musikanalyse import RATE, YAMNET

log = logging.getLogger(__name__)

FX_ANALYSE_VERSION = 1
STILLE_DB = -50.0                # darunter gilt ein Fenster als still (relativ zur Vollaussteuerung)
FENSTER_S = 0.05
NICHT_ZEIGEN = {"Silence", "Music", "Inside, small room", "Inside, large room or hall", "Outside, rural or natural",
                "Outside, urban or manmade", "Sound effect", "Environmental noise", "Static", "Noise"}

# Woran man eine Kategorie erkennt: Wörter im Dateinamen (englisch/deutsch) und YAMNet-Klassen (genaue Namen)
MERKMALE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "donner": (("thunder", "donner", "gewitter", "lightning"), ("Thunder", "Thunderstorm")),
    "schrei": (("scream", "schrei", "shriek", "yell"), ("Screaming", "Shout", "Yell", "Children shouting")),
    "lachen": (("laugh", "lach", "cackle", "giggle"), ("Laughter", "Giggle", "Chuckle, chortle", "Belly laugh",
                                                     "Snicker")),
    "herz": (("heart", "herz"), ("Heart sounds, heartbeat", "Heart murmur")),
    "wind": (("wind", "breeze"), ("Wind", "Wind noise (microphone)", "Rustle")),
    "kette": (("chain", "kette"), ("Rattle", "Chink, clink", "Jingle, tinkle")),
    "kettensaege": (("chainsaw", "kettensäge", "kettensaege"), ("Chainsaw", "Power tool")),
    "werwolf": (("wolf", "wolves", "werewolf", "werwolf", "howl"), ("Howl", "Growling")),
    "tuer": (("door", "tür", "tuer", "creak", "squeak"), ("Door", "Creak", "Squeak")),
    "schritte": (("footstep", "steps", "schritt", "walking"), ("Walk, footsteps", "Run")),
    "kraehe": (("crow", "raven", "krähe", "kraehe", "rabe"), ("Crow", "Caw")),
    "glocke": (("bell", "glocke", "toll", "chime", "gong"), ("Bell", "Church bell", "Gong", "Chime")),
    "spieluhr": (("music-box", "musicbox", "music box", "spieluhr", "lullaby"), ("Music box", "Glockenspiel")),
    "zombie": (("zombie", "undead", "groan", "moan"), ("Groan", "Grunt")),
    "fluestern": (("whisper", "flüster", "fluester"), ("Whispering",)),
    "klopfen": (("knock", "bang", "klopf", "pound", "slam"), ("Knock", "Bang", "Thump, thud", "Slam")),
    "tiere": (("owl", "eule", "bat-", "bats", "fledermaus", "hoot"), ("Owl", "Hoot")),
}


def hoerbar_s(pfad: Path, verstaerkung_db: float = 0.0) -> float:
    """Bis wohin ist der Sound (nach der Verstärkung) zu hören? 0 = gar nicht/unbekannt."""
    x = _laden(pfad) * np.float32(10 ** (verstaerkung_db / 20))
    return _verlauf(x)[0] / 1000


def _laden(pfad: Path) -> np.ndarray:
    roh = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(pfad), "-ac", "1", "-ar", str(RATE),
                          "-f", "f32le", "-"], capture_output=True, check=True, timeout=60).stdout
    return np.frombuffer(roh, np.float32)


def _verlauf(x: np.ndarray) -> tuple[int, float, str]:
    """(hörbar_ms, lautester Moment s, Verlauf in Worten)."""
    n = max(1, int(FENSTER_S * RATE))
    k = len(x) // n
    if k == 0:
        return 0, 0.0, "sehr kurz"
    rms = np.sqrt(np.mean(x[: k * n].reshape(k, n) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    laut = np.nonzero(db > STILLE_DB)[0]
    if len(laut) == 0:
        return 0, 0.0, "still"
    ende = laut[-1] + 1
    db = db[:ende]
    hoerbar_ms = int(ende * FENSTER_S * 1000)
    spitze = int(np.argmax(db))
    spitze_s = spitze * FENSTER_S
    # Stöße: der Pegel steigt mehrmals nah an die Spitze und fällt dazwischen deutlich ab
    stoesse, oben, spitzenpegel = 0, False, db.max()
    for v in db:
        if not oben and v > spitzenpegel - 12:
            stoesse, oben = stoesse + 1, True
        elif oben and v < spitzenpegel - 20:
            oben = False
    anstieg = np.argmax(db >= db.max() - 3) * FENSTER_S        # bis 3 dB unter der Spitze
    if stoesse >= 3:
        text = f"in {stoesse} Stößen"
    elif anstieg <= 0.3:
        text = "setzt schlagartig ein" + (", klingt aus" if spitze_s < 0.4 * ende * FENSTER_S else "")
    elif spitze_s > 0.6 * ende * FENSTER_S:
        text = "schwillt langsam an"
    else:
        text = "gleichmäßig"
    return hoerbar_ms, round(spitze_s, 1), text


def _klassen(x: np.ndarray) -> list[str]:
    if not YAMNET.bereit() or len(x) == 0:
        return []
    w = YAMNET.werte(x).max(axis=0)
    aus = []
    for i in np.argsort(w)[::-1]:
        name = YAMNET.labels[i]
        if w[i] < 0.12 or len(aus) >= 4:
            break
        if name not in NICHT_ZEIGEN:
            aus.append(name)
    return aus


def kategorie_vorschlag(name: str, kategorie: str, klassen: list[str]) -> str | None:
    """Andere Kategorie, wenn Dateiname oder Klang klar dorthin zeigen und nichts für die eigene spricht."""
    datei = name.lower()

    def passt(kat: str) -> tuple[bool, bool]:
        woerter, yam = MERKMALE.get(kat, ((), ()))
        return any(w in datei for w in woerter), any(k in yam for k in klassen)

    eigen_datei, eigen_klang = passt(kategorie)
    if eigen_datei or kategorie in ("stimme", "reaktion", "sonstiges", "system"):
        return None
    andere = [k for k in MERKMALE if k != kategorie]
    # der Dateiname ist der stärkste Hinweis, dann der Klang (nur wenn für die eigene Kategorie nichts spricht)
    treffer = [k for k in andere if passt(k)[0]] or ([] if eigen_klang else [k for k in andere if passt(k)[1]])
    return treffer[0] if treffer else None


def beschreibung(name: str, a: FxAnalyse) -> str:
    teile = [f"Datei „{name_sauber(name)}“"]
    if a.klassen:
        teile.append("klingt nach " + ", ".join(a.klassen))
    teile.append(f"{a.hoerbar_ms / 1000:.1f} s, {a.verlauf}")
    if a.spitze_s > 0.3:
        teile.append(f"am lautesten bei {a.spitze_s:.1f} s")
    return "; ".join(teile)


def analysieren(pfad: Path, name: str, kategorie: str) -> FxAnalyse:
    x = _laden(pfad)
    hoerbar_ms, spitze_s, verlauf = _verlauf(x)
    klassen = _klassen(x[: int(hoerbar_ms / 1000 * RATE) + RATE // 4] if hoerbar_ms else x)
    a = FxAnalyse(version=FX_ANALYSE_VERSION, hoerbar_ms=hoerbar_ms, spitze_s=spitze_s, verlauf=verlauf,
                  klassen=klassen)
    a.vorschlag = kategorie_vorschlag(name, kategorie, klassen)
    a.text = beschreibung(name, a)
    return a


def name_sauber(name: str) -> str:
    """„lazychillzone-wolves-howling-1-225304“ -> lesbarer für Mensch und Modell (nur zur Anzeige)."""
    return re.sub(r"[-_]\d{5,}$", "", name).replace("-", " ").replace("_", " ").strip()
