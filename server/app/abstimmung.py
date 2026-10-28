"""Zufalls-Effekte passend zum laufenden Ambiente-Stück.

Der Generator fragt vor jedem Zufalls-Effekt planen(): Passt der Effekt jetzt (ggf. auf den nächsten Schlag
verschoben), oder soll er warten – weil gerade gesungen wird (außer Kategorien, die über Gesang dürfen) oder weil
gleich eine ruhige Stelle kommt, in der er besser wirkt? Szenen und Auslöser (Klingel …) fragen nicht, sie haben
immer Vorrang. Häufigkeit: gewicht() skaliert den Abstand einer Regel nach der Stimmung des Stücks.
"""

from dataclasses import dataclass

from .modelle import MusikAbstimmung, Musikstueck

VORLAUF_S = 0.3          # so lange dauert es ungefähr, bis ein Effekt nach dem Senden zu hören ist
NACHLAUF_S = 0.5         # Effekt soll so lange nach seinem Ende auch nicht in Gesang hängen
PAUSE_VORAUS_S = 10.0    # auf eine ruhige Stelle warten, die so bald beginnt
TAKT_MAX_S = 1.5         # weiter nicht auf einen Schlag warten


@dataclass
class Plan:
    jetzt: bool               # True: auslösen (nach verzoegerung_s), False: warten_s warten und neu fragen
    verzoegerung_s: float = 0.0
    warten_s: float = 0.0
    grund: str = ""


def _ueberlappt(a0: float, a1: float, abschnitte) -> tuple[float, float] | None:
    for b0, b1 in abschnitte:
        if a0 < b1 and b0 < a1:
            return b0, b1
    return None


def planen(stueck: Musikstueck | None, pos_s: float | None, kategorie: str, fx_dauer_s: float,
           einst: MusikAbstimmung, pause_gewartet: bool = False) -> Plan:
    if not einst.aktiv or stueck is None or pos_s is None or stueck.analyse is None:
        return Plan(True, grund="keine Musikdaten")
    a = stueck.analyse
    start = pos_s + VORLAUF_S

    # 1. Gesang: nur erlaubte Kategorien dürfen hinein
    if kategorie not in einst.ueber_gesang:
        treffer = _ueberlappt(start, start + fx_dauer_s + NACHLAUF_S, stueck.stimme)
        if treffer:
            ende = treffer[1] - pos_s + 0.5
            return Plan(False, warten_s=min(max(ende, 1.0), einst.max_warten_s), grund="Gesang")

    # 2. Ruhige Stelle: mitten drin -> sofort; kommt bald eine -> einmal darauf warten
    verz = 0.0
    if einst.pausen and not _ueberlappt(start, start + 0.1, a.ruhig) and not pause_gewartet:
        kommend = [b0 for b0, _ in a.ruhig if 0 < b0 - pos_s <= PAUSE_VORAUS_S]
        if kommend:
            return Plan(False, warten_s=max(0.5, kommend[0] - pos_s - VORLAUF_S), grund="ruhige Stelle")

    # 3. Takt: auf den nächsten Schlag legen
    if einst.takt and a.bpm:
        periode = 60.0 / a.bpm
        n = -(-(start - a.beat0_s) // periode)                     # aufrunden
        naechster = a.beat0_s + n * periode
        if 0 <= naechster - start <= TAKT_MAX_S:
            verz = naechster - start
    return Plan(True, verzoegerung_s=round(verz, 3), grund="passt")


def gewicht(stueck: Musikstueck | None, kategorie: str, einst: MusikAbstimmung) -> float:
    """Faktor für die Häufigkeit: >1 kommt öfter (Abstand wird durch den Faktor geteilt)."""
    if not einst.aktiv or not einst.stimmung or stueck is None or stueck.analyse is None:
        return 1.0
    return min(2.0, max(0.5, stueck.analyse.gewichte.get(kategorie, 1.0)))
