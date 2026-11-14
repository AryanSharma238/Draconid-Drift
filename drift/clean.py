"""Turn raw SPHEREx cutouts into one clean, comparable cube on a common north-up grid.

Per frame: subtract the ZODI model (IMAGE is NOT zodi-subtracted), mask only the flag
bits the docs call unusable (SOURCE/OUTLIER/TRANSIENT are NOT masked: a mover may carry
them), subtract a sigma-clipped sky offset, reproject. Flag bit numbers differ slightly
between IRSA documents; verify against the FLAGS header of your files if results look odd.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from astropy.stats import sigma_clipped_stats
from astropy.wcs import WCS
from reproject import reproject_interp

from . import SPHEREX_PIXEL_ARCSEC

FLAG_BITS = dict(NONFUNC=6, DICHROIC=7, MISSING_DATA=9, HOT=10, COLD=11,
                 NONLINEAR=15, PERSIST=17, SATELLITE_HALO=29)
BAD_FLAG_MASK = sum(1 << b for b in FLAG_BITS.values())


@dataclass
class Cube:
    cube: np.ndarray      # (n, ny, nx) cleaned image, NaN = unusable
    var: np.ndarray       # (n, ny, nx) variance, NaN = unusable
    mjd: np.ndarray       # (n,) observation time
    wl: np.ndarray        # (n,) wavelength at field centre (micron)
    wcs: WCS              # common north-up TAN grid
    detector: str
    source: str           # "IRSA" | "CACHE" | "SYNTHETIC"
    n_found: int = 0      # images the TAP query found (before download failures)

    @property
    def n(self):
        return self.cube.shape[0]

    def subset(self, idx):
        idx = np.asarray(idx)
        return Cube(self.cube[idx], self.var[idx], self.mjd[idx], self.wl[idx],
                    self.wcs, self.detector, self.source, self.n_found)


def make_grid(ra, dec, fov_arcsec, scale_arcsec=SPHEREX_PIXEL_ARCSEC):
    npix = int(np.ceil(fov_arcsec / scale_arcsec))
    npix += npix % 2
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [npix / 2 + 0.5, npix / 2 + 0.5]
    w.wcs.cdelt = [-scale_arcsec / 3600.0, scale_arcsec / 3600.0]
    return w, (npix, npix)


def build_cube(raw, ra, dec, fov_arcsec, detector, source="IRSA", n_found=0,
               min_coverage=0.6):
    """raw: list of cutout dicts (irsa.fetch_cutout format). Returns Cube."""
    wcs_out, shape = make_grid(ra, dec, fov_arcsec)
    imgs, vars_, ts, wls = [], [], [], []
    for c in sorted(raw, key=lambda c: c["mjd"]):
        img = np.array(c["image"], float)
        if c.get("zodi") is not None:
            img = img - c["zodi"]
        var = np.array(c["variance"], float)
        bad = (np.asarray(c["flags"]).astype(np.int64) & BAD_FLAG_MASK) != 0
        bad |= ~np.isfinite(img) | ~np.isfinite(var) | (var <= 0)
        if bad.all():
            continue
        _, med, _ = sigma_clipped_stats(img[~bad], sigma=3.0, maxiters=5)
        img = img - med
        img[bad] = np.nan
        var[bad] = np.nan
        r_img, fp = reproject_interp((img, c["wcs"]), wcs_out, shape_out=shape)
        r_var, _ = reproject_interp((var, c["wcs"]), wcs_out, shape_out=shape)
        if np.mean(fp > 0) < min_coverage:          # patch mostly off this detector
            continue
        ok = np.isfinite(r_img) & np.isfinite(r_var) & (r_var > 0)
        r_img[~ok] = np.nan
        r_var[~ok] = np.nan
        imgs.append(r_img); vars_.append(r_var)
        ts.append(c["mjd"]); wls.append(c.get("wavelength_um", np.nan))
    if not imgs:
        raise ValueError("No usable frames after cleaning.")
    return Cube(np.array(imgs), np.array(vars_), np.array(ts), np.array(wls),
                wcs_out, detector, source, n_found)


def select_same_colour(cube: Cube, rel_width=0.04):
    """Keep only the frames inside the densest wavelength window (+/- rel_width/2)."""
    wl = cube.wl
    ok = np.isfinite(wl)
    if ok.sum() < 3:
        return cube
    best = None
    for w0 in wl[ok]:
        sel = ok & (np.abs(wl - w0) <= rel_width * w0 / 2)
        if best is None or sel.sum() > best.sum():
            best = sel
    return cube.subset(np.where(best)[0]) if best.sum() >= 3 else cube


def burst_groups(mjd, gap_days=20.0):
    """Group frames into observing bursts (gap > gap_days starts a new burst)."""
    order = np.argsort(mjd)
    groups, cur = [], [order[0]]
    for a, b in zip(order[:-1], order[1:]):
        if mjd[b] - mjd[a] > gap_days:
            groups.append(np.array(cur)); cur = []
        cur.append(b)
    groups.append(np.array(cur))
    return groups
