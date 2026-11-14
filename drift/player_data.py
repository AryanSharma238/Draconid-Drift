"""Build the JSON payload the JavaScript player needs (all frames pre-rendered)."""
from __future__ import annotations

import numpy as np
from astropy.time import Time

from .clean import Cube, burst_groups
from .render import flip_y, robust_sigma, stretch_params, to_png_b64, to_png_diff_b64
from .targets import mjd_to_year
from .track import stack_image


def _date(mjd):
    return Time(float(mjd), format="mjd").iso[:10]


def build_payload(cube: Cube, expected_xy, v_expected, v_tracked, zoom=None):
    """expected_xy(mjd) -> (x, y) array-pixel position of the target at that date (or None).
    v_expected / v_tracked: (vx, vy) px/yr for the marker and for the tracked stack."""
    n, ny, nx = cube.cube.shape
    zoom = zoom or max(4, int(560 / max(nx, ny)))

    def mark(mjd):
        p = expected_xy(mjd) if expected_xy else None
        return None if p is None else [float(p[0]), float(flip_y(p[1], ny))]

    # --- view 1: every frame
    pf = stretch_params(cube.cube)
    frames = []
    for i in range(n):
        wl = cube.wl[i]
        wls = f"{wl:.2f} µm" if np.isfinite(wl) else "λ ?"
        frames.append(dict(imgs=[to_png_b64(cube.cube[i], pf)],
                           label=f"frame {i + 1}/{n} · {_date(cube.mjd[i])} · {wls}",
                           marks=[mark(cube.mjd[i])], mjd=float(cube.mjd[i])))

    # --- view 2: one stack per observing burst (stars fixed, mover steps between bursts)
    groups = burst_groups(cube.mjd)
    stacks = []
    for g in groups:
        img, _ = stack_image(cube.cube[g], cube.var[g], cube.mjd[g], (0, 0), tracked=False)
        stacks.append((g, img))
    ps = stretch_params([s[1] for s in stacks])
    bursts = []
    for g, img in stacks:
        m = float(cube.mjd[g].mean())
        d0, d1 = _date(cube.mjd[g].min()), _date(cube.mjd[g].max())
        bursts.append(dict(imgs=[to_png_b64(img, ps)],
                           label=f"burst of {len(g)} frames · {d0} to {d1}",
                           marks=[mark(m)], mjd=m))

    # --- view 2b: change since the first burst (static stars cancel; a mover leaves red + blue)
    diff = []
    if len(stacks) >= 2:
        ref = stacks[0][1]
        ds = [img - ref for _, img in stacks[1:]]
        flat = np.concatenate([d[np.isfinite(d)].ravel() for d in ds])
        lim = max(float(np.percentile(np.abs(flat), 99.5)), 4 * robust_sigma(flat)) if flat.size else 1.0
        d0 = _date(cube.mjd[stacks[0][0]].min())
        for (g, _), d in zip(stacks[1:], ds):
            m = float(cube.mjd[g].mean())
            diff.append(dict(imgs=[to_png_diff_b64(d, lim)], mjd=m, marks=[mark(m)],
                             label=f"burst of {len(g)} frames ({_date(cube.mjd[g].min())}) minus the first burst ({d0}): "
                                   "red = brighter now, blue = dimmer now"))

    # --- view 3: fixed stack vs tracked stack (all frames)
    fixed, _ = stack_image(cube.cube, cube.var, cube.mjd, (0, 0), tracked=False)
    tracked, _ = stack_image(cube.cube, cube.var, cube.mjd, v_tracked, tracked=True)
    pc = stretch_params([fixed, tracked])
    mm = float(cube.mjd.mean())
    cmark = mark(mm)
    compare = [dict(imgs=[to_png_b64(fixed, pc), to_png_b64(tracked, pc)],
                    captions=["Ordinary stack: stars sharp, a mover smears",
                              "Tracked stack: the mover sharpens, stars smear"],
                    label=f"all {n} frames stacked", marks=[cmark, cmark], mjd=mm)]

    track = None
    if expected_xy:
        a, b = mark(float(cube.mjd.min())), mark(float(cube.mjd.max()))
        if a and b:
            track = [a, b]

    return dict(nx=int(nx), ny=int(ny), zoom=int(zoom), track=track,
                views=dict(frames=frames, bursts=bursts, diff=diff, compare=compare),
                detector=cube.detector, source=cube.source,
                t0=float(cube.mjd.min()), t1=float(cube.mjd.max()))
