import numpy as np
import pytest

from backend.photometry import aperture_flux

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
