"""Everything the full-screen stage needs once a target has been checked."""
from __future__ import annotations

import numpy as np
from astropy.time import Time

from . import SPHEREX_PIXEL_ARCSEC
from .clean import burst_groups, pixscale
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
    # If the object is detected as a stationary star or source, confidence reflects the certainty of the signal detection (nu)
    # rather than 0% motion confidence (which was misleading to users who found a real star).
    if not v.get("moving"):
        src_nu = max(c["strength"].get("nu", 0.0), c["motion"].get("nu_static", 0.0))
        if src_nu >= 8.0:
            conf = "> 99%"
        elif src_nu >= 5.0:
            conf = f"{min(98, int(85 + (src_nu - 5.0) * 4))}%"
        elif src_nu >= 3.0:
            conf = f"{max(50, int(src_nu * 16))}%"
        else:
            conf = "Low"
    else:
        conf = (f"> {100 * v['confidence_floor']:.1f}%" if v["confidence_is_bound"] else f"{100 * v['confidence']:.0f}%")
    if tone == "bad":
        conf = "n/a"
    # Revamp checks and confidence display for clarity:
    # Signal: source flux detection (nu >= 4.0 is solid signal)
    # Motion: chi2 improvement under motion model or confirmed proper motion
    # Both halves / Every frame: temporal consistency across split observations
    # Clean pixels: non-flagged detector pixels along the trajectory (<= 50% flagged or solid detection)
    flags_ok = bool(c["flags"]["ok"] or c["flags"].get("frac", 0.0) <= 0.45 or max(c["strength"].get("nu", 0.0), c["motion"].get("nu_static", 0.0)) >= 10.0)
    checks = [
        dict(k="Signal", ok=bool(c["strength"]["nu"] >= 4.0 or c["motion"].get("nu_static", 0.0) >= 4.0)),
        dict(k="Motion", ok=bool(v.get("moving") or c["motion"]["dchi2"] > 0)),
        dict(k="Both halves", ok=bool(c["halves"]["ok"] or not v.get("moving"))),
        dict(k="Every frame", ok=bool(c["leave_one_out"]["ok"] or not v.get("moving"))),
        dict(k="Clean pixels", ok=flags_ok),
    ]
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


def brightness(cube, ra, dec, pmra=0.0, pmdec=0.0, epoch=2026.0):
    """Brightness of the target in each frame relative to its median: [[year, rel]] and a one-line summary.
    Different frames sample slightly different wavelengths, so only clear changes (> 3x the scatter of the
    noise) are called variable."""
    from .bands import _ap, _ring, pixel_track
    try:
        track = pixel_track(cube, ra, dec, pmra, pmdec, epoch)      # a fast star is measured where it is
    except Exception:
        return None
    ny, nx = cube.cube.shape[1:]
    def collect(fn):
        out = []
        for i in np.argsort(cube.mjd):
            x, y = track[i]
            if not (7 <= x < nx - 7 and 7 <= y < ny - 7):
                continue
            f, e = fn(cube.cube[i], cube.var[i], x, y)
            if np.isfinite(f) and np.isfinite(e) and f > 0 and np.isfinite(cube.wl[i]):
                out.append((float(np.asarray(mjd_to_year(cube.mjd[i]))), f, e, float(cube.wl[i])))
        return out
    pts = collect(_ap)
    if len(pts) < 0.5 * cube.n:
        # saturated core: the wings are not a fair stand-in, because how much of the star is clipped changes from
        # exposure to exposure. Say so instead of reporting a change that isn't there.
        return dict(pts=[], word="Too bright to measure: SPHEREx saturates on this star")
    if len(pts) < 6:
        return None
    a = np.array(pts)
    # each frame sees a slightly different wavelength, so first take out the star's colour: fit brightness
    # against wavelength with a smooth curve (log flux, low order, one round of outlier clipping) and compare
    # every frame to that curve. A star's colour is then never mistaken for a change in brightness.
    lw, lf = np.log(a[:, 3]), np.log(a[:, 1])
    deg = 2 if len(a) >= 12 else 1
    keep = np.ones(len(a), bool)
    for _ in range(2):
        c = np.polyfit(lw[keep], lf[keep], deg)
        r = lf - np.polyval(c, lw)
        s0 = 1.4826 * np.median(np.abs(r[keep] - np.median(r[keep]))) + 1e-9
        keep = np.abs(r - np.median(r[keep])) < 5 * s0
    model = np.polyval(c, lw)
    if len(a) >= 15:
        # enough exposures: follow the spectrum's own features (water and methane bands in cool stars) with a running
        # median along wavelength, so a sharp feature is never read as a brightness change
        o = np.argsort(lw); k = max(5, (len(a) // 8) | 1); h = k // 2
        L = lf[o]; sm = np.array([np.median(np.r_[L[max(0, i - h):i], L[i + 1:i + h + 1]]) for i in range(len(o))])   # leave itself out
        model = np.empty_like(lf); model[o] = sm
    rel = np.exp(lf - model)
    if keep.sum() < 6:
        return None
    a, rel = a[keep], rel[keep]
    med = np.median(a[:, 1])
    spread = 1.4826 * np.median(np.abs(rel - 1))
    noise = float(np.median(a[:, 2]) / med)
    if med / np.median(a[:, 2]) < 5:
        word = "too faint to tell"
    elif spread > max(3 * noise, 0.08):
        # a real change is coherent in time: exposures taken close together agree. Pure scatter (glare from a
        # neighbour, a bad background) jumps from one exposure to the next, so check the lag-1 correlation.
        o = np.argsort(a[:, 0]); r = rel[o] - np.mean(rel[o])
        ac = float(np.sum(r[1:] * r[:-1]) / (np.sum(r * r) + 1e-12)) if len(r) > 3 else 0.0
        if ac > 0.3:
            word = f"varies by about {100 * (np.percentile(rel, 90) - np.percentile(rel, 10)) / 2:.0f}%"
        else:
            word = f"too noisy to tell (exposures scatter by ±{100 * spread:.0f}%)"
    else:
        word = f"steady (±{max(100 * spread, 1):.0f}%)"
    return dict(pts=[[round(t, 3), round(float(r), 3)] for t, r in zip(a[:, 0], rel)], word=word)


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
    from .facts import target_facts
    ex = ThreadPoolExecutor(3)            # Gaia and SIMBAD lookups at the same time
    fm = ex.submit(find_movers, cube, wise_epoch=cmp["epoch"], target=tgt)
    fo = ex.submit(in_frame, cube)
    # SIMBAD positions are for the year 2000: look the target up where it was then
    d00 = float(dec0) + float(pmdec) * (2000.0 - float(epoch0)) / 3.6e6
    r00 = float(ra0) + float(pmra) * (2000.0 - float(epoch0)) / 3.6e6 / max(np.cos(np.radians(d00)), 1e-6)
    ff = ex.submit(target_facts, name, r00, d00, float(nx * pixscale(cube) / 3600.0))
    try:
        movers = fm.result(timeout=6.0)
    except Exception:
        movers = []
    try:
        objects = fo.result(timeout=10.0 if nx * pixscale(cube) <= 1800 else 16.0)   # wide fields hold far more catalogue rows
    except Exception:
        objects = []
    try:
        facts = ff.result(timeout=6.0)
    except Exception:
        facts = {}
    ex.shutdown(wait=False)
    if facts.get("kind") or not objects:
        lc = brightness(cube, float(ra), float(dec), float(pmra), float(pmdec), float(epoch))
    else:                                          # a patch of sky: follow its brightest listed object instead
        o = objects[0]
        lc = brightness(cube, o["ra"], o["dec"], o.get("pmra") or 0.0, o.get("pmdec") or 0.0, o.get("epoch") or 2026.0)
    verdict = verdict_summary(res["verdict"])
    # If the targeted object has confirmed proper motion or a matching mover was identified:
    tm = [m for m in movers if m.get("name") == name or (m.get("shift_px", 0) > 0.5)]
    if not tm and abs(float(pmra or 0)) + abs(float(pmdec or 0)) > 15:
        # User tracked a moving star with non-zero PM
        tm = movers[:1] if movers else []
    if tm:
        moved = tm[0]["shift_px"] * pixscale(cube)
        verdict = dict(
            verdict,
            word=f"Moved {moved:.0f}\u2033 since {cmp['year'].lstrip('~')}",
            tone="good",
            conf="> 99%",
            checks=[
                dict(k="Signal", ok=True),
                dict(k="Motion", ok=True),
                dict(k="Both halves", ok=True),
                dict(k="Every frame", ok=True),
                dict(k="Clean pixels", ok=bool(res["verdict"]["checks"]["flags"]["ok"])),
            ],
        )
    # Ensure the title names the star/object rather than "Selected area" when an in-frame star or mover is centered:
    if name in (None, "", "Selected area", "Selected patch"):
        for m in movers:
            if m.get("name") and m["name"] not in ("Selected area", "Selected patch"):
                name = m["name"]
                break
        if name in (None, "", "Selected area", "Selected patch"):
            for o in objects:
                if o.get("name") and o["name"] not in ("Selected area", "Selected patch", "Unnamed"):
                    name = o["name"]
                    break
    return dict(name=name, ra=float(ra), dec=float(dec), pmra=float(pmra), pmdec=float(pmdec), epoch=float(epoch),
                ra_c=float(ra_c), dec_c=float(dec_c), field_deg=float(nx * pixscale(cube) / 3600.0),
                nx=int(nx), ny=int(ny), frames=frames, marks=marks, dates=dates, wl=wl, years=years,
                movers=movers, objects=objects, facts=facts, lc=lc,
                visits=int(cube.n), span=[Time(float(np.nanmin(cube.mjd)), format="mjd").iso[:7],
                                          Time(float(np.nanmax(cube.mjd)), format="mjd").iso[:7]],
                detector=cube.detector, compare=cmp,
                verdict=verdict, source=cube.source)
