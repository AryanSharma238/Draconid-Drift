"""Synthetic SPHEREx-like raw cutouts: used for tests and the clearly-labelled DEMO mode.

Stars are fixed; one source moves with a preset's real proper motion. Frames come in
bursts about 6 months apart, are rotated relative to north, and carry zodi, flags and
per-frame wavelength, so the whole pipeline (reprojection included) is exercised.
"""
from __future__ import annotations

import numpy as np
from astropy.wcs import WCS

from . import SPHEREX_PIXEL_ARCSEC as SC
from .targets import Target, mjd_to_year, propagate, target_position, year_to_mjd

MID_EPOCH = 2026.3
PSF_SIGMA = 0.8
NONFUNC = 1 << 6


def _raw_wcs(ra, dec, npix, rot_deg):
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [npix / 2 + 0.5, npix / 2 + 0.5]
    s = SC / 3600.0
    c, sn = np.cos(np.radians(rot_deg)), np.sin(np.radians(rot_deg))
    w.wcs.cd = [[-s * c, s * sn], [s * sn, s * c]]
    return w


def _add_source(img, x, y, peak, sigma=PSF_SIGMA):
    ny, nx = img.shape
    r = int(np.ceil(4 * sigma))
    x0, y0 = int(round(x)), int(round(y))
    ys = slice(max(y0 - r, 0), min(y0 + r + 1, ny)); xs = slice(max(x0 - r, 0), min(x0 + r + 1, nx))
    if ys.start >= ys.stop or xs.start >= xs.stop:
        return
    yy, xx = np.mgrid[ys, xs]
    img[ys, xs] += peak * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma ** 2))


def synthetic_raw(target: Target, detector="SPHEREx-D2", n_bursts=4, per_burst=6, seed=0,
                  mover_peak=None, fov_arcsec=240, n_stars=14, rot_deg=17.0, noise=1.0):
    """Returns (raw_frames, field_ra, field_dec). mover_peak=0 -> noise and stars only."""
    rng = np.random.default_rng(seed)
    fra, fdec = target_position(target, MID_EPOCH)
    npix = int(np.ceil(fov_arcsec * 1.5 / SC))
    if mover_peak is None:
        mover_peak = 1.4 if target.key == "wise0855" else 6.0
    # fixed stars in the field (offsets in arcsec east/north)
    off = rng.uniform(-fov_arcsec * 0.75, fov_arcsec * 0.75, (n_stars * 3, 2))
    off = off[np.hypot(off[:, 0], off[:, 1]) > 10 * SC][:n_stars]   # keep the target's patch clear
    n_stars = len(off)
    flux = np.exp(rng.uniform(np.log(3.0), np.log(60.0), n_stars))
    cosd = np.cos(np.radians(fdec))
    s_ra = fra + off[:, 0] / 3600.0 / cosd
    s_dec = fdec + off[:, 1] / 3600.0
    raw = []
    t0 = 2025.45
    for b in range(n_bursts):
        for k in range(per_burst):
            yr = t0 + 0.5 * b + 0.03 * k + rng.uniform(0, 0.01)
            wcs = _raw_wcs(fra, fdec, npix, rot_deg + 180.0 * (b % 2))
            wl = rng.uniform(1.15, 1.55)
            img = rng.normal(0, noise, (npix, npix))
            var = np.full((npix, npix), noise ** 2)
            xs_, ys_ = wcs.world_to_pixel_values(s_ra, s_dec)
            for xx, yy, f in zip(xs_, ys_, flux):
                _add_source(img, xx, yy, f * (1 + 0.25 * np.sin(wl * 6)))
            if mover_peak > 0:
                mra, mdec = target_position(target, yr)
                mx, my = wcs.world_to_pixel_values(mra, mdec)
                _add_source(img, float(mx), float(my), mover_peak)
            zodi = np.full((npix, npix), 40.0 + 5 * rng.random())
            img = img + zodi + rng.normal(2.0, 0.2)   # un-subtracted sky + offset
            flags = np.zeros((npix, npix), np.int32)
            flags[rng.random((npix, npix)) < 0.01] |= NONFUNC
            raw.append(dict(mjd=float(year_to_mjd(yr)), det=detector, wavelength_um=float(wl),
                            version="synthetic", image=img, flags=flags, variance=var,
                            zodi=zodi, wcs=wcs))
    return raw, fra, fdec
