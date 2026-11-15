"""All six SPHEREx detector ranges for one field: a looping image set per range, and a spectrum for each listed object.

SPHEREx splits 0.75-5 um over six detectors; at one sky position every exposure samples one wavelength. So the
frames of all six detectors at a spot, measured with the same aperture, are that spot's spectrum.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import numpy as np
from astropy.time import Time

from . import SPHEREX_PIXEL_ARCSEC
from .clean import flatten
from .render import stretch_params, to_dark_jpeg_b64

DETS = (1, 2, 3, 4, 5, 6)
BAND_FRAMES = 24          # exposures fetched per extra detector (evenly spread in time)
UJY_PER_SUM = (SPHEREX_PIXEL_ARCSEC / 206264.806) ** 2 * 1e12   # MJy/sr summed over pixels -> microjansky
NOTE = {1: "starlight; hot stars shine brightest", 2: "water vapour dips in cool stars",
        3: "sees through dust", 4: "water ice and glowing dust", 5: "carbon dioxide ice", 6: "carbon monoxide ice, warm dust"}

# (label, centre, half-width, left side window, right side window, sign): sign -1 = absorption dip, +1 = emission bump
FEATURES = [
    ("H₂O", 1.40, 0.07, (1.22, 1.30), (1.55, 1.65), -1),
    ("H₂O", 1.88, 0.08, (1.68, 1.76), (2.05, 2.15), -1),
    ("CO", 2.34, 0.06, (2.15, 2.25), (2.45, 2.55), -1),
    ("H₂O ice", 3.05, 0.13, (2.55, 2.75), (3.55, 3.75), -1),
    ("CH₄", 3.32, 0.07, (3.05, 3.15), (3.55, 3.70), -1),
    ("PAH", 3.29, 0.05, (3.10, 3.20), (3.40, 3.50), +1),
    ("CO₂", 4.27, 0.06, (4.05, 4.15), (4.40, 4.50), -1),
    ("CO", 4.67, 0.08, (4.45, 4.55), (4.85, 4.98), -1),
]


def _ap(img, var, x, y, r=2.0, rin=4.0, rout=6.0):
    yy, xx = np.indices(img.shape)
    d2 = (xx - x) ** 2 + (yy - y) ** 2
    ap, ann = d2 <= r * r, (d2 >= rin * rin) & (d2 <= rout * rout)
    if not np.isfinite(img[ap]).all():
        return np.nan, np.nan
    bk = img[ann & np.isfinite(img)]
    if bk.size < 5:
        return np.nan, np.nan
    n = int(ap.sum())
    f = img[ap].sum() - np.median(bk) * n
    e = np.sqrt(np.nansum(var[ap])) if var is not None else 1.4826 * np.median(np.abs(bk - np.median(bk))) * np.sqrt(n)
    return float(f) * UJY_PER_SUM, float(e) * UJY_PER_SUM


RINGS = ((2.0, 4.0, 6.0, 8.0), (3.5, 5.5, 8.0, 10.0), (5.0, 7.0, 9.5, 11.5))


def _ring(img, var, x, y, rr=RINGS[0]):
    """Flux in the star's wings for saturated stars whose core is masked: the shape of the spectrum survives,
    the absolute level is lower than the true total. rr = (ring in, ring out, background in, background out) px."""
    yy, xx = np.indices(img.shape)
    d2 = (xx - x) ** 2 + (yy - y) ** 2
    ring, ann = (d2 >= rr[0] ** 2) & (d2 <= rr[1] ** 2), (d2 >= rr[2] ** 2) & (d2 <= rr[3] ** 2)
    ok = ring & np.isfinite(img)
    bk = img[ann & np.isfinite(img)]
    if ok.sum() < 0.6 * ring.sum() or bk.size < 5:
        return np.nan, np.nan
    f = (img[ok].sum() - np.median(bk) * ok.sum()) * ring.sum() / ok.sum()
    return float(f) * UJY_PER_SUM, float(np.sqrt(np.nansum(var[ok]))) * UJY_PER_SUM


def pixel_track(cube, ra, dec, pmra=0.0, pmdec=0.0, epoch=2026.0):
    """Array pixel of a (possibly moving) source in each frame: [(x, y)]."""
    from .targets import mjd_to_year
    yrs = np.asarray(mjd_to_year(cube.mjd), float)
    d = dec + pmdec * (yrs - epoch) / 3.6e6
    a = ra + pmra * (yrs - epoch) / 3.6e6 / np.maximum(np.cos(np.radians(d)), 1e-6)
    x, y = cube.wcs.world_to_pixel_values(a, d)
    return list(zip(np.atleast_1d(x).astype(float), np.atleast_1d(y).astype(float)))


def points(cube, track, wings=False):
    """wings: False for aperture photometry, or a ring tuple from RINGS."""
    """[(wl, flux_uJy, err_uJy)] for every usable frame, measured where the source is in that frame."""
    out = []
    ny, nx = cube.cube.shape[1:]
    for i in range(cube.n):
        x, y = track[i]
        m = (int(wings[3]) + 1) if wings else 7          # the background ring must fit inside the image
        if not np.isfinite(cube.wl[i]) or not (m <= x < nx - m and m <= y < ny - m):
            continue
        f, e = _ring(cube.cube[i], cube.var[i], x, y, wings) if wings else _ap(cube.cube[i], cube.var[i], x, y)
        if np.isfinite(f) and np.isfinite(e) and e > 0:
            out.append((float(cube.wl[i]), f, e))
    return out


def smooth(pts, nbins=36):
    """Median in log-spaced wavelength bins: [(wl, flux, lo, hi)]; bins with fewer than 2 points are left out."""
    if len(pts) < 4:
        return []
    a = np.array(pts)
    edges = np.geomspace(0.74, 5.02, nbins + 1)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (a[:, 0] >= lo) & (a[:, 0] < hi)
        if m.sum() < 2:
            continue
        f = a[m, 1]
        med = float(np.median(f))
        err = max(float(1.2533 * np.median(a[m, 2]) / np.sqrt(m.sum())), float(1.4826 * np.median(np.abs(f - med)) / np.sqrt(m.sum())))
        out.append((float(np.sqrt(lo * hi)), med, med - err, med + err))
    return out


def features(pts):
    """Features actually present: a dip (or bump) below (above) the straight line between its two side windows,
    at least 8% deep and 3 sigma. Labels nothing that is not measured."""
    if len(pts) < 8:
        return []
    a = np.array(pts)
    found = []
    for label, c, hw, L, R, sign in FEATURES:
        sel = lambda w: (a[:, 0] >= w[0]) & (a[:, 0] <= w[1])
        mi, ml, mr = (a[:, 0] >= c - hw) & (a[:, 0] <= c + hw), sel(L), sel(R)
        if mi.sum() < 2 or ml.sum() < 2 or mr.sum() < 2:
            continue
        fl, fr, fi = np.median(a[ml, 1]), np.median(a[mr, 1]), np.median(a[mi, 1])
        wl_, wr = np.mean(L), np.mean(R)
        cont = fl + (fr - fl) * (c - wl_) / (wr - wl_)
        if not cont > 0:
            continue
        depth = sign * (fi - cont) / cont
        noise = np.sqrt((1.2533 * np.median(a[mi, 2])) ** 2 / mi.sum() + (1.2533 * np.median(a[ml, 2])) ** 2 / ml.sum() / 2
                        + (1.2533 * np.median(a[mr, 2])) ** 2 / mr.sum() / 2) / cont
        if depth >= 0.08 and depth / max(noise, 1e-9) >= 3:
            found.append(dict(label=label, wl=c, depth=round(float(depth), 3), kind="dip" if sign < 0 else "bump"))
    labels = {f["label"] for f in found}               # CH4 and PAH share 3.3 um: keep the stronger reading
    if "CH₄" in labels and "PAH" in labels:
        found = [f for f in found if f["label"] != min(("CH₄", "PAH"), key=lambda k: next(x["depth"] for x in found if x["label"] == k))]
    return found


def spectrum(cubes, ra, dec, pmra=0.0, pmdec=0.0, epoch=2026.0):
    """Spectrum of a (possibly moving) source over all loaded detectors."""
    def measure(wings):
        per = []
        for cube in cubes.values():
            tr = pixel_track(cube, ra, dec, pmra, pmdec, epoch)
            inside = sum(7 <= x < cube.cube.shape[2] - 7 and 7 <= y < cube.cube.shape[1] - 7 for x, y in tr)
            per.append((points(cube, tr, wings), inside))
        return per
    per = measure(False)
    pts = [p for ps, _ in per for p in ps]
    # a saturated core in any detector: measure every detector in the wings, so the whole spectrum shares one scale
    wings = any(n >= 4 and len(ps) < 0.5 * n for ps, n in per)
    if wings:                                     # the smallest ring that is clean in most frames of every detector
        best = None
        for rr in RINGS:
            m = measure(rr)
            if all(len(ps) >= 0.5 * n for ps, n in m if n >= 4):
                best = m
                break
            if best is None or sum(len(ps) for ps, _ in m) > sum(len(ps) for ps, _ in best):
                best = m
        pts = [p for ps, _ in best for p in ps]
    pts.sort()
    return dict(wings=bool(wings), pts=[[round(w, 4), round(f, 2), round(e, 2)] for w, f, e in pts],
                smooth=[[round(v, 4) for v in s] for s in smooth(pts)], features=features(pts), unit="µJy")


def band_frames(cube, n_show=12, out_px=360):
    """A small looping set for one detector: frames in time order, one shared stretch."""
    from .stage_data import smoothed_frames
    sm = smoothed_frames(cube)
    order = np.argsort(cube.mjd)
    if len(order) > n_show:
        order = order[np.linspace(0, len(order) - 1, n_show).round().astype(int)]
    p = stretch_params([sm[i] for i in order])
    return dict(frames=[to_dark_jpeg_b64(sm[i], p, out_px=out_px) for i in order],
                dates=[Time(float(cube.mjd[i]), format="mjd").iso[:10] for i in order])


def all_bands(spec, base_cube=None, targets=(), max_frames=BAND_FRAMES):
    """Load the other detectors for `spec`'s field and return bands + spectra.

    targets: [(id, ra, dec[, pmra, pmdec, epoch])] what to measure (the target and each listed object)."""
    from . import pipeline
    base_det = int(str(spec.detector)[-1])

    def one(d):
        if d == base_det and base_cube is not None:
            return d, base_cube
        s = pipeline.Spec(spec.name, spec.ra, spec.dec, spec.pmra, spec.pmdec, spec.epoch, spec.fov_arcsec,
                          f"SPHEREx-D{d}", f"{spec.key}_b{d}")
        try:
            c, _ = pipeline.load_frames(s, max_frames=max_frames)
            return d, flatten(c)
        except Exception:
            return d, None

    with ThreadPoolExecutor(6) as ex:
        cubes = {d: c for d, c in ex.map(one, DETS) if c is not None and c.n}
    bands = []
    for d in DETS:
        c = cubes.get(d)
        if c is None:
            bands.append(dict(det=d, ok=False, note=NOTE[d]))
            continue
        wl = c.wl[np.isfinite(c.wl)]
        bands.append(dict(det=d, ok=True, note=NOTE[d], wl_lo=round(float(wl.min()), 2) if wl.size else None,
                          wl_hi=round(float(wl.max()), 2) if wl.size else None, n=int(c.n), **band_frames(c)))
    spectra = {str(t[0]): spectrum(cubes, *t[1:]) for t in targets}
    return dict(bands=bands, spectra=spectra)
