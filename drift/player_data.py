"""Build the JSON payload the JavaScript player needs (all frames pre-rendered).

Delegates wavelength-matched residuals, pass co-adds, astrometry, lightcurve, and
aperture photometry directly to `backend.motion` and `backend.photometry`.
"""
from __future__ import annotations

import numpy as np
from astropy.time import Time

from backend import config
from backend import motion as backend_motion
from backend.photometry import aperture_flux

from .clean import Cube, burst_groups, cube_to_frames
from .render import (
    flip_y,
    reconstruct_saturated_cores,
    robust_sigma,
    stretch_params,
    to_diff_raw_b64,
    to_png_b64,
    to_raw_b64,
)
from .track import stack_image


def _date(mjd):
    return Time(float(mjd), format="mjd").iso[:10]


def _astrometry_from_backend(frames, stacks, mjd_groups, center_xy, v_expected):
    """Use `backend.motion.measure_movers` on residuals, falling back to pass-coadd centroid fit."""
    try:
        res_frames = backend_motion.lambda_matched_residuals(frames)
        pass_res = backend_motion.pass_stack(res_frames)
        movers = backend_motion.measure_movers(pass_res)
    except Exception:
        movers = []
    if movers:
        # Pick mover closest to expected center
        x0, y0 = center_xy
        best = min(movers, key=lambda m: np.hypot(m.x0 - x0, m.y0 - y0))
        if np.hypot(best.x0 - x0, best.y0 - y0) <= 6.0:
            vx = -best.pmra_masyr / (config.PIXSCALE_ARCSEC * 1000.0)
            vy = +best.pmdec_masyr / (config.PIXSCALE_ARCSEC * 1000.0)
            evx = best.epmra_masyr / (config.PIXSCALE_ARCSEC * 1000.0)
            evy = best.epmdec_masyr / (config.PIXSCALE_ARCSEC * 1000.0)
            return dict(
                n_passes=int(best.n_pass_used),
                vx=float(vx),
                vy=float(vy),
                evx=float(evx),
                evy=float(evy),
                pmra_masyr=float(best.pmra_masyr),
                pmdec_masyr=float(best.pmdec_masyr),
                epmra_masyr=float(best.epmra_masyr),
                epmdec_masyr=float(best.epmdec_masyr),
                pm_total_arcsec_yr=float(best.pm_total_arcsec_yr),
            )

    # Pass-coadd centroid fit along target trajectory (handles saturated stars like Barnard's)
    m_mean = float(np.mean([m.mean() for m in mjd_groups]))
    x0, y0 = center_xy
    ts, xs, ys, es = [], [], [], []
    half = 2
    for img, mjds in zip(stacks, mjd_groups):
        if len(mjds) < 2:
            continue
        sig = robust_sigma(img)
        rec, _, _ = reconstruct_saturated_cores(img, sig)
        ny, nx = rec.shape
        dt_p = float((mjds.mean() - m_mean) / 365.25)
        ix = int(round(x0 + v_expected[0] * dt_p))
        iy = int(round(y0 + v_expected[1] * dt_p))
        if ix - half < 0 or ix + half >= nx or iy - half < 0 or iy + half >= ny:
            continue
        box = rec[iy - half : iy + half + 1, ix - half : ix + half + 1]
        w = np.clip(np.nan_to_num(box, nan=0.0), 0.0, None)
        if w.sum() <= 0:
            continue
        yy, xx = np.mgrid[iy - half : iy + half + 1, ix - half : ix + half + 1]
        cx = float((xx * w).sum() / w.sum())
        cy = float((yy * w).sum() / w.sum())
        snr_p = float(w.sum()) / (sig * (2 * half + 1))
        err = 1.46 / (2.355 * max(snr_p, 1.0)) + 0.08
        ts.append(dt_p)
        xs.append(cx)
        ys.append(cy)
        es.append(err)
    if len(ts) < 2:
        return None
    ts_a, xs_a, ys_a, es_a = np.array(ts), np.array(xs), np.array(ys), np.array(es)
    if len(ts_a) == 2:
        dt_span = max(abs(ts_a[1] - ts_a[0]), 1e-3)
        vx = float((xs_a[1] - xs_a[0]) / (ts_a[1] - ts_a[0]))
        vy = float((ys_a[1] - ys_a[0]) / (ts_a[1] - ts_a[0]))
        evx = float(np.hypot(es_a[0], es_a[1]) / dt_span) * 1.5
        evy = float(np.hypot(es_a[0], es_a[1]) / dt_span) * 1.2
    else:
        px, covx = np.polyfit(ts_a, xs_a, 1, w=1.0 / es_a, cov="unscaled")
        py, covy = np.polyfit(ts_a, ys_a, 1, w=1.0 / es_a, cov="unscaled")
        vx, vy = float(px[0]), float(py[0])
        evx = float(np.sqrt(max(covx[0, 0], 1e-6)) * 1.5)
        evy = float(np.sqrt(max(covy[0, 0], 1e-6)) * 1.2)
    pmra = -vx * config.PIXSCALE_ARCSEC * 1000.0
    pmdec = +vy * config.PIXSCALE_ARCSEC * 1000.0
    return dict(
        n_passes=len(ts),
        vx=vx,
        vy=vy,
        evx=evx,
        evy=evy,
        pmra_masyr=pmra,
        pmdec_masyr=pmdec,
        epmra_masyr=evx * config.PIXSCALE_ARCSEC * 1000.0,
        epmdec_masyr=evy * config.PIXSCALE_ARCSEC * 1000.0,
        pm_total_arcsec_yr=float(np.hypot(pmra, pmdec) / 1000.0),
    )


def _photometry_from_backend(frames, center_xy, v_expected):
    """Extract forced-aperture spectrum via `backend.photometry.aperture_flux` and
    differential light curve via `backend.motion.lambda_matched_lightcurve`."""
    m_mean = float(np.mean([f.ref.t_mjd for f in frames]))
    x0, y0 = center_xy
    spec_pts = []
    rec_frames = []
    for i, f in enumerate(frames):
        sig_i = robust_sigma(f.data)
        rec_i, _, _ = reconstruct_saturated_cores(f.data, sig_i)
        dt_i = float((f.ref.t_mjd - m_mean) / 365.25)
        xi = x0 + v_expected[0] * dt_i
        yi = y0 + v_expected[1] * dt_i
        fl, er = aperture_flux(
            rec_i,
            f.var,
            xi,
            yi,
            config.SPECTRUM_APERTURE_R,
            config.SPECTRUM_ANNULUS_R_IN,
            config.SPECTRUM_ANNULUS_R_OUT,
        )
        if np.isfinite(fl) and np.isfinite(f.lambda_um):
            spec_pts.append(
                dict(
                    i=int(i),
                    mjd=float(f.ref.t_mjd),
                    wl=float(f.lambda_um),
                    pass_idx=int(f.pass_idx),
                    flux=float(fl),
                    err=float(er),
                )
            )
        # Shift reconstructed frame to target rest frame for `backend.motion.lambda_matched_lightcurve`
        fr_copy = type(f)(
            ref=f.ref,
            data=rec_i.astype(np.float32),
            var=f.var,
            flags=f.flags,
            lambda_um=f.lambda_um,
            pass_idx=f.pass_idx,
        )
        rec_frames.append(fr_copy)

    lc_raw = backend_motion.lambda_matched_lightcurve(rec_frames, float(x0), float(y0))
    lc_list = lc_raw.get("points", []) if isinstance(lc_raw, dict) else lc_raw
    lc_pts = [
        dict(
            i=idx,
            mjd=float(51544.5 + (p["t_yr"] - 2000.0) * 365.25),
            wl=float(p["lambda_um"]),
            pass_idx=int(p["pass"]),
            ratio=float(p["ratio"]),
            err=float(p["err"]),
            dmag=float(p["dmag"]),
        )
        for idx, p in enumerate(lc_list)
    ]
    spec_pts.sort(key=lambda p: p["wl"])
    lc_pts.sort(key=lambda p: p["mjd"])
    return spec_pts, lc_pts


def build_payload(cube: Cube, expected_xy, v_expected, v_tracked, zoom=None, best_track=None):
    """expected_xy(mjd) -> (x, y) array-pixel position of the target at that date (or None).
    v_expected / v_tracked: (vx, vy) px/yr for the marker and for the tracked stack.
    best_track: optional dict {x, y, vx, vy} from verdict['best']."""
    n, ny, nx = cube.cube.shape
    zoom = zoom or max(6, int(520 / max(nx, ny)))
    m_mean = float(cube.mjd.mean())
    center_xy = expected_xy(m_mean) if expected_xy else (nx / 2.0, ny / 2.0)

    def mark(mjd):
        p = expected_xy(mjd) if expected_xy else None
        return None if p is None else [float(p[0]), float(flip_y(p[1], ny))]

    def track_mark(mjd):
        if not best_track:
            return None
        dt = (float(mjd) - m_mean) / 365.25
        tx = best_track["x"] + best_track["vx"] * dt
        ty = best_track["y"] + best_track["vy"] * dt
        return [float(tx), float(flip_y(ty, ny))]

    traj_exp = None
    if expected_xy:
        p0, p1 = mark(cube.mjd.min()), mark(cube.mjd.max())
        if p0 and p1:
            traj_exp = [p0, p1]
    traj_best = None
    if best_track:
        traj_best = [track_mark(cube.mjd.min()), track_mark(cube.mjd.max())]

    groups = burst_groups(cube.mjd)
    b_frames = cube_to_frames(cube)

    # --- view 1: every frame
    pf = stretch_params(cube.cube)
    frames = []
    for i in range(n):
        wl = cube.wl[i]
        wls = f"{wl:.2f} µm" if np.isfinite(wl) else "λ ?"
        frames.append(dict(imgs=[to_png_b64(cube.cube[i], pf)],
                           raws=[to_raw_b64(cube.cube[i], pf)],
                           is_diff=False,
                           label=f"Frame {i + 1}/{n} · {_date(cube.mjd[i])} · {wls}",
                           date=_date(cube.mjd[i]),
                           wl=float(wl) if np.isfinite(wl) else None,
                           marks=[mark(cube.mjd[i])],
                           tmarks=[track_mark(cube.mjd[i])],
                           mjd=float(cube.mjd[i])))

    # --- view 2: one stack per observing pass via `backend.motion.pass_stack`
    pass_coadds = backend_motion.pass_stack(b_frames)
    stacks = [(g, pc.data) for g, pc in zip(groups, pass_coadds)]
    ps = stretch_params([s[1] for s in stacks])
    bursts = []
    for bi, (g, img) in enumerate(stacks):
        m = float(cube.mjd[g].mean())
        d0, d1 = _date(cube.mjd[g].min()), _date(cube.mjd[g].max())
        w_b = float(np.nanmedian(cube.wl[g])) if np.isfinite(cube.wl[g]).any() else None
        bursts.append(dict(imgs=[to_png_b64(img, ps)],
                           raws=[to_raw_b64(img, ps)],
                           is_diff=False,
                           label=f"Pass {bi + 1}/{len(stacks)} ({len(g)} frames) · {d0} to {d1}",
                           date=f"{d0} → {d1}",
                           wl=w_b,
                           marks=[mark(m)],
                           tmarks=[track_mark(m)],
                           mjd=m))

    # --- view 3: wavelength-matched difference via `backend.motion.lambda_matched_residuals`
    res_frames = backend_motion.lambda_matched_residuals(b_frames)
    res_pass_coadds = backend_motion.pass_stack(res_frames)
    res_sig = robust_sigma(np.stack([rf.data for rf in res_frames]))
    lmdiff = []
    for bi, (g, rpc) in enumerate(zip(groups, res_pass_coadds)):
        r_img = rpc.data
        m = float(cube.mjd[g].mean())
        d0, d1 = _date(cube.mjd[g].min()), _date(cube.mjd[g].max())
        lmdiff.append(dict(imgs=[to_png_b64(r_img, ps)],
                           raws=[to_diff_raw_b64(r_img, res_sig)],
                           is_diff=True,
                           captions=["λ-Matched Residual (Orange = current epoch +peak · Blue = other-pass −ghost · Static stars cancel)"],
                           label=f"λ-Diff Pass {bi + 1}/{len(groups)} ({len(g)} frames) · {d0} to {d1}",
                           date=f"{d0} → {d1}",
                           wl=float(np.nanmedian(cube.wl[g])) if np.isfinite(cube.wl[g]).any() else None,
                           marks=[mark(m)],
                           tmarks=[track_mark(m)],
                           mjd=m))

    # --- view 4: fixed stack vs tracked stack (all frames)
    fixed, _ = stack_image(cube.cube, cube.var, cube.mjd, (0, 0), tracked=False)
    tracked, _ = stack_image(cube.cube, cube.var, cube.mjd, v_tracked, tracked=True)
    pc = stretch_params([fixed, tracked])
    mm = float(cube.mjd.mean())
    cmark = mark(mm)
    ctmark = track_mark(mm)
    compare = [dict(imgs=[to_png_b64(fixed, pc), to_png_b64(tracked, pc)],
                    raws=[to_raw_b64(fixed, pc), to_raw_b64(tracked, pc)],
                    is_diff=False,
                    captions=["Ordinary stack: stars stay sharp, moving target smears",
                              "Motion-tracked stack: moving target sharpens, field stars trail"],
                    label=f"All {n} frames co-added ({_date(cube.mjd.min())} to {_date(cube.mjd.max())})",
                    date=f"{_date(cube.mjd.min())} → {_date(cube.mjd.max())}",
                    wl=float(np.nanmedian(cube.wl)) if np.isfinite(cube.wl).any() else None,
                    marks=[cmark, cmark],
                    tmarks=[ctmark, ctmark],
                    mjd=mm)]

    astrometry = _astrometry_from_backend(
        b_frames, [s[1] for s in stacks], [cube.mjd[g] for g in groups], center_xy, v_expected
    )
    spectrum, lightcurve = _photometry_from_backend(b_frames, center_xy, v_expected)

    return dict(nx=int(nx), ny=int(ny), zoom=int(zoom),
                views=dict(frames=frames, bursts=bursts, lmdiff=lmdiff, compare=compare),
                traj_exp=traj_exp, traj_best=traj_best,
                v_expected=[float(v_expected[0]), float(v_expected[1])],
                v_tracked=[float(v_tracked[0]), float(v_tracked[1])],
                astrometry=astrometry, spectrum=spectrum, lightcurve=lightcurve,
                detector=cube.detector, source=cube.source,
                t0=float(cube.mjd.min()), t1=float(cube.mjd.max()))
