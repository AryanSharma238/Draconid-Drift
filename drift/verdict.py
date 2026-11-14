"""The verdict: one headline number plus five checks.

Headline = confidence that the best track is NOT noise = 1 - FAP, where FAP is the
empirical false-alarm probability: how often scrambled data (frame times shuffled)
produces a 'moving beats still' statistic T = nu^2 - nu_static^2 as large as the real
one, anywhere in the searched region and velocity grid. Because the search region and
velocity grid are identical in the scrambles, the look-elsewhere effect is included.

It is NOT the probability that the object is real; the five checks below it address
stars, artifacts and single bad frames.
"""
from __future__ import annotations

import numpy as np

from .track import PSF_SIGMA_PIX, TrackSearch, frame_maps, velocity_grid

TARGET_HALF_PX = 2          # target mode: +/-2 px around the expected position
TARGET_V_HALF = 0.6         # px/yr around the expected motion
TARGET_V_STEP = 0.15
BLIND_VMAX = 2.5            # px/yr
BLIND_V_STEP = 0.25
BLIND_MARGIN = 3            # px kept clear of the edge

# heuristic pass thresholds (documented in README, not derived)
HALF_MIN_FRACTION = 0.5     # each half must keep >= 50% of the full S/N
LOO_MIN_FRACTION = 0.8      # dropping any one frame must keep >= 80% of S/N
FLAG_MAX_FRACTION = 0.25    # <= 25% of the track's pixels unusable


def assess(cube, mode="target", v0=(0.0, 0.0), center=None, n_null=300, seed=1,
           psf_sigma=PSF_SIGMA_PIX, progress=None):
    """cube: drift.clean.Cube. center=(x, y) expected pixel at the mean epoch.
    v0=(vx, vy) expected motion in px/yr (array x, y axes). Returns a dict."""
    n, ny, nx = cube.cube.shape
    if n < 4:
        raise ValueError("Need at least 4 frames for a verdict.")
    dt = (cube.mjd - cube.mjd.mean()) / 365.25
    A, B, good = frame_maps(cube.cube, cube.var, psf_sigma)

    if mode == "blind":
        m = BLIND_MARGIN
        region = (m, ny - m, m, nx - m)
        vgrid = velocity_grid((0.0, 0.0), BLIND_VMAX, BLIND_V_STEP)
    else:
        cx, cy = (nx // 2, ny // 2) if center is None else (int(round(center[0])),
                                                            int(round(center[1])))
        h = TARGET_HALF_PX
        region = (max(cy - h, 2), min(cy + h + 1, ny - 2), max(cx - h, 2), min(cx + h + 1, nx - 2))
        vgrid = velocity_grid(v0, TARGET_V_HALF, TARGET_V_STEP)
    vmax = max(max(abs(v[0]), abs(v[1])) for v in vgrid)
    pad = int(np.ceil(vmax * np.abs(dt).max())) + 4
    S = TrackSearch(A, B, pad)

    S.static_map(dt)
    obs = S.best_over_grid(vgrid, dt, region)

    rng = np.random.default_rng(seed)
    exceed = 0
    for k in range(n_null):
        null = S.best_over_grid(vgrid, rng.permutation(dt), region)
        exceed += null["T"] >= obs["T"]
        if progress and k % 10 == 0:
            progress(k, n_null)
    fap = (1 + exceed) / (1 + n_null)

    x, y, v = obs["x"], obs["y"], obs["v"]
    nu = obs["nu"]
    idx = np.arange(n)
    h1, h2 = idx[0::2], idx[1::2]
    nu_h = [S.nu_at(x, y, v, dt, h1), S.nu_at(x, y, v, dt, h2)]
    loo = [S.nu_at(x, y, v, dt, np.delete(idx, i)) for i in range(n)]
    nu_loo_min = float(min(loo))

    bad = []
    for i in range(n):
        xi = int(round(x + v[0] * dt[i])); yi = int(round(y + v[1] * dt[i]))
        if 0 <= xi < nx and 0 <= yi < ny:
            win = good[i, max(yi - 1, 0):yi + 2, max(xi - 1, 0):xi + 2]
            bad.append(1.0 - win.mean())
        else:
            bad.append(1.0)
    flag_frac = float(np.mean(bad))

    moving = obs["T"] > 0 and nu > 0
    half_ok = moving and min(nu_h) >= HALF_MIN_FRACTION * nu
    loo_ok = moving and nu_loo_min >= LOO_MIN_FRACTION * nu
    flag_ok = flag_frac <= FLAG_MAX_FRACTION
    conf = 1.0 - fap
    floor = 1.0 - 1.0 / (1 + n_null)          # best confidence the null can resolve

    if not moving:
        headline = "No motion detected: the best track is no better than a stationary source."
    elif conf >= 0.99 and half_ok and loo_ok and flag_ok:
        headline = "Looks like a real mover: it beats noise and passes all four other checks."
    elif conf >= 0.95:
        headline = "Possible mover: strong against noise, but check the details below."
    else:
        headline = "Inconclusive: this could easily be noise."

    return dict(
        mode=mode, n_frames=n, n_null=n_null, fap=float(fap), confidence=float(conf),
        confidence_is_bound=bool(exceed == 0), confidence_floor=float(floor),
        headline=headline, moving=bool(moving),
        best=dict(x=x, y=y, vx=v[0], vy=v[1]),
        checks=dict(
            strength=dict(nu=float(nu)),
            motion=dict(dchi2=float(obs["T"]), nu_static=float(obs["nu_static"])),
            halves=dict(nu1=float(nu_h[0]), nu2=float(nu_h[1]), ok=bool(half_ok)),
            leave_one_out=dict(nu_min=nu_loo_min, ok=bool(loo_ok)),
            flags=dict(frac=flag_frac, ok=bool(flag_ok)),
        ),
        baseline_days=float(cube.mjd.max() - cube.mjd.min()),
    )
