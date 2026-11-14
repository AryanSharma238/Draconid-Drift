import numpy as np
import pytest

from backend.models import make_grid
from tests.fixtures import make_fake_cutout


def test_make_grid_snaps_and_odd_npix():
    g = make_grid(133.79513, -7.24515, 0.1)
    assert g.npix % 2 == 1
    assert g.ra == pytest.approx(133.795, abs=1e-9)
    assert g.dec == pytest.approx(-7.245, abs=1e-9)


def test_grid_centre_is_centre_pixel():
    # Why: the crosshair contract C4 puts the input position at ((npix-1)/2, (npix-1)/2).
    g = make_grid(133.79513, -7.24515, 0.1)
    x, y = g.wcs().world_to_pixel_values(g.ra, g.dec)
    c = (g.npix - 1) / 2
    assert abs(x - c) < 1e-9
    assert abs(y - c) < 1e-9


def test_fake_cutout_star_lands_on_its_wcs_pixel():
    ra, dec = 133.795, -7.245
    star_ra, star_dec = ra + 0.004, dec + 0.003
    hdul = make_fake_cutout(ra, dec, stars=[(star_ra, star_dec, 5000.0)])
    from astropy.wcs import WCS

    img = hdul["IMAGE"].data
    sx, sy = WCS(hdul["IMAGE"].header).world_to_pixel_values(star_ra, star_dec)
    py, px = np.unravel_index(np.argmax(img), img.shape)
    assert abs(px - sx) < 1 and abs(py - sy) < 1
