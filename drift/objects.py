"""Find the objects worth classifying in a cube: the brightest source, plus every source at least 75% as bright.

Works on the noise-weighted stack of all frames (a mover smears, but the objects to classify are the bright, nearly
static ones). Flux is a background-subtracted aperture sum from backend.photometry, so the 75% rule compares the same
quantity for every source. Returns sky positions the classifier can ask the EC2 backend for spectra at.
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import maximum_filter

from backend import config
from backend.photometry import aperture_flux

REL_BRIGHT = 0.75      # a source is kept if its flux >= REL_BRIGHT * brightest flux
MAX_SOURCES = 6
PEAK_NSIG = 6.0
EDGE_PX = 4            # sources closer to the edge than this have a clipped background annulus
MIN_SEP_PX = 4.0       # two peaks closer than this are one blended source (SPHEREx PSF ~1.5 px FWHM)


def stack(cube) -> tuple[np.ndarray, np.ndarray]:
    """Inverse-variance mean image and its variance."""
    d, v = cube.cube, cube.var
    w = np.where(np.isfinite(d) & np.isfinite(v) & (v > 0), 1.0 / np.where(v > 0, v, 1.0), 0.0)
    ws = w.sum(0)
    with np.errstate(invalid="ignore", divide="ignore"):
        img = np.where(ws > 0, (np.nan_to_num(d) * w).sum(0) / ws, np.nan)
        var = np.where(ws > 0, 1.0 / ws, np.nan)
    return img, var


def _robust_sigma(a: np.ndarray) -> float:
    f = a[np.isfinite(a)]
    return float(1.4826 * np.median(np.abs(f - np.median(f)))) if f.size else float("nan")


def detect(cube, rel: float = REL_BRIGHT, max_sources: int = MAX_SOURCES) -> list[dict]:
    """[{x, y, ra, dec, flux, err, rel, rank}] brightest first; every entry has flux >= rel * brightest."""
    img, var = stack(cube)
    if not np.isfinite(img).any():
        return []
    med = float(np.nanmedian(img))
    sig = _robust_sigma(img)
    if not np.isfinite(sig) or sig <= 0:
        return []
    ny, nx = img.shape
    filled = np.where(np.isfinite(img), img, med)
    peak = (filled == maximum_filter(filled, size=5)) & (filled > med + PEAK_NSIG * sig)
    ys, xs = np.nonzero(peak)
    cand = []
    for y, x in zip(ys, xs):
        if min(x, y, nx - 1 - x, ny - 1 - y) < EDGE_PX:
            continue
        f, e = aperture_flux(img, var, float(x), float(y), config.SPECTRUM_APERTURE_R,
                             config.SPECTRUM_ANNULUS_R_IN, config.SPECTRUM_ANNULUS_R_OUT)
        if np.isfinite(f) and f > 0:
            cand.append((float(f), float(e), int(x), int(y)))
    cand.sort(reverse=True)
    kept: list[tuple] = []
    for c in cand:                       # drop faint peaks that sit on the wings of a brighter one
        if all(np.hypot(c[2] - k[2], c[3] - k[3]) >= MIN_SEP_PX for k in kept):
            kept.append(c)
    sat = _saturated(img)
    sat = [s for s in sat if all(np.hypot(s[0] - k[2], s[1] - k[3]) >= MIN_SEP_PX for k in kept)]
    if not kept and not sat:
        return []
    out = []
    if sat:
        # A saturated core is NaN in the delivered pixels, so its flux is unmeasurable but exceeds any measurable
        # core: such sources count as the brightest (rel = 1). Measurable sources cannot be compared to them, so
        # they are left out rather than ranked against a number we do not have.
        for rank, (x, y) in enumerate(sat[:max_sources]):
            ra, dec = (float(v) for v in cube.wcs.pixel_to_world_values(x, y))
            out.append(dict(x=float(x), y=float(y), ra=ra % 360.0, dec=dec, flux=float("inf"), err=float("nan"),
                            rel=1.0, rank=rank, saturated=True))
        return out
    top = kept[0][0]
    for rank, (f, e, x, y) in enumerate(kept):
        if f < rel * top or rank >= max_sources:
            break
        ra, dec = (float(v) for v in cube.wcs.pixel_to_world_values(x, y))
        out.append(dict(x=x, y=y, ra=ra % 360.0, dec=dec, flux=f, err=e, rel=f / top, rank=rank, saturated=False))
    return out


def _saturated(img: np.ndarray) -> list[tuple[float, float]]:
    """Centres of small NaN holes inside the image (a bright star's saturated core), largest hole first.
    Holes touching the edge are frame/footprint edges, not stars; big ones are masked regions."""
    from scipy.ndimage import center_of_mass, label

    lab, n = label(~np.isfinite(img))
    ny, nx = img.shape
    out = []
    for i in range(1, n + 1):
        m = lab == i
        size = int(m.sum())
        ys, xs = np.nonzero(m)
        if size < 3 or size > 60 or min(xs.min(), ys.min(), nx - 1 - xs.max(), ny - 1 - ys.max()) < EDGE_PX:
            continue
        cy, cx = center_of_mass(m)
        out.append((size, float(cx), float(cy)))
    out.sort(reverse=True)
    return [(x, y) for _, x, y in out]
