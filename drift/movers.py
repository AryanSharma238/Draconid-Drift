"""Which stars in this field visibly moved between AllWISE (2010) and SPHEREx (2025-26)?

Method (no guessing from pictures):
1. Ask Gaia DR3 for every star in and around the field whose proper motion would carry it at least
   MIN_SHIFT_PX SPHEREx pixels between the AllWISE epoch and the SPHEREx frames.
2. Move each one to the SPHEREx epoch with its Gaia proper motion and check that SPHEREx really sees a
   source there (local peak >= MIN_SNR in the deep stack), so we only draw movers SPHEREx confirms.
3. Keep the brightest few that land inside the field.
The target itself (e.g. a brown dwarf Gaia cannot see) is added from its catalogue motion when SPHEREx sees it.
"""
from __future__ import annotations

import functools
import io

import numpy as np

from . import SPHEREX_PIXEL_ARCSEC
from .targets import mjd_to_year, propagate

WISE_EPOCH = 2010.5          # AllWISE mid-mission (Jan 2010 - Feb 2011)
GAIA_EPOCH = 2016.0
MIN_SHIFT_PX = 1.0           # motion between 2010.5 and the SPHEREx frames, in SPHEREx pixels
MIN_SNR = 5.0
MAX_OFFSET_PX = 0.75
MAX_MOVERS = 8
GAIA_TAP = "https://gea.esac.esa.int/tap-server/tap/sync"


@functools.lru_cache(maxsize=256)
def _gaia_fast_stars(ra, dec, radius_deg, pm_min_masyr, timeout=8.0):
    """Gaia DR3 stars with total proper motion >= pm_min_masyr inside a cone. [] if Gaia is unreachable."""
    import httpx
    q = (f"SELECT source_id, ra, dec, pmra, pmdec, phot_g_mean_mag FROM gaiadr3.gaia_source "
         f"WHERE 1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra:.6f}, {dec:.6f}, {radius_deg:.5f})) "
         f"AND pmra IS NOT NULL AND pmra*pmra + pmdec*pmdec >= {pm_min_masyr ** 2:.1f}")
    try:
        r = httpx.get(GAIA_TAP, params=dict(REQUEST="doQuery", LANG="ADQL", FORMAT="csv", QUERY=q), timeout=timeout)
        r.raise_for_status()
        rows = np.genfromtxt(io.StringIO(r.text), delimiter=",", names=True, dtype=None, encoding="utf-8")
    except Exception:
        return ()
    rows = np.atleast_1d(rows)
    if rows.size == 0 or rows.dtype.names is None:
        return ()
    return tuple(dict(name=f"Gaia DR3 {int(x['source_id'])}", ra=float(x["ra"]), dec=float(x["dec"]),
                      pmra=float(x["pmra"]), pmdec=float(x["pmdec"]),
                      g=float(x["phot_g_mean_mag"]) if np.isfinite(x["phot_g_mean_mag"]) else 99.0,
                      epoch=GAIA_EPOCH) for x in rows)


@functools.lru_cache(maxsize=256)
def simbad_fast_stars(ra, dec, radius_deg, pm_min_masyr, timeout=6.0):
    """Same as the Gaia query, from SIMBAD's Harvard mirror (usually answers in about a second). None if unreachable."""
    from .naming import _tap
    rows = _tap(f"SELECT main_id, ra, dec, pmra, pmdec FROM basic WHERE 1=CONTAINS(POINT('ICRS',ra,dec),"
                f"CIRCLE('ICRS',{ra:.6f},{dec:.6f},{radius_deg:.5f})) AND pmra*pmra + pmdec*pmdec >= {pm_min_masyr ** 2:.1f}", timeout)
    if rows is None:
        return None
    out = []
    for r in rows:
        try:
            out.append(dict(name=str(r["main_id"]).strip('"'), ra=float(r["ra"]), dec=float(r["dec"]), pmra=float(r["pmra"]),
                            pmdec=float(r["pmdec"]), g=99.0, epoch=2000.0))
        except (ValueError, TypeError, KeyError):
            pass
    return tuple(out)


def gaia_fast_stars(ra, dec, radius_deg, pm_min_masyr, wait=1.5):
    """Fast stars from Gaia and SIMBAD asked at once; Gaia is used if it answers within `wait` seconds, SIMBAD fills the rest."""
    from concurrent.futures import ThreadPoolExecutor, wait as fwait
    ex = ThreadPoolExecutor(2)
    fg = ex.submit(_gaia_fast_stars, ra, dec, radius_deg, pm_min_masyr)
    fs = ex.submit(simbad_fast_stars, ra, dec, radius_deg, pm_min_masyr)
    fwait([fs], timeout=7.0)
    fwait([fg], timeout=wait)
    ex.shutdown(wait=False)
    gaia = fg.result() if fg.done() else ()
    sim = (fs.result() if fs.done() else None) or ()
    out = list(gaia)
    for s in sim:                      # SIMBAD stars Gaia did not return (or all of them, when Gaia was slow)
        d0 = s["dec"] + s["pmdec"] * (GAIA_EPOCH - 2000) / 3.6e6
        a0 = s["ra"] + s["pmra"] * (GAIA_EPOCH - 2000) / 3.6e6 / max(np.cos(np.radians(d0)), 1e-6)
        if not any(abs(g["dec"] - d0) * 3600 < 3 and abs(g["ra"] - a0) * np.cos(np.radians(d0)) * 3600 < 3 for g in gaia):
            out.append(s)
    return tuple(out)


def deep_stack(cube):
    """Noise-weighted mean of all frames and its variance (sources moving <1 px a year stay sharp)."""
    ok = np.isfinite(cube.cube) & np.isfinite(cube.var) & (cube.var > 0)
    w = np.where(ok, 1.0 / np.where(ok, cube.var, 1.0), 0.0)
    s = (np.where(ok, cube.cube, 0.0) * w).sum(0)
    ws = w.sum(0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(ws > 0, s / ws, np.nan), np.where(ws > 0, 1.0 / ws, np.nan)


def measure(img, var, x, y, r_ap=1.5, r_in=4.0, r_out=7.0, half=2):
    """Point source at (x, y)? Returns (flux S/N from the pixel variances, centroid offset in px)."""
    ny, nx = img.shape
    xi, yi = int(round(x)), int(round(y))
    if not (3 <= xi < nx - 3 and 3 <= yi < ny - 3):
        return 0.0, 99.0
    y0, y1, x0, x1 = max(0, yi - 8), min(ny, yi + 9), max(0, xi - 8), min(nx, xi + 9)
    sub, sv = img[y0:y1, x0:x1], var[y0:y1, x0:x1]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    rr = np.hypot(xx - x, yy - y)
    ann = sub[(rr >= r_in) & (rr <= r_out) & np.isfinite(sub)]
    if ann.size < 8:
        return 0.0, 99.0
    bg = float(np.median(ann))
    core = rr <= r_ap
    if (~np.isfinite(sub[core])).mean() > 0.3:           # saturated star: its core is masked, measure the wings
        r_ap, half = 3.0, 3
    ap = (rr <= r_ap) & np.isfinite(sub) & np.isfinite(sv)
    if ap.sum() < 3:
        return 0.0, 99.0
    flux = float((sub[ap] - bg).sum())
    noise = float(np.sqrt(sv[ap].sum() + ap.sum() ** 2 * np.var(ann) / ann.size))
    box = (np.abs(xx - xi) <= half) & (np.abs(yy - yi) <= half) & np.isfinite(sub)
    wgt = np.clip(sub[box] - bg, 0, None)
    if wgt.sum() <= 0:
        return flux / noise if noise > 0 else 0.0, 99.0
    cx, cy = (wgt * xx[box]).sum() / wgt.sum(), (wgt * yy[box]).sum() / wgt.sum()
    return (flux / noise if noise > 0 else 0.0), float(np.hypot(cx - x, cy - y))


def confirmed(img, var, x, y, x0, y0):
    """SPHEREx confirms a mover when a point source sits at its SPHEREx-epoch position (S/N >= MIN_SNR,
    centred within MAX_OFFSET_PX) and its 2010 spot, when far enough away and in the field, is much fainter."""
    snr, off = measure(img, var, x, y)
    if snr < MIN_SNR or off > MAX_OFFSET_PX:
        return False, snr
    if np.hypot(x - x0, y - y0) >= 3.0:
        snr0, _ = measure(img, var, x0, y0)
        if snr0 > 0.35 * snr:                                # something just as bright never left: not this mover
            return False, snr
    return True, snr


def find_movers(cube, target=None, wise_epoch=WISE_EPOCH):
    """cube: drift.clean.Cube (flattened). target: dict(name, ra, dec, pmra, pmdec, epoch) or None.
    Returns a list of dicts with the 2010 and SPHEREx-epoch sky positions, sorted brightest first."""
    n, ny, nx = cube.cube.shape
    wcs = cube.wcs
    t_spx = float(np.asarray(mjd_to_year(np.nanmean(cube.mjd))))
    dt = max(t_spx - wise_epoch, 1.0)
    pm_min = MIN_SHIFT_PX * SPHEREX_PIXEL_ARCSEC * 1000.0 / dt          # mas/yr
    ra_c, dec_c = (float(v) for v in wcs.wcs.crval)
    half = 0.5 * max(nx, ny) * SPHEREX_PIXEL_ARCSEC / 3600.0
    reach = 12000.0 * (GAIA_EPOCH - 2000 + 30) / 3.6e6                    # how far a 12"/yr star moves in ~30 yr, deg
    stars = list(gaia_fast_stars(round(ra_c, 4), round(dec_c, 4), round(half * 1.42 + min(reach, 0.1), 4), round(pm_min, 1)))
    if target and np.hypot(target.get("pmra", 0), target.get("pmdec", 0)) >= pm_min:
        stars.insert(0, dict(target, g=-1.0))                             # always considered first, keeps its name
    stack, svar = deep_stack(cube)
    out, seen = [], []
    for s in stars:
        r1, d1 = map(float, propagate(s["ra"], s["dec"], s["pmra"], s["pmdec"], s["epoch"], t_spx))
        x, y = map(float, wcs.world_to_pixel_values(r1, d1))
        if not (1 <= x < nx - 1 and 1 <= y < ny - 1):
            continue
        if any(np.hypot(x - a, y - b) < 2.0 for a, b in seen):            # the target and its own Gaia entry
            continue
        r0, d0 = map(float, propagate(s["ra"], s["dec"], s["pmra"], s["pmdec"], s["epoch"], wise_epoch))
        x0, y0 = map(float, wcs.world_to_pixel_values(r0, d0))
        ok, snr = confirmed(stack, svar, x, y, x0, y0)
        if not ok:
            continue
        seen.append((x, y))
        out.append(dict(name=s["name"], g=round(s["g"], 2), snr=round(snr, 1),
                        pmra=round(s["pmra"], 1), pmdec=round(s["pmdec"], 1),
                        ra0=float(r0), dec0=float(d0), epoch0=wise_epoch,
                        shift_px=round(float(np.hypot(x - x0, y - y0)), 2)))
    out.sort(key=lambda m: m["g"])
    return out[:MAX_MOVERS]
