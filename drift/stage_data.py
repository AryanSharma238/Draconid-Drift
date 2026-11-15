"""Everything the full-screen stage needs once a target has been checked."""
from __future__ import annotations

import numpy as np
from astropy.time import Time

from . import SPHEREX_PIXEL_ARCSEC
from .clean import burst_groups
from .movers import find_movers
from .render import flip_y, stretch_params, to_dark_jpeg_b64
from .targets import mjd_to_year

# The archival comparison image is taken at the SPHEREx wavelength: AllWISE W1 (3.4 um) for D4, W2 (4.6 um) for D5/D6.
COMPARE = {
    "SPHEREx-D1": dict(name="2MASS", band="J", um=1.24, url="https://irsa.ipac.caltech.edu/data/hips/CDS/2MASS/J", order=9, fmt="jpg", epoch=2000.0, year="~2000"),
    "SPHEREx-D2": dict(name="2MASS", band="H", um=1.66, url="https://irsa.ipac.caltech.edu/data/hips/CDS/2MASS/H", order=9, fmt="jpg", epoch=2000.0, year="~2000"),
    "SPHEREx-D3": dict(name="2MASS", band="K", um=2.16, url="https://irsa.ipac.caltech.edu/data/hips/CDS/2MASS/K", order=9, fmt="jpg", epoch=2000.0, year="~2000"),
    "SPHEREx-D4": dict(name="AllWISE", band="W1", um=3.4, url="https://irsa.ipac.caltech.edu/data/hips/CDS/AllWISE/W1", order=8, fmt="jpg", epoch=2010.5, year="2010"),
    "SPHEREx-D5": dict(name="AllWISE", band="W2", um=4.6, url="https://irsa.ipac.caltech.edu/data/hips/CDS/AllWISE/W2", order=8, fmt="jpg", epoch=2010.5, year="2010"),
    "SPHEREx-D6": dict(name="AllWISE", band="W2", um=4.6, url="https://irsa.ipac.caltech.edu/data/hips/CDS/AllWISE/W2", order=8, fmt="jpg", epoch=2010.5, year="2010"),
}
DEFAULT_COMPARE = COMPARE["SPHEREx-D4"]
SMOOTH_K = 5          # frames averaged per displayed frame (same observing burst only)


def classify(v):
    """Plain label for what sits at this position."""
    c = v["checks"]
    if c["strength"]["nu"] < -3.0:
        return "UNRELIABLE"
    src = max(c["strength"]["nu"], c["motion"].get("nu_static", 0.0))
    all_ok = c["halves"]["ok"] and c["leave_one_out"]["ok"] and c["flags"]["ok"]
    if src < 6:
        return "NOISE"
    if not v["moving"]:
        return "STAR"
    if v["confidence"] >= 0.99 and all_ok:
        return "MOVER"
    if v["confidence"] >= 0.95:
        return "MOVER?"
    return "SOURCE"


_WORDS = {"UNRELIABLE": ("Unreliable here", "bad"), "NOISE": ("Nothing stands out", "dim"),
          "STAR": ("Not moving", "info"), "MOVER?": ("Moving, unconfirmed", "warn"),
          "MOVER": ("Moving object", "good"), "SOURCE": ("Source, motion unclear", "warn")}


def verdict_summary(v):
    word, tone = _WORDS[classify(v)]
    c = v["checks"]
    conf = (f"> {100 * v['confidence_floor']:.1f}%" if v["confidence_is_bound"] else f"{100 * v['confidence']:.0f}%")
    if tone == "bad":
        conf = "n/a"
    checks = [dict(k="Signal", ok=bool(c["strength"]["nu"] >= 6)),
              dict(k="Motion", ok=bool(c["motion"]["dchi2"] > 0)),
              dict(k="Both halves", ok=bool(c["halves"]["ok"])),
              dict(k="Every frame", ok=bool(c["leave_one_out"]["ok"])),
              dict(k="Clean pixels", ok=bool(c["flags"]["ok"]))]
    return dict(word=word, tone=tone, conf=conf, checks=checks)


def smoothed_frames(cube, k=SMOOTH_K):
    """Each displayed frame = noise-weighted mean of up to k neighbouring frames of the same burst.
    Frames in one burst are taken within days, so nothing real moves between them, while the noise drops ~sqrt(k)."""
    n = cube.n
    out = np.empty_like(cube.cube)
    order = np.argsort(cube.mjd)
    groups = burst_groups(cube.mjd[order])
    for g in groups:
        idx = order[np.asarray(g)]
        for j, i in enumerate(idx):
            lo, hi = max(0, j - k // 2), min(len(idx), j + k // 2 + 1)
            sel = idx[lo:hi]
            d, v = cube.cube[sel], cube.var[sel]
            w = np.where(np.isfinite(d) & np.isfinite(v) & (v > 0), 1.0 / np.where(v > 0, v, 1.0), 0.0)
            s = (np.nan_to_num(d) * w).sum(0)
            ws = w.sum(0)
            with np.errstate(invalid="ignore", divide="ignore"):
                out[i] = np.where(ws > 0, s / ws, np.nan)
    return out


def build_stage(res, name, ra, dec, pmra=0.0, pmdec=0.0, epoch=2000.0, ra0=None, dec0=None, epoch0=None):
    """ra/dec: position shown in the info panel. ra0/dec0/epoch0: catalogue position for the mover search."""
    ra0, dec0, epoch0 = (ra if ra0 is None else ra0), (dec if dec0 is None else dec0), (epoch if epoch0 is None else epoch0)
    cube = res["cube"]
    n, ny, nx = cube.cube.shape
    sm = smoothed_frames(cube)
    p = stretch_params(sm)
    cmp = COMPARE.get(cube.detector, DEFAULT_COMPARE)
    # Prefer frames close to the archival comparison wavelength (bandpass matching), but fall back to all frames
    near = np.isfinite(cube.wl) & (np.abs(cube.wl - cmp["um"]) < 0.6)
    if near.sum() >= 6:
        order = [i for i in np.argsort(cube.mjd) if near[i]]
    else:
        order = list(np.argsort(cube.mjd))
    frames, marks, dates, wl, years = [], [], [], [], []
    for i in order:
        frames.append(to_dark_jpeg_b64(sm[i], p))
        m = res["mark_fn"](cube.mjd[i]) if res.get("mark_fn") else None
        marks.append(None if m is None else [round((m[0] + 0.5) / nx, 4), round((flip_y(m[1], ny) + 0.5) / ny, 4)])
        dates.append(Time(float(cube.mjd[i]), format="mjd").iso[:10])
        years.append(round(float(np.asarray(mjd_to_year(cube.mjd[i]))), 4))
        wl.append(None if not np.isfinite(cube.wl[i]) else round(float(cube.wl[i]), 2))
    ra_c, dec_c = (float(v) for v in cube.wcs.wcs.crval)
    from concurrent.futures import ThreadPoolExecutor
    from .inframe import in_frame
    tgt = dict(name=name, ra=float(ra0), dec=float(dec0), pmra=float(pmra), pmdec=float(pmdec), epoch=float(epoch0))
    with ThreadPoolExecutor(2) as ex:            # Gaia and SIMBAD lookups at the same time
        fm = ex.submit(find_movers, cube, wise_epoch=cmp["epoch"], target=tgt)
        fo = ex.submit(in_frame, cube)
        try:
            movers = fm.result()
        except Exception:
            movers = []
        try:
            objects = fo.result()
        except Exception:
            objects = []
    verdict = verdict_summary(res["verdict"])
    tm = [m for m in movers if m["name"] == name]
    if tm:                                         # long-baseline motion confirmed against the archive image
        moved = tm[0]["shift_px"] * SPHEREX_PIXEL_ARCSEC
        verdict = dict(verdict, word=f"Moved {moved:.0f}\u2033 since {cmp['year'].lstrip('~')}", tone="good")
    return dict(name=name, ra=float(ra), dec=float(dec), pmra=float(pmra), pmdec=float(pmdec), epoch=float(epoch),
                ra_c=float(ra_c), dec_c=float(dec_c), field_deg=float(nx * SPHEREX_PIXEL_ARCSEC / 3600.0),
                nx=int(nx), ny=int(ny), frames=frames, marks=marks, dates=dates, wl=wl, years=years,
                movers=movers, objects=objects,
                detector=cube.detector, compare=cmp,
                verdict=verdict, source=cube.source)
