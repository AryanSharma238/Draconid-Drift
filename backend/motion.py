"""Motion algorithms (Task 08): wavelength-matched residuals, pass stacking with
registration refinement, shift-and-stack search, proper-motion measurement.

Conventions (tasks/08-motion.md): FITS orientation (row 0 = south); (x, y) =
(column, row); grid CDELT1 < 0 so +x = west, +y = north; velocities in px/yr.
  pmra_cosdec = -vx * pixscale * 1000 ; pmdec = +vy * pixscale * 1000.
"""

import logging
import time
from dataclasses import dataclass, field, replace

import numpy as np
from scipy.ndimage import gaussian_filter, maximum_filter
from scipy.ndimage import shift as nd_shift

from backend import config
from backend.models import Frame, Grid

log = logging.getLogger(__name__)

MAD_TO_SIGMA = 1.4826
CENTROID_BOX_NPIX = 25  # (2*half+1)^2 with half=2, used in the centroid error model
REG_MAX_STARS = 30
DEDUP_RADIUS_PX = 2.0
GHOST_TAIL_SIGMA = 3.0  # PSF tail extent (sigma) that must clear the ghost aperture
CENTROID_RECENTRE_ITERS = 2
MASK_THRESHOLD = 0.5
COVERAGE_FRAC = 0.5
MJD_J2000 = 51544.5
DAYS_PER_YEAR = 365.25


def mjd_to_yr(t_mjd: float) -> float:
    return 2000.0 + (t_mjd - MJD_J2000) / DAYS_PER_YEAR


# --------------------------------------------------------------------- M1
def _lambda_matches(f: Frame, frames: list[Frame], dlam_frac: float) -> list[Frame]:
    """Frames from OTHER passes within dlam_frac * lambda of f (shared by M1 and E4)."""
    return [
        g
        for g in frames
        if g.pass_idx != f.pass_idx and abs(g.lambda_um - f.lambda_um) <= dlam_frac * f.lambda_um
    ]


def lambda_matched_residuals(frames: list[Frame], dlam_frac: float = config.LAMBDA_MATCH_FRAC) -> list[Frame]:
    """Subtract, from each frame, a co-add of other-pass frames at matching wavelength."""
    from backend import process  # Track B owns this module

    out: list[Frame] = []
    n_skipped = 0
    for f in frames:
        matches = _lambda_matches(f, frames, dlam_frac)
        if not matches:
            n_skipped += 1
            log.info("lmdiff: no wavelength match for %s", f.ref.obs_id)
            continue
        tmpl, tmpl_var = process.coadd(matches)
        resid = f.data - tmpl
        resid_var = f.var + tmpl_var if (f.var is not None and tmpl_var is not None) else None
        out.append(
            Frame(
                ref=f.ref,
                data=resid.astype(np.float32),
                var=None if resid_var is None else resid_var.astype(np.float32),
                flags=f.flags.copy(),
                lambda_um=f.lambda_um,
                pass_idx=f.pass_idx,
            )
        )
    log.info("lmdiff: n_in=%d n_out=%d n_skipped_no_match=%d", len(frames), len(out), n_skipped)
    return out


# --------------------------------------------------------------------- M2
@dataclass
class PassImage:
    data: np.ndarray  # background-subtracted co-add, NaN where no data
    sigma: float  # 1.4826 * MAD of finite pixels
    t_yr: float  # mean of input times
    n: int  # number of frames
    dx: float = 0.0  # registration offset (px): measured = true + dx
    dy: float = 0.0


def robust_sigma(a: np.ndarray) -> float:
    v = a[np.isfinite(a)]
    if v.size == 0:
        return float("nan")
    return float(MAD_TO_SIGMA * np.median(np.abs(v - np.median(v))))


def pass_stack(frames: list[Frame]) -> list[PassImage]:
    from backend import process  # Track B owns this module

    frames = process.group_by_pass(frames)
    by_pass: dict[int, list[Frame]] = {}
    for f in frames:
        by_pass.setdefault(f.pass_idx, []).append(f)
    if len(by_pass) < 2:
        raise ValueError("need >=2 passes")
    out = []
    for p in sorted(by_pass):
        fs = by_pass[p]
        data, _ = process.coadd(fs)
        data = np.asarray(data, dtype=np.float64)
        if np.isfinite(data).any():
            data = data - np.nanmedian(data)
        t = float(np.mean([mjd_to_yr(f.ref.t_mjd) for f in fs]))
        out.append(PassImage(data=data, sigma=robust_sigma(data), t_yr=t, n=len(fs)))
    out.sort(key=lambda p: p.t_yr)
    return out


def find_peaks(
    img: np.ndarray, sigma: float, nsig: float = config.PEAK_NSIG, border: int = 3
) -> list[tuple[float, float, float]]:
    """3x3 local maxima above nsig*sigma, `border` px from the edge. Sorted by value desc."""
    a = np.where(np.isfinite(img), img, -np.inf)
    is_max = (a == maximum_filter(a, size=3, mode="constant", cval=-np.inf)) & (a > nsig * sigma)
    mask = np.zeros_like(is_max)
    if min(a.shape) > 2 * border:
        mask[border : a.shape[0] - border, border : a.shape[1] - border] = True
    ys, xs = np.nonzero(is_max & mask)
    peaks = [(float(x), float(y), float(a[y, x])) for x, y in zip(xs, ys, strict=True)]
    peaks.sort(key=lambda p: (-p[2], p[1], p[0]))
    return peaks


def centroid(img: np.ndarray, x: float, y: float, half: int = 2) -> tuple[float, float, float] | None:
    """Flux-weighted centroid of positive pixels in a (2*half+1)^2 box. None if invalid."""
    xi, yi = int(np.floor(x + 0.5)), int(np.floor(y + 0.5))
    if xi - half < 0 or yi - half < 0 or xi + half >= img.shape[1] or yi + half >= img.shape[0]:
        return None
    box = img[yi - half : yi + half + 1, xi - half : xi + half + 1]
    if not np.isfinite(box).all():
        return None
    w = np.where(box > 0, box, 0.0)
    flux = float(w.sum())
    if flux <= 0:
        return None
    yy, xx = np.mgrid[-half : half + 1, -half : half + 1]
    return float(xi + (w * xx).sum() / flux), float(yi + (w * yy).sum() / flux), flux


def refine_registration(
    passes: list[PassImage], max_match_px: float = 1.5, min_stars: int = 5
) -> list[PassImage]:
    """Set dx, dy per pass from the median centroid offset of bright field stars vs the
    reference (most frames) pass. Images are not resampled. Returns new PassImages."""
    ref_i = max(range(len(passes)), key=lambda i: (passes[i].n, -i))
    ref = passes[ref_i]
    stars = []
    for x, y, _ in find_peaks(ref.data, ref.sigma, nsig=config.REG_NSIG)[:REG_MAX_STARS]:
        c = centroid(ref.data, x, y)
        if c is not None:
            stars.append((c[0], c[1]))
    out = []
    for i, p in enumerate(passes):
        if i == ref_i:
            out.append(replace(p, dx=0.0, dy=0.0))
            continue
        offs = []
        for sx, sy in stars:
            c = centroid(p.data, sx, sy)
            if c is None:
                continue
            ox, oy = c[0] - sx, c[1] - sy
            if np.hypot(ox, oy) < max_match_px:
                offs.append((ox, oy))
        if len(offs) >= min_stars:
            a = np.array(offs)
            out.append(replace(p, dx=float(np.median(a[:, 0])), dy=float(np.median(a[:, 1]))))
        else:
            log.warning(
                "registration: only %d matched stars (< %d) for pass at t=%.3f; dx=dy=0",
                len(offs),
                min_stars,
                p.t_yr,
            )
            out.append(replace(p, dx=0.0, dy=0.0))
    return out


# --------------------------------------------------------------------- M3
@dataclass
class MotionMap:
    snr: np.ndarray
    vx: np.ndarray
    vy: np.ndarray
    snr_static: np.ndarray
    t0: float
    v_grid: np.ndarray


def _baseline_yr(passes: list[PassImage]) -> float:
    return max(p.t_yr for p in passes) - min(p.t_yr for p in passes)


def velocity_grid(passes: list[PassImage], vmax: float = config.VMAX_PX_PER_YR) -> np.ndarray:
    step = config.VGRID_STEP_PX / _baseline_yr(passes)
    g = np.arange(-vmax, vmax + step / 2, step)
    log.info("velocity grid: step=%.4f px/yr, %d per axis (%d total)", step, g.size, g.size**2)
    return g


LOW_TAIL_PCT = 2.5  # percentile of a Gaussian at -1.96 sigma
LOW_TAIL_Z = 1.959964


def _noise_sigma(s: np.ndarray) -> float:
    """Empirical noise of a stacked map S, from its LOWER tail only.

    Deviation from the task text (nanmedian / 1.4826 MAD, per velocity), made because it
    failed on star-rich fields: bright stars smeared by a wrong velocity inflate the
    centre of the distribution, so a per-velocity median/MAD grows with |v| and the
    search is biased to v = 0. Pass images are background-subtracted (zero-mean noise)
    and no source is negative, so -P(2.5)/1.96 is clean. It is measured ONCE at v = 0 and
    reused for every velocity, so that comparing velocities is comparing raw S (the
    matched-filter statistic) and not noise-estimate jitter between velocities."""
    fin = s[np.isfinite(s)]
    if fin.size == 0:
        return float("nan")
    return -float(np.percentile(fin, LOW_TAIL_PCT)) / LOW_TAIL_Z


def motion_search(
    passes: list[PassImage],
    vmax: float = config.VMAX_PX_PER_YR,
    psf_sigma_px: float = config.PSF_SIGMA_PX,
) -> MotionMap:
    t_start = time.perf_counter()
    w = np.array([p.n / p.sigma**2 for p in passes])
    t = np.array([p.t_yr for p in passes])
    t0 = float((w * t).sum() / w.sum())
    C, M = [], []
    for p in passes:
        fin = np.isfinite(p.data)
        C.append(gaussian_filter(np.where(fin, p.data, 0.0), psf_sigma_px))
        m = gaussian_filter(fin.astype(np.float64), psf_sigma_px) > MASK_THRESHOLD
        M.append(m.astype(np.float64))
    wsum = w.sum()
    shape = passes[0].data.shape

    def stack(vx: float, vy: float) -> np.ndarray:
        S = np.zeros(shape)
        W = np.zeros(shape)
        for k, p in enumerate(passes):
            dt = p.t_yr - t0
            # scipy order is (row=y, col=x). A source at true x appears at x+dx in this
            # pass, so undo both its motion and the registration offset.
            sh = (-(vy * dt) - p.dy, -(vx * dt) - p.dx)
            S += w[k] * nd_shift(C[k], sh, order=1, mode="constant", cval=0.0)
            W += w[k] * nd_shift(M[k], sh, order=1, mode="constant", cval=0.0)
        ok = W > COVERAGE_FRAC * wsum
        S = np.where(ok, S / np.where(ok, W, 1.0), np.nan)
        return S

    s_static = stack(0.0, 0.0)
    sigma = _noise_sigma(s_static)
    if not np.isfinite(sigma) or sigma <= 0:
        raise ValueError("motion_search: cannot estimate noise (no coverage or flat image)")
    vg = velocity_grid(passes, vmax)
    snr = np.full(shape, -np.inf)
    vxm = np.zeros(shape)
    vym = np.zeros(shape)
    for vy in vg:
        for vx in vg:
            s = stack(vx, vy) / sigma
            better = np.nan_to_num(s, nan=-np.inf) > snr
            snr = np.where(better, s, snr)
            vxm = np.where(better, vx, vxm)
            vym = np.where(better, vy, vym)
    snr[np.isneginf(snr)] = np.nan
    snr_static = s_static / sigma
    log.info("motion_search: %.3f s", time.perf_counter() - t_start)
    return MotionMap(snr=snr, vx=vxm, vy=vym, snr_static=snr_static, t0=t0, v_grid=vg)


# --------------------------------------------------------------------- M4
@dataclass
class Mover:
    x0: float
    y0: float
    ra0: float
    dec0: float
    t0_yr: float
    vx: float
    vy: float
    evx: float
    evy: float
    pmra_masyr: float
    pmdec_masyr: float
    epmra_masyr: float
    epmdec_masyr: float
    pm_total_arcsec_yr: float
    snr_stack: float
    motion_sig: float
    n_pass_used: int
    track: list[tuple[float, float, float]] = field(default_factory=list)


def _wls_line(dt: np.ndarray, pos: np.ndarray, err: np.ndarray):
    """Weighted LSQ pos = p0 + v*dt. Returns (p0, v, ep0, ev). For n>=3 the covariance is
    scaled by max(1, chi2_red); for n==2 it is the propagated measurement error."""
    A = np.stack([np.ones_like(dt), dt], axis=1)
    Wm = 1.0 / err**2
    cov = np.linalg.inv(A.T @ (A * Wm[:, None]))
    p0, v = cov @ (A.T @ (Wm * pos))
    if len(dt) >= 3:
        chi2 = float(np.sum(Wm * (pos - (p0 + v * dt)) ** 2))
        cov = cov * max(1.0, chi2 / (len(dt) - 2))
    return float(p0), float(v), float(np.sqrt(cov[0, 0])), float(np.sqrt(cov[1, 1]))


def _recentred_centroid(
    img: np.ndarray, x: float, y: float, n_iter: int = CENTROID_RECENTRE_ITERS
) -> tuple[float, float, float] | None:
    """centroid() re-centred on its own result. A single 5x5 box placed off-centre on a
    star returns a centroid biased toward the box centre, which, with the tiny errors of a
    bright star, would turn a static star into a 'significant' mover."""
    c = centroid(img, x, y)
    for _ in range(n_iter):
        if c is None:
            return None
        c = centroid(img, c[0], c[1]) or c
    return c


def _box_flux(img: np.ndarray, x: float, y: float, half: int = config.GHOST_HALF) -> float:
    xi, yi = int(np.floor(x + 0.5)), int(np.floor(y + 0.5))
    y0, x0 = max(yi - half, 0), max(xi - half, 0)
    box = img[y0 : yi + half + 1, x0 : xi + half + 1]
    return float(np.nansum(box)) if box.size else 0.0


def _is_static_chain(used: list[tuple[PassImage, float, float]]) -> bool:
    """True if the 'mover' is really static stars strung together by the stack: a real
    mover is ABSENT from its other-pass positions, a static star is not. `used` holds
    (pass, x, y) with x, y the registration-corrected track positions. For every pair of
    passes whose track points are further apart than the aperture plus PSF tail (min_sep), the point seen in
    pass p must not also be present at the same place in pass q."""
    # Only test pairs far enough apart that the object itself cannot fall inside the
    # aperture (half-width + 0.5 px) at the other position, including its PSF tail.
    min_sep = config.GHOST_HALF + 0.5 + GHOST_TAIL_SIGMA * config.PSF_SIGMA_PX
    for p, xp, yp in used:
        fp = _box_flux(p.data, xp + p.dx, yp + p.dy)
        for q, xq, yq in used:
            if q is p or np.hypot(xp - xq, yp - yq) < min_sep:
                continue
            fq = _box_flux(q.data, xp + q.dx, yp + q.dy)
            noise = config.GHOST_NSIG * q.sigma * (2 * config.GHOST_HALF + 1)
            if fq > noise and fq >= config.GHOST_FRAC * fp:
                return True
    return False


def measure_movers(
    passes: list[PassImage],
    mmap: MotionMap,
    grid: Grid,
    nsig: float = config.PEAK_NSIG,
    min_motion_sig: float = config.MIN_MOTION_SIG,
    min_v_px: float = config.MIN_V_PX_PER_YR,
) -> list[Mover]:
    t0 = mmap.t0
    step = float(mmap.v_grid[1] - mmap.v_grid[0])
    wcs = grid.wcs()
    s = grid.pixscale_arcsec * 1000.0
    cands: list[Mover] = []
    for px, py, pk in find_peaks(mmap.snr, 1.0, nsig=nsig):
        xi, yi = int(px), int(py)
        gvx, gvy = float(mmap.vx[yi, xi]), float(mmap.vy[yi, xi])
        # No speed gate on the grid velocity: for sub-PSF motion the grid peaks near v=0
        # (M5, real data). The gate applies to the refit velocity below.
        ts, xs, ys, es, used = [], [], [], [], []
        for p in passes:
            dt = p.t_yr - t0
            c = _recentred_centroid(p.data, px + gvx * dt + p.dx, py + gvy * dt + p.dy)
            if c is None:
                continue
            snr_p = c[2] / (p.sigma * np.sqrt(CENTROID_BOX_NPIX))
            ts.append(p.t_yr)
            used.append((p, c[0] - p.dx, c[1] - p.dy))
            xs.append(c[0] - p.dx)
            ys.append(c[1] - p.dy)
            es.append(config.PSF_FWHM_PX / (2.355 * max(snr_p, 1.0)))
        if len(ts) < 2:
            x0, y0, vx, vy, evx, evy = px, py, gvx, gvy, step, step
        else:
            dt = np.array(ts) - t0
            e = np.array(es)
            x0, vx, _, evx = _wls_line(dt, np.array(xs), e)
            y0, vy, _, evy = _wls_line(dt, np.array(ys), e)
        if _is_static_chain(used):
            continue
        vmag = float(np.hypot(vx, vy))
        if vmag < min_v_px:
            continue
        sig_v = float(np.hypot(vx * evx, vy * evy) / vmag) if vmag > 0 else float("inf")
        motion_sig = vmag / sig_v if sig_v > 0 else float("inf")
        if motion_sig < min_motion_sig:
            continue
        ra0, dec0 = wcs.pixel_to_world_values(x0, y0)
        cands.append(
            Mover(
                x0=float(x0),
                y0=float(y0),
                ra0=float(ra0),
                dec0=float(dec0),
                t0_yr=t0,
                vx=vx,
                vy=vy,
                evx=evx,
                evy=evy,
                pmra_masyr=-vx * s,
                pmdec_masyr=vy * s,
                epmra_masyr=evx * s,
                epmdec_masyr=evy * s,
                pm_total_arcsec_yr=vmag * s / 1000.0,
                snr_stack=float(pk),
                motion_sig=float(motion_sig),
                n_pass_used=len(ts),
                track=[(float(a), float(b), float(c)) for a, b, c in zip(ts, xs, ys, strict=True)],
            )
        )
    cands.sort(key=lambda m: -m.snr_stack)
    kept: list[Mover] = []
    for m in cands:
        if all(np.hypot(m.x0 - k.x0, m.y0 - k.y0) >= DEDUP_RADIUS_PX for k in kept):
            kept.append(m)
    return kept[: config.MAX_MOVERS]


# --------------------------------------------------------------------- E4
def lambda_matched_lightcurve(
    frames: list[Frame],
    x: float,
    y: float,
    dlam_frac: float = config.LAMBDA_MATCH_FRAC,
    ring: bool | None = None,
) -> list[dict]:
    """Lambda-matched light curve at a fixed pixel position (tasks/09-edge.md E4).

    (x, y) are internal-array pixel coordinates (row 0 = south, +x = west), the same for every frame.
    Each frame is divided by a co-add of its lambda-matched frames from OTHER passes, so the spectral
    shape cancels and only change between passes remains. Frames are skipped, counted and logged when
    they have no match, when a flux is non-finite, when f_i <= 0 (dmag undefined), or when
    f_t <= 3 * e_t. Returns [{"t_yr","lambda_um","pass","ratio","err","dmag"}] sorted by t_yr.
    """
    from backend import photometry, process  # Track B / E own these modules

    frames = process.group_by_pass(frames)
    ap = (config.LC_APERTURE_R, config.LC_ANNULUS_R_IN, config.LC_ANNULUS_R_OUT)
    if ring is None:
        # a saturated/NaN core leaves most apertures unusable: use PSF-wing photometry for the WHOLE curve
        usable = sum(bool(np.isfinite(photometry.aperture_flux(f.data, f.var, x, y, *ap)[0])) for f in frames)
        ring = bool(frames) and usable / len(frames) < config.RING_AUTO_APERTURE_FRAC
        if ring:
            log.info(
                "lightcurve: aperture usable in %d of %d frames, switching to ring photometry",
                usable,
                len(frames),
            )

    def phot(data: np.ndarray, var: np.ndarray | None) -> tuple[float, float]:
        if ring:
            return photometry.ring_flux(
                data, var, x, y, config.RING_R_IN, config.RING_R_OUT, config.RING_MIN_FINITE_FRAC
            )
        return photometry.aperture_flux(data, var, x, y, *ap)

    out: list[dict] = []
    n_no_match = n_bad_flux = n_low_template = 0
    for f in frames:
        matches = _lambda_matches(f, frames, dlam_frac)
        if not matches:
            n_no_match += 1
            log.info("lightcurve: no wavelength match for %s", f.ref.obs_id)
            continue
        tmpl, tmpl_var = process.coadd(matches)
        f_i, e_i = phot(f.data, f.var)
        f_t, e_t = phot(tmpl, tmpl_var)
        if not (np.isfinite([f_i, e_i, f_t, e_t]).all() and f_i > 0 and e_i > 0 and e_t > 0):
            n_bad_flux += 1
            log.info(
                "lightcurve: unusable flux for %s (f_i=%s e_i=%s f_t=%s e_t=%s)",
                f.ref.obs_id,
                f_i,
                e_i,
                f_t,
                e_t,
            )
            continue
        if f_t <= config.LC_MIN_TEMPLATE_SNR * e_t:
            n_low_template += 1
            log.info("lightcurve: template too faint for %s (f_t=%.3g e_t=%.3g)", f.ref.obs_id, f_t, e_t)
            continue
        ratio = f_i / f_t
        err = ratio * float(np.hypot(e_i / f_i, e_t / f_t))
        out.append(
            {
                "t_yr": mjd_to_yr(f.ref.t_mjd),
                "lambda_um": float(f.lambda_um),
                "pass": int(f.pass_idx),
                "ratio": float(ratio),
                "err": err,
                "dmag": float(-2.5 * np.log10(ratio)),
                **({"ring": True} if ring else {}),
            }
        )
    out.sort(key=lambda p: p["t_yr"])
    log.info(
        "lightcurve: n_in=%d n_out=%d n_skipped_no_match=%d n_skipped_bad_flux=%d n_skipped_faint_template=%d",
        len(frames),
        len(out),
        n_no_match,
        n_bad_flux,
        n_low_template,
    )
    return out


def lightcurve_summary(points: list[dict]) -> dict:
    """Per-pass median dmag and the reduced chi^2 of the ratios against the constant 1.0.

    chi2_red = sum(((ratio - 1) / err)^2) / N (no fitted parameter, so dof = N); NaN if < 2 points.
    """
    by_pass: dict[int, list[float]] = {}
    for p in points:
        by_pass.setdefault(p["pass"], []).append(p["dmag"])
    per_pass = [
        {"pass": k, "median_dmag": float(np.median(v)), "n": len(v)} for k, v in sorted(by_pass.items())
    ]
    if len(points) < 2:
        chi2 = float("nan")
    else:
        ratio = np.array([p["ratio"] for p in points])
        err = np.array([p["err"] for p in points])
        chi2 = float(np.sum(((ratio - 1.0) / err) ** 2) / len(points))
    summary = {"per_pass_dmag": per_pass, "chi2_red_constant": chi2}
    if any(p.get("ring") for p in points):
        summary["photometry"] = "ring"  # PSF-wing photometry (saturated core), see config.RING_*
    return summary
