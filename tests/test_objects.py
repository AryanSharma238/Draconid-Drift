import numpy as np

from backend.models import make_grid
from drift import classifier, objects
from drift.clean import Cube


def _cube(stars, n=12, size=0.0667, seed=0):
    """Cube of n frames with Gaussian stars [(x, y, peak)] on noise."""
    g = make_grid(150.0, 20.0, size)
    ny = nx = g.npix
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:ny, 0:nx]
    base = np.zeros((ny, nx))
    for x, y, a in stars:
        base += a * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 0.62 ** 2))
    cub = base[None] + rng.normal(0, 0.05, (n, ny, nx))
    var = np.full_like(cub, 0.05 ** 2)
    return Cube(cub, var, np.linspace(60800, 61300, n), np.full(n, 1.0), g.wcs(), "SPHEREx-D4", "TEST", n), g


def test_brightest_and_75pct_rule():
    cube, _ = _cube([(10, 10, 10.0), (30, 12, 8.0), (20, 30, 5.0)])   # 100%, ~80%, ~50%
    got = objects.detect(cube)
    assert [round(o["x"]) for o in got] == [10, 30]                     # the 50% star is excluded
    assert got[0]["rel"] == 1.0 and 0.75 <= got[1]["rel"] < 1.0


def test_nothing_bright_means_no_sources():
    cube, _ = _cube([])
    assert objects.detect(cube) == []


def test_edge_sources_are_dropped():
    cube, _ = _cube([(1, 15, 10.0), (20, 20, 6.0)])
    got = objects.detect(cube)
    assert len(got) == 1 and round(got[0]["x"]) == 20


def test_sky_position_matches_wcs():
    cube, g = _cube([(15, 15, 10.0)])
    o = objects.detect(cube)[0]
    ra, dec = g.wcs().pixel_to_world_values(15, 15)
    assert abs(o["ra"] - ra % 360) < 1e-4 and abs(o["dec"] - dec) < 1e-4


def test_saturated_core_is_found_and_counts_as_brightest():
    cube, _ = _cube([(30, 12, 8.0)])
    cube.cube[:, 18:21, 18:21] = np.nan                     # delivered pixels of a saturated star are NaN
    got = objects.detect(cube)
    assert len(got) == 1 and got[0]["saturated"] and abs(got[0]["x"] - 19) < 1 and abs(got[0]["y"] - 19) < 1


def test_edge_holes_are_not_stars():
    cube, _ = _cube([(30, 12, 8.0)])
    cube.cube[:, 0:3, 0:5] = np.nan
    got = objects.detect(cube)
    assert len(got) == 1 and not got[0]["saturated"]


def _points(f):
    lam = np.geomspace(0.76, 4.9, 300)
    return [dict(lambda_um=float(l), flux=float(f(l)), err=0.01) for l in lam]


def test_features_are_colours_not_brightness():
    a = classifier.features(_points(lambda l: 5.0 * l ** -2))
    b = classifier.features(_points(lambda l: 500.0 * l ** -2))
    assert a.shape == (classifier.N_FEATURES,)
    assert np.allclose(a[:47], b[:47], equal_nan=True)                 # same shape, 100x brighter: identical colours


def test_features_separate_blue_and_red_spectra():
    blue = classifier.features(_points(lambda l: l ** -3))
    red = classifier.features(_points(lambda l: l ** 1.0))
    assert not np.allclose(blue[:24], red[:24], equal_nan=True)


def test_thin_spectrum_is_rejected():
    assert classifier.features([]) is None
    assert classifier.features(_points(lambda l: 1.0)[:3]) is None
