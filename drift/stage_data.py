"""Everything the full-screen stage needs once a target has been checked."""
from __future__ import annotations

import numpy as np
from astropy.time import Time

from . import SPHEREX_PIXEL_ARCSEC
from .render import flip_y, stretch_params, to_dark_jpeg_b64
from .verdict_html import classify

# short, plain-language verdict words keyed on the existing classifier's label prefix
_WORDS = [("UNRELIABLE", "Unreliable here", "bad"), ("NOISE", "Nothing stands out", "dim"),
          ("STAR", "Star, not moving", "info"), ("MOVER (unconfirmed)", "Moving, unconfirmed", "warn"),
          ("MOVER", "Moving object", "good"), ("SOURCE FOUND", "Source, motion unclear", "warn")]


def verdict_summary(v):
    label, _ = classify(v)
    word, tone = "Unclear", "dim"
    for pref, w, t in _WORDS:
        if label.startswith(pref):
            word, tone = w, t
            break
    c = v["checks"]
    conf = (f"> {100 * v['confidence_floor']:.1f}%" if v["confidence_is_bound"] else f"{100 * v['confidence']:.0f}%")
    if tone in ("bad",):
        conf = "n/a"
    checks = [dict(k="Signal", ok=bool(c["strength"]["nu"] >= 6)),
              dict(k="Motion", ok=bool(c["motion"]["dchi2"] > 0)),
              dict(k="Both halves", ok=bool(c["halves"]["ok"])),
              dict(k="Every frame", ok=bool(c["leave_one_out"]["ok"])),
              dict(k="Clean pixels", ok=bool(c["flags"]["ok"]))]
    return dict(word=word, tone=tone, conf=conf, checks=checks)


def build_stage(res, name, ra, dec):
    cube = res["cube"]
    n, ny, nx = cube.cube.shape
    p = stretch_params(cube.cube)
    frames = [to_dark_jpeg_b64(cube.cube[i], p) for i in range(n)]
    marks = []
    for i in range(n):
        m = res["mark_fn"](cube.mjd[i]) if res.get("mark_fn") else None
        marks.append(None if m is None else [round((m[0] + 0.5) / nx, 4), round((flip_y(m[1], ny) + 0.5) / ny, 4)])
    order = np.argsort(cube.mjd)
    dates = [Time(float(cube.mjd[i]), format="mjd").iso[:10] for i in range(n)]
    wl = [None if not np.isfinite(w) else round(float(w), 2) for w in cube.wl]
    ra_c, dec_c = res["info"]["center"]
    return dict(name=name, ra=float(ra), dec=float(dec), ra_c=float(ra_c), dec_c=float(dec_c),
                field_deg=float(nx * SPHEREX_PIXEL_ARCSEC / 3600.0), nx=int(nx), ny=int(ny),
                frames=[frames[i] for i in order], marks=[marks[i] for i in order],
                dates=[dates[i] for i in order], wl=[wl[i] for i in order],
                baseline_days=float(cube.mjd.max() - cube.mjd.min()),
                verdict=verdict_summary(res["verdict"]), source=cube.source,
                synthetic=cube.source == "SYNTHETIC")
