import numpy as np
import pytest

from backend.photometry import aperture_flux, ring_flux

N = 41
R, R_IN, R_OUT = 3.0, 6.0, 9.0  # r=3 sigma-1 star: ~98.9% of the flux is inside


def gaussian(x0: float, y0: float, flux: float = 1000.0, sigma: float = 1.0) -> np.ndarray:
    yy, xx = np.indices((N, N))
    g = np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * sigma**2))
    return flux * g / g.sum()


def test_gaussian_star_recovers_98_percent():
    img = gaussian(20.0, 20.0) + 5.0
    flux, err = aperture_flux(img, np.full_like(img, 0.01), 20.0, 20.0, R, R_IN, R_OUT)
    assert flux >= 0.98 * 1000.0
    assert flux <= 1001.0  # background was subtracted, not added
    assert err == pytest.approx(np.sqrt(0.01 * np.pi * 9), rel=0.2)


def test_pure_background_is_zero_within_3_sigma():
    rng = np.random.default_rng(1)
    sig = 2.0
    img = 10.0 + rng.normal(0, sig, (N, N))
    flux, err = aperture_flux(img, np.full_like(img, sig**2), 20.0, 20.0, R, R_IN, R_OUT)
    assert abs(flux) < 3 * err


def test_nan_in_aperture_is_nan_not_biased():
    img = gaussian(20.0, 20.0)
    img[20, 21] = np.nan
    flux, err = aperture_flux(img, None, 20.0, 20.0, R, R_IN, R_OUT)
    assert np.isnan(flux) and np.isnan(err)


def test_moving_aperture_recovers_95_percent_of_static_flux():
    # Why: a fixed aperture loses flux on fast movers and biases the E7 spectrum.
    flux_true = 1000.0
    sx, sy = 20.3, 20.6
    static, _ = aperture_flux(gaussian(sx, sy, flux_true), None, sx, sy, R, R_IN, R_OUT)
    v = (1.7, 0.4)  # px per frame step: a fast mover
    moving, fixed = [], []
    for k in range(8):
        x, y = 8.0 + v[0] * k, 14.0 + v[1] * k
        img = gaussian(x, y, flux_true)
        moving.append(aperture_flux(img, None, x, y, R, R_IN, R_OUT)[0])
        fixed.append(aperture_flux(img, None, 8.0, 14.0, R, R_IN, R_OUT)[0])
    assert min(moving) >= 0.95 * static
    assert min(fixed) < 0.5 * static  # the fixed aperture really would lose it


def _saturated(flux: float, hole: float = 2.5, sigma: float = 1.6) -> np.ndarray:
    """A bright star whose core (r < hole) is NaN, as in R Dra's delivered data."""
    img = gaussian(20.0, 20.0, flux, sigma)
    yy, xx = np.indices((N, N))
    img[(xx - 20.0) ** 2 + (yy - 20.0) ** 2 < hole**2] = np.nan
    return img


def test_ring_flux_works_where_aperture_flux_cannot_and_scales_with_the_star():
    """Why: R Dra's core is NaN, so aperture_flux returns nothing for the headline variable. The wing ring must still
    give a flux that is PROPORTIONAL to the star (that is what a light curve needs), within a percent."""
    img1, img2 = _saturated(1000.0), _saturated(2000.0)
    assert np.isnan(aperture_flux(img1, None, 20.0, 20.0, 2.0, 4.0, 6.0)[0])  # the failure being fixed
    f1, e1 = ring_flux(img1, np.full_like(img1, 0.01), 20.0, 20.0, 3.0, 6.0)
    f2, _ = ring_flux(img2, np.full_like(img2, 0.01), 20.0, 20.0, 3.0, 6.0)
    assert np.isfinite(f1) and e1 > 0
    assert f2 / f1 == pytest.approx(2.0, rel=0.01)


def test_ring_flux_refuses_a_clipped_or_mostly_masked_ring():
    """Why: a ring cut off by the image edge, or mostly NaN, changes the flux scale between frames; a silent partial
    sum would fake variability, so it must be (nan, nan)."""
    img = _saturated(1000.0)
    assert np.isnan(ring_flux(img, None, 3.0, 20.0, 3.0, 6.0)[0])  # ring leaves the image
    bad = img.copy()
    yy, xx = np.indices((N, N))
    bad[(xx - 20.0) ** 2 + (yy - 20.0) ** 2 < 4.5**2] = np.nan  # blanks most of the 3-6 px ring
    assert np.isnan(ring_flux(bad, None, 20.0, 20.0, 3.0, 6.0)[0])


def test_ring_flux_fills_a_few_masked_pixels_instead_of_dropping_the_frame():
    img = _saturated(1000.0)
    clean, _ = ring_flux(img, None, 20.0, 20.0, 3.0, 6.0)
    img[22, 21] = np.nan  # one bad pixel inside the ring
    assert ring_flux(img, None, 20.0, 20.0, 3.0, 6.0)[0] == pytest.approx(clean, rel=0.03)
