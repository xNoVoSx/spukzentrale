from datetime import date, datetime

import pytest

from app.zeitplan import TZ, Zeitplan, in_zeitraum, minuten, stufe_nach_uhrzeit
from app.zentrale import Zentrale


def test_in_zeitraum_normal_und_ueber_mitternacht():
    assert in_zeitraum(minuten("18:30"), "18:00", "22:00")
    assert not in_zeitraum(minuten("22:00"), "18:00", "22:00")
    assert in_zeitraum(minuten("23:30"), "21:00", "07:00")
    assert in_zeitraum(minuten("03:00"), "21:00", "07:00")
    assert not in_zeitraum(minuten("12:00"), "21:00", "07:00")
    assert not in_zeitraum(minuten("12:00"), "10:00", "10:00")


@pytest.mark.parametrize("uhr,stufe", [("17:00", 1), ("19:00", 2), ("20:29", 2), ("20:30", 3), ("01:00", 3), ("11:59", 3), ("12:00", 1)])
def test_gruselstufe_nach_uhrzeit(uhr, stufe):
    assert stufe_nach_uhrzeit(minuten(uhr), "19:00", "20:30") == stufe


def test_sonnenuntergang_halloween_mitteldeutschland():
    z = Zentrale()
    su = Zeitplan(z).sonnenuntergang(date(2026, 10, 31))
    assert su is not None
    # Mitte Deutschlands: Sonnenuntergang am 31.10. gegen 16:50 Uhr
    assert 16 * 60 + 30 < su.hour * 60 + su.minute < 17 * 60 + 15


def _zeit(t: str, tag: int = 31) -> datetime:
    h, m = map(int, t.split(":"))
    return datetime(2026, 10, tag, h, m, tzinfo=TZ)


async def test_zeitplan_schaltet_nur_bei_wechsel():
    z = Zentrale()
    z.einst.zeitplan.aktiv = True
    z.einst.zeitplan.start_modus = "uhrzeit"
    z.einst.zeitplan.start = "18:00"
    z.einst.zeitplan.ende = "22:00"
    z.zustand.licht.an = False
    zp = Zeitplan(z)

    await zp.tick(_zeit("17:00"))
    assert not z.zustand.licht.an
    await zp.tick(_zeit("18:00"))
    assert z.zustand.licht.an and z.zustand.ambiente_an and z.zustand.ereignisse_an

    # von Hand aus -> bleibt aus, bis zum nächsten Wechsel
    await z.show(False)
    await zp.tick(_zeit("19:00"))
    assert not z.zustand.licht.an

    await z.show(True)
    await zp.tick(_zeit("22:00"))
    assert not z.zustand.licht.an and not z.zustand.ambiente_an


async def test_zeitplan_ueber_mitternacht_und_neustart_im_fenster():
    z = Zentrale()
    z.einst.zeitplan.aktiv = True
    z.einst.zeitplan.start_modus = "uhrzeit"
    z.einst.zeitplan.start = "20:00"
    z.einst.zeitplan.ende = "01:00"
    z.zustand.licht.an = False
    zp = Zeitplan(z)
    # Server startet um 00:30 -> Show muss an
    await zp.tick(_zeit("00:30", 1))
    assert z.zustand.licht.an
    await zp.tick(_zeit("01:00", 1))
    assert not z.zustand.licht.an


async def test_ruhezeit_stumm_setzt_lautstaerke_null():
    z = Zentrale()
    z.einst.ruhezeit.aktiv = True
    z.einst.ruhezeit.ab = "21:00"
    z.einst.ruhezeit.bis = "07:00"
    z.einst.ruhezeit.modus = "stumm"
    zp = Zeitplan(z)
    await zp.tick(_zeit("20:00"))
    assert not z.ruhezeit_aktiv and z.ton_effektiv().master == z.einst.ton.master
    await zp.tick(_zeit("21:30"))
    assert z.ruhezeit_aktiv and z.ton_effektiv().master == 0 and z.stumm
