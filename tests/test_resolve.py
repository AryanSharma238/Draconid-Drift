from pathlib import Path

import httpx
import pytest

from backend import config, resolve

SAMPLE = Path(__file__).parent / "data" / "sesame_barnard.xml"


def test_parse_coords_sexagesimal_c3_required():
    ra, dec = resolve.parse_coords("08h55m10.831s -07d14m42.53s")
    assert ra == pytest.approx(133.79513, abs=1e-5)
    assert dec == pytest.approx(-7.24515, abs=1e-5)


@pytest.mark.parametrize("q", ["133.795, -7.245", "133.795 -7.245", "133.795,-7.245"])
def test_parse_coords_decimal(q):
    assert resolve.parse_coords(q) == (133.795, -7.245)


def test_parse_coords_colon_form():
    ra, dec = resolve.parse_coords("10:00:00 +20:00:00")
    assert (ra, dec) == (pytest.approx(150.0), pytest.approx(20.0))


def test_parse_coords_six_tokens():
    ra, dec = resolve.parse_coords("10 00 00 +20 00 00")
    assert (ra, dec) == (pytest.approx(150.0), pytest.approx(20.0))


@pytest.mark.parametrize("q", ["Barnard", "Mira", "M42", "R Dra", "WISE 0855-0714"])
def test_names_are_not_coords(q):
    # Why: names containing h/m/s/d letters must fall through to Sesame, not raise.
    assert resolve.parse_coords(q) is None


def test_parse_sesame_barnard_sample():
    info = resolve.parse_sesame_xml(SAMPLE.read_text(), "Barnard's Star")
    assert info["name"] == "Barnard's star"
    assert info["epoch"] == 2000.0
    assert info["ra"] == pytest.approx(269.4520770, abs=1e-6)
    assert info["dec"] == pytest.approx(4.6933650, abs=1e-6)
    assert info["pmra"] == pytest.approx(-801.551)
    assert info["pmdec"] == pytest.approx(10362.394)


def test_parse_sesame_missing_pm_is_zero():
    xml = "<Sesame><Target><Resolver><jradeg>10.0</jradeg><jdedeg>-5.0</jdedeg></Resolver></Target></Sesame>"
    info = resolve.parse_sesame_xml(xml, "x")
    assert (info["pmra"], info["pmdec"]) == (0.0, 0.0)


def test_parse_sesame_unresolved_is_none():
    assert resolve.parse_sesame_xml("<Sesame><Target><Resolver/></Target></Sesame>", "zzz") is None


def test_propagate_barnard_lands_on_the_star():
    # Why: the demo crosshair must be on the star, which moved ~4.5 arcmin since J2000.
    info = resolve.parse_sesame_xml(SAMPLE.read_text(), "Barnard's Star")
    ra, dec = resolve.propagate(info["ra"], info["dec"], info["pmra"], info["pmdec"], 2000.0, 2025.9)
    assert ra == pytest.approx(269.446290, abs=2e-4)  # 0.7 arcsec
    assert dec == pytest.approx(4.767917, abs=2e-4)
    assert (dec - info["dec"]) * 3600 == pytest.approx(10.362 * 25.9, abs=2.0)  # ~268 arcsec north


def test_propagate_ra_wraps_and_zero_pm_is_identity():
    assert resolve.propagate(10.0, 20.0, 0.0, 0.0, 2000.0, 2025.9) == (10.0, 20.0)
    ra, _ = resolve.propagate(0.0001, 0.0, -1000.0, 0.0, 2000.0, 2025.9)
    assert 359.9 < ra < 360.0


def test_resolve_payload_shape_for_coords():
    r = resolve.resolve("133.795, -7.245")
    assert set(r) == {"name", "ra", "dec", "epoch", "pmra", "pmdec", "ra_now", "dec_now", "epoch_now"}
    assert r["epoch_now"] == config.SPHEREX_MID_EPOCH
    assert (r["ra_now"], r["dec_now"]) == (r["ra"], r["dec"])  # raw coords: static crosshair


def test_resolve_name_uses_sesame_and_propagates(monkeypatch):
    class R:
        text = SAMPLE.read_text()

        def raise_for_status(self):
            pass

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    r = resolve.resolve("Barnard's Star")
    assert r["dec_now"] == pytest.approx(4.767917, abs=2e-4)
    assert r["pmdec"] == pytest.approx(10362.394)


def test_sesame_failure_falls_back_to_skycoord(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(resolve, "resolve_name", lambda n: (1.0, 2.0))
    assert resolve.resolve_name_pm("x") == {
        "name": "x",
        "ra": 1.0,
        "dec": 2.0,
        "epoch": 2000.0,
        "pmra": 0.0,
        "pmdec": 0.0,
    }


@pytest.mark.network
def test_network_sesame_barnard():
    r = resolve.resolve_name_pm("Barnard's Star")
    assert r["pmdec"] == pytest.approx(10362.394, abs=1.0)


@pytest.mark.network
def test_network_resolve_name_rdra():
    assert resolve.resolve_name("R Dra") is not None
