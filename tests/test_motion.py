"""Tests for backend/motion.py (Task 08 M1-M4).

backend.process is owned by Track B and may not be merged yet, so a minimal fake with the
contract signatures of tasks/03-fetch-process.md (group_by_pass, coadd) is injected.
"""

import logging
import sys
import types

import numpy as np
import pytest
from scipy.ndimage import shift as nd_shift

from backend import config, motion
from backend.models import Frame, ImageRef, make_grid
from backend.motion import (
    PassImage,
    centroid,
    find_peaks,
    lambda_matched_residuals,
    measure_movers,
    motion_search,
    pass_stack,
    refine_registration,
    velocity_grid,
)

NPIX = 59
T_PASSES = (2025.4, 2025.9, 2026.4)
GRID = make_grid(150.0, 2.0, 0.1)
PIX_MAS = config.PIXSCALE_ARCSEC * 1000.0


# ------------------------------------------------------------------ fakes / helpers
def _fake_coadd(frames):
    """Inverse-variance weighted mean ignoring NaN (equal weights if any var is None)."""
    data = np.stack([f.data.astype(np.float64) for f in frames])
    if any(f.var is None for f in frames):
        w = np.ones_like(data)
    else:
        w = 1.0 / np.stack([f.var.astype(np.float64) for f in frames])
    fin = np.isfinite(data)
    w = np.where(fin, w, 0.0)
    den = w.sum(axis=0)
    num = (w * np.where(fin, data, 0.0)).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(den > 0, num / den, np.nan)
        var = np.where(den > 0, 1.0 / den, np.nan)
    return out, var


def _fake_group_by_pass(frames, gap_days=config.PASS_GAP_DAYS):
    frames = sorted(frames, key=lambda f: f.ref.t_mjd)
    p = 0
    for i, f in enumerate(frames):
        if i and f.ref.t_mjd - frames[i - 1].ref.t_mjd > gap_days:
            p += 1
        f.pass_idx = p
    return frames


@pytest.fixture
def fake_process(monkeypatch):
    import backend

    mod = types.ModuleType("backend.process")
    mod.coadd = _fake_coadd
    mod.group_by_pass = _fake_group_by_pass
    monkeypatch.setitem(sys.modules, "backend.process", mod)
    monkeypatch.setattr(backend, "process", mod, raising=False)
    return mod


# Test amplitudes below are quoted as the peak of a sigma = 1 px PSF. The real PSF is
# narrower (config.PSF_SIGMA_PX), and the matched-filter SNR of a Gaussian source is
# proportional to peak * sigma_psf, so the rendered peak is amp / PSF_SIGMA_PX: every
# test keeps the matched-filter SNR it was designed with.
AMP_SCALE = 1.0 / config.PSF_SIGMA_PX


def render(shape, stars, noise=1.0, rng=None, psf=config.PSF_SIGMA_PX):
    """stars = [(x, y, peak_amplitude)], Gaussian PSF of sigma `psf`, + white noise."""
    yy, xx = np.mgrid[0 : shape[0], 0 : shape[1]]
    img = np.zeros(shape)
    for x, y, a in stars:
        img += a * AMP_SCALE * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * psf**2))
    if rng is not None and noise > 0:
        img += rng.normal(0, noise, shape)
    return img


def random_static(rng, n, amp=(25, 60), margin=8, shape=(NPIX, NPIX), avoid=()):
    stars = []
    while len(stars) < n:
        x, y = rng.uniform(margin, shape[1] - margin), rng.uniform(margin, shape[0] - margin)
        if any(np.hypot(x - ax, y - ay) < 8 for ax, ay in avoid):
            continue
        if any(np.hypot(x - sx, y - sy) < 6 for sx, sy, _ in stars):
            continue
        stars.append((x, y, rng.uniform(*amp)))
    return stars


def make_passes(mover=None, static=(), seed=0, shape=(NPIX, NPIX), times=T_PASSES, offsets=None, n=3):
    """PassImages straight from numpy. mover = (x_ref, y_ref, amp, vx, vy) with x_ref the
    position at t_ref = mean(times) (== t0 when all passes carry equal weight).
    offsets = {pass_index: (dx, dy)} adds a registration error to everything in that pass
    (measured = true + offset), by rendering all sources at the shifted position."""
    rng = np.random.default_rng(seed)
    t_ref = float(np.mean(times))
    offsets = offsets or {}
    out = []
    for k, t in enumerate(times):
        ox, oy = offsets.get(k, (0.0, 0.0))
        stars = [(x + ox, y + oy, a) for x, y, a in static]
        if mover is not None:
            x, y, a, vx, vy = mover
            stars.append((x + vx * (t - t_ref) + ox, y + vy * (t - t_ref) + oy, a))
        img = render(shape, stars, rng=rng)
        out.append(PassImage(data=img, sigma=motion.robust_sigma(img), t_yr=t, n=n))
    return out


def mjd_of(t_yr):
    return motion.MJD_J2000 + (t_yr - 2000.0) * motion.DAYS_PER_YEAR


_uid = iter(range(10**6))


def make_frame(data, t_mjd, lam, var=1.0):
    i = next(_uid)
    ref = ImageRef(
        obs_id=f"f{i}_D6",
        exposure_id=f"f{i}",
        detector=6,
        t_mjd=float(t_mjd),
        release="QR3",
        version="v",
        uri="x",
    )
    v = None if var is None else np.full(data.shape, var, np.float32)
    return Frame(
        ref=ref,
        data=data.astype(np.float32),
        var=v,
        flags=np.zeros(data.shape, np.uint32),
        lambda_um=lam,
    )


def mover_peak_pos(passes):
    return motion_search(passes)


# ------------------------------------------------------------------ M1
def _lm_frames(rng, star_fn, lams=(1.0, 2.0, 3.0), n_pass=3):
    frames = []
    for p in range(n_pass):
        for lam in lams:
            img = render((NPIX, NPIX), star_fn(p, lam), rng=rng)
            frames.append(make_frame(img, 60000 + 100 * p + lam, lam))
    return frames


def test_01_colour_is_removed_naive_diff_is_not(fake_process):
    """A static star whose flux scales with lambda must vanish in the wavelength-matched
    residual; a naive diff against the all-frame median leaves a huge colour residual,
    which is exactly the failure this algorithm exists to avoid."""
    rng = np.random.default_rng(1)
    frames = _lm_frames(rng, lambda p, lam: [(29.0, 29.0, 100.0 * lam)])
    frames = fake_process.group_by_pass(frames)
    res = lambda_matched_residuals(frames)
    assert len(res) == len(frames)
    naive_med = np.median(np.stack([f.data for f in frames]), axis=0)
    for f_in, f_res in zip(frames, res, strict=True):
        sig = motion.robust_sigma(f_res.data)
        assert abs(f_res.data[29, 29]) < 3 * sig
        if f_in.lambda_um != 2.0:  # lambda=2 happens to equal the median colour
            naive = f_in.data - naive_med
            assert abs(naive[29, 29]) > 10 * sig


def test_02_mover_leaves_positive_blob_and_negative_ghosts(fake_process):
    """Template comes from OTHER passes, so a mover is a + peak where it is now and
    - ghosts where it was in the other passes (not subtracted away)."""
    rng = np.random.default_rng(2)
    xs = {0: 15.0, 1: 30.0, 2: 45.0}
    frames = _lm_frames(rng, lambda p, lam: [(xs[p], 29.0, 80.0)], lams=(1.5,))
    frames = fake_process.group_by_pass(frames)
    res = lambda_matched_residuals(frames)
    for f in res:
        sig = motion.robust_sigma(f.data)
        assert f.data[29, int(xs[f.pass_idx])] > 10 * sig
        for p, x in xs.items():
            if p != f.pass_idx:
                assert f.data[29, int(x)] < -3 * sig


def test_03_unmatched_frame_skipped_and_logged(fake_process, caplog):
    """A frame with no other-pass frame at its wavelength has no valid template; emitting
    it would pass through raw colour, so it is dropped and the count logged."""
    rng = np.random.default_rng(3)
    frames = _lm_frames(rng, lambda p, lam: [(29.0, 29.0, 50.0)], lams=(1.0,))
    frames.append(make_frame(render((NPIX, NPIX), [], rng=rng), 60500, 4.0))  # lone lambda
    frames = fake_process.group_by_pass(frames)
    with caplog.at_level(logging.INFO, logger="backend.motion"):
        res = lambda_matched_residuals(frames)
    assert len(res) == len(frames) - 1
    assert all(f.lambda_um == 1.0 for f in res)
    assert "n_skipped_no_match=1" in caplog.text
    assert f"n_in={len(frames)}" in caplog.text


def test_04_nan_in_template_inputs_ignored(fake_process):
    """Bad pixels in one template frame must not blank the residual when another match
    has data there; otherwise every masked pixel would spread across all outputs."""
    rng = np.random.default_rng(4)
    frames = _lm_frames(rng, lambda p, lam: [], lams=(1.0,))
    frames[1].data[10:15, 10:15] = np.nan  # the pass-1 frame: a template for passes 0 and 2
    frames = fake_process.group_by_pass(frames)
    res = lambda_matched_residuals(frames)
    first = next(f for f in res if f.pass_idx == 0)
    assert np.isfinite(first.data[10:15, 10:15]).all()
    own = next(f for f in res if f.ref.obs_id == frames[1].ref.obs_id)
    assert np.isnan(own.data[10:15, 10:15]).all()  # its own data is NaN there
    assert np.isfinite(own.data).sum() == own.data.size - 25


# ------------------------------------------------------------------ M2
def test_05_registration_recovers_known_shift():
    """Residual WCS error between passes would fake a proper motion; the median star
    offset must recover a known (dx, dy) = (0.4, -0.3), with sign = measured - reference."""
    rng = np.random.default_rng(5)
    stars = random_static(rng, 20, amp=(30, 80))
    p1 = make_passes(static=stars, seed=1, n=10)
    p2 = make_passes(static=stars, seed=2, n=3)
    p2[1].data = nd_shift(p2[1].data, (-0.3, 0.4), order=3)  # (row, col) = (dy, dx)
    out = refine_registration([p2[1], p1[0]])  # pass with n=10 is the reference
    ref, shifted = out[1], out[0]
    assert (ref.dx, ref.dy) == (0.0, 0.0)
    assert shifted.dx == pytest.approx(0.4, abs=0.05)
    assert shifted.dy == pytest.approx(-0.3, abs=0.05)


def test_06_too_few_stars_gives_zero_and_warns(caplog):
    """With < min_stars the median is untrustworthy; fall back to 0 and say so."""
    rng = np.random.default_rng(6)
    stars = random_static(rng, 3, amp=(40, 80))
    p = make_passes(static=stars, seed=1, n=10)[:2]
    p[1].data = nd_shift(p[1].data, (0.0, 0.4), order=3)
    p[1].n = 3
    with caplog.at_level(logging.WARNING, logger="backend.motion"):
        out = refine_registration(p)
    assert (out[1].dx, out[1].dy) == (0.0, 0.0)
    assert "registration" in caplog.text


def test_pass_stack_groups_sorts_and_requires_two_passes(fake_process):
    """pass_stack must split frames at the pass gap, time-order the result and refuse a
    single pass (no baseline = no motion)."""
    rng = np.random.default_rng(7)
    frames = []
    for t in (mjd_of(2025.9), mjd_of(2025.4), mjd_of(2025.41)):
        frames.append(make_frame(render((NPIX, NPIX), [(29, 29, 50)], rng=rng) + 7.0, t, 1.0))
    ps = pass_stack(frames)
    assert [p.n for p in ps] == [2, 1]
    assert ps[0].t_yr < ps[1].t_yr
    assert abs(np.nanmedian(ps[0].data)) < 0.1  # background removed
    with pytest.raises(ValueError, match="need >=2 passes"):
        pass_stack(frames[:1])


def test_find_peaks_and_centroid_basics():
    """Border exclusion and NaN rejection protect the fit from edge/bad-pixel garbage."""
    img = render((NPIX, NPIX), [(20.3, 25.6, 50), (1.0, 1.0, 90)])
    pk = find_peaks(img, 1.0)
    assert len(pk) == 1 and (pk[0][0], pk[0][1]) == (20.0, 26.0)
    cx, cy, _ = centroid(img, 20, 26)
    assert (cx, cy) == (pytest.approx(20.3, abs=0.05), pytest.approx(25.6, abs=0.05))
    img[25, 20] = np.nan
    assert centroid(img, 20, 26) is None
    assert centroid(img, 1, 1) is None


# ------------------------------------------------------------------ M3
def _search_peak(mm):
    s = np.nan_to_num(mm.snr, nan=-np.inf)
    iy, ix = np.unravel_index(np.argmax(s), s.shape)
    return ix, iy


def _scene(amp, vx=1.6, vy=-0.4, seed=10, **kw):
    rng = np.random.default_rng(seed)
    mover = (29.0, 29.0, amp, vx, vy)
    static = random_static(rng, 30, avoid=[(29, 29)])
    return make_passes(mover=mover, static=static, seed=seed, **kw), static


def _mover_peak_in_map(mm, x, y, r=2):
    sub = np.nan_to_num(mm.snr, nan=-np.inf)[y - r : y + r + 1, x - r : x + r + 1]
    iy, ix = np.unravel_index(np.argmax(sub), sub.shape)
    return x - r + ix, y - r + iy


def test_07_recovers_moving_source():
    """Core claim: shift-and-stack localises a mover at t0 and gets its velocity right."""
    passes, _ = _scene(8.0)
    mm = motion_search(passes)
    step = mm.v_grid[1] - mm.v_grid[0]
    ix, iy = _mover_peak_in_map(mm, 29, 29)
    assert np.hypot(ix - 29, iy - 29) <= 1.0
    # the velocity surface is flat near the optimum (the mover travels only ~1.6 px over
    # the baseline, less than the PSF FWHM) and order=1 interpolation biases it slightly,
    # so "within one step" is the achievable precision; M4's least-squares fit refines it
    assert mm.snr[iy, ix] > config.PEAK_NSIG
    assert abs(mm.vx[iy, ix] - 1.6) <= step and abs(mm.vy[iy, ix] + 0.4) <= step


def test_08_faint_mover_below_single_pass_threshold_is_detected():
    """The reason for shift-and-stack: at 3.5 sigma per pass the source is invisible by
    eye / find_peaks in any single pass, yet the stack must reach >= 5."""
    passes, _ = _scene(3.5, seed=11)
    assert all(3.5 < config.PEAK_NSIG * p.sigma for p in passes)  # below single-pass threshold
    mm = motion_search(passes)
    ix, iy = _mover_peak_in_map(mm, 29, 29)
    assert np.hypot(ix - 29, iy - 29) <= 1.0
    assert mm.snr[iy, ix] >= 5.0


def test_09_static_stars_have_zero_velocity():
    """A static star must not masquerade as a mover: argmax v at its pixel is 0 +- 1 step."""
    passes, static = _scene(8.0, seed=12)
    mm = motion_search(passes)
    step = mm.v_grid[1] - mm.v_grid[0]
    for x, y, a in static:
        xi, yi = round(x), round(y)
        assert abs(mm.vx[yi, xi]) <= step and abs(mm.vy[yi, xi]) <= step
        assert mm.snr_static[yi, xi] > 5  # and it is clearly seen at v=0


def test_10_registration_offset_is_corrected_with_right_sign():
    """Pass 3 has a +0.5 px x registration error (measured = true + 0.5), which looks
    like +1 px/yr of extra motion (dt = 0.5 yr). With dx from refine_registration the
    stack still finds the mover and the fitted vx is right; with dx forced to 0 the fitted
    vx is biased by ~1 px/yr (>= 1 grid step). Fails if the sign of dx is flipped in either
    the stack or the fit. (The task puts the offset on pass 2, but pass 2 sits at t0 where
    dt = 0 and an offset there cannot bias v. The stack argmax on its own is too flat to
    show a 0.5 px offset, so the discriminating check is on the fitted velocity.)"""
    rng = np.random.default_rng(13)
    static = random_static(rng, 30, amp=(40, 90), avoid=[(29, 29)])
    mover = (29.0, 29.0, 10.0, 1.6, -0.4)
    passes = make_passes(mover=mover, static=static, seed=13, offsets={2: (0.5, 0.0)}, n=3)
    passes[0].n = 10  # make pass 0 the registration reference
    reg = refine_registration(passes)
    assert reg[2].dx == pytest.approx(0.5, abs=0.1)
    assert reg[0].dx == 0.0
    for p in reg:  # n only sets weights; equalise so t0 is identical in both runs
        p.n = 3
    raw = [PassImage(p.data, p.sigma, p.t_yr, p.n) for p in reg]  # dx = dy = 0

    def fitted_vx(ps):
        mm = motion_search(ps)
        ms = measure_movers(ps, mm, GRID)
        m = min(ms, key=lambda m: np.hypot(m.x0 - 29, m.y0 - 29))
        assert np.hypot(m.x0 - 29, m.y0 - 29) < 1.0
        return m.vx, mm.v_grid[1] - mm.v_grid[0]

    vx_ok, step = fitted_vx(reg)
    vx_bad, _ = fitted_vx(raw)
    assert abs(vx_ok - 1.6) < 0.25
    assert vx_bad - 1.6 >= step  # uncorrected: apparent motion is too fast, in the +x sense


# ------------------------------------------------------------------ M4
def test_11_pm_recovered_within_errors_and_realistic_sigma():
    """The validation table depends on this: injected pmra=-8000, pmdec=+700 mas/yr must
    come back within 3 sigma with an error that is small enough to be useful.
    Mover: matched-filter SNR equivalent to a sigma=1 px source of peak 20 per pass (see
    AMP_SCALE). KNOWN LIMIT at the undersampled PSF (sigma 0.62 px): the centroid has a
    pixel-phase bias that the error model ignores, so over 40 seeds the pmra pull has
    rms ~1.7 (not 1) and ~7% exceed 3 sigma; this fixed seed is within 3 sigma, the
    tolerance is NOT widened, and the formal errors should be treated as optimistic."""
    vx = 8000.0 / PIX_MAS  # pmra < 0 (moving west) -> +x
    vy = 700.0 / PIX_MAS
    passes, _ = _scene(20.0, vx=vx, vy=vy, seed=14)
    mm = motion_search(passes)
    movers = measure_movers(passes, mm, GRID)
    m = min(movers, key=lambda m: np.hypot(m.x0 - 29, m.y0 - 29))
    assert np.hypot(m.x0 - 29, m.y0 - 29) < 1.0
    assert abs(m.pmra_masyr + 8000) < 3 * m.epmra_masyr
    assert abs(m.pmdec_masyr - 700) < 3 * m.epmdec_masyr
    assert m.epmra_masyr < 1500 and m.epmdec_masyr < 1500
    assert m.n_pass_used == 3 and len(m.track) == 3
    assert m.pm_total_arcsec_yr == pytest.approx(np.hypot(m.pmra_masyr, m.pmdec_masyr) / 1000)
    assert m.t0_yr == pytest.approx(mm.t0)


def test_11b_sub_psf_mover_is_not_gated_by_coarse_grid_velocity():
    """M5 regression (real data, 2026-11-14): eps Ind B (0.76 px/yr) and WISE 0855 (1.3 px/yr)
    shift less than ~1 PSF width over the baseline, so the coarse velocity grid peaks near
    v=0 and the MIN_V gate threw them away before the centroid refit, although they were
    SNR 15-400 detections. The gate must apply to the refit velocity."""
    vx, vy = -0.65, -0.40  # eps Ind B's measured px/yr in the real field
    passes, _ = _scene(200.0, vx=vx, vy=vy, seed=21)
    movers = measure_movers(passes, motion_search(passes), GRID)
    assert movers, "bright sub-PSF mover rejected"
    m = min(movers, key=lambda m: np.hypot(m.x0 - 29, m.y0 - 29))
    assert np.hypot(m.x0 - 29, m.y0 - 29) < 1.0
    assert abs(m.pmra_masyr - (-vx * PIX_MAS)) < 3 * m.epmra_masyr
    assert abs(m.pmdec_masyr - vy * PIX_MAS) < 3 * m.epmdec_masyr


def test_12_east_motion_gives_positive_pmra():
    """East = RA increasing = toward smaller x (CDELT1 < 0). A flipped sign would put every
    proper motion in the wrong hemisphere of the validation table."""
    passes, _ = _scene(20.0, vx=-1.2, vy=0.0, seed=15)  # vx < 0: moving to lower x = east
    movers = measure_movers(passes, motion_search(passes), GRID)
    m = min(movers, key=lambda m: np.hypot(m.x0 - 29, m.y0 - 29))
    assert m.pmra_masyr > 0
    assert m.pmra_masyr == pytest.approx(1.2 * PIX_MAS, rel=0.1)
    # and the sky position agrees: later positions have larger RA (east)
    ra_first = GRID.wcs().pixel_to_world_values(m.track[0][1], m.track[0][2])[0]
    ra_last = GRID.wcs().pixel_to_world_values(m.track[-1][1], m.track[-1][2])[0]
    assert ra_last > ra_first


def test_12b_north_motion_gives_positive_pmdec():
    """+y = north (row 0 = south): a source moving to higher rows has pmdec > 0."""
    passes, _ = _scene(20.0, vx=0.0, vy=1.2, seed=16)
    movers = measure_movers(passes, motion_search(passes), GRID)
    m = min(movers, key=lambda m: np.hypot(m.x0 - 29, m.y0 - 29))
    assert m.pmdec_masyr > 0


def test_13_static_field_has_no_movers():
    """False positives would flood the candidate table: stars alone must give none."""
    rng = np.random.default_rng(17)
    static = random_static(rng, 30)
    passes = make_passes(static=static, seed=17)
    assert measure_movers(passes, motion_search(passes), GRID) == []


def test_14_deterministic():
    """Same inputs, same outputs (cached results and the submission table must be stable)."""
    passes, _ = _scene(20.0, seed=18)
    a = measure_movers(passes, motion_search(passes), GRID)
    b = measure_movers(passes, motion_search(passes), GRID)
    assert a == b and len(a) >= 1


# ------------------------------------------------------------------ timing (Done-when)
def test_velocity_grid_step_scales_with_baseline():
    """Step = VGRID_STEP / baseline so misalignment across the baseline <= one step px."""
    passes = make_passes(times=(2025.0, 2025.5, 2027.0))
    g = velocity_grid(passes)
    assert g[1] - g[0] == pytest.approx(config.VGRID_STEP_PX / 2.0)


@pytest.mark.parametrize("npix,limit", [(59, 0.5), (291, 10.0)])
def test_motion_search_timing(npix, limit):
    """Performance budget from the task's Done-when."""
    import time

    rng = np.random.default_rng(19)
    shape = (npix, npix)
    static = random_static(rng, 30, margin=8, shape=shape)
    c = npix // 2
    passes = make_passes(mover=(c, c, 10.0, 1.6, -0.4), static=static, seed=19, shape=shape)
    t = time.perf_counter()
    motion_search(passes)
    dt = time.perf_counter() - t
    print(f"motion_search {npix}x{npix}: {dt:.3f} s")
    assert dt < limit


def test_10b_stack_applies_dx_with_correct_sign():
    """Direct check of the sign of the registration term inside motion_search: pass 3 is
    misregistered by +3 px (measured = true + 1.5). Declaring dx = +3 must recover a
    stronger stack at the mover than dx = 0 or dx = -3 (a flipped sign would double the
    misalignment)."""
    rng = np.random.default_rng(21)
    static = random_static(rng, 30, avoid=[(29, 29)])
    mover = (29.0, 29.0, 15.0, 0.0, 0.0)  # at rest, so only dx can misalign the stack
    base = make_passes(mover=mover, static=static, seed=21, offsets={2: (3.0, 0.0)})

    def peak(dx):
        ps = [PassImage(p.data, p.sigma, p.t_yr, p.n, dx=dx if k == 2 else 0.0) for k, p in enumerate(base)]
        mm = motion_search(ps)
        return np.nanmax(mm.snr_static[27:32, 27:32])  # v=0: velocity cannot absorb dx

    good, none, flipped = peak(3.0), peak(0.0), peak(-3.0)
    assert good > 1.2 * none and good > 1.2 * flipped


# ------------------------------------------------------------------ E4 light curves
LC_X = LC_Y = 29
LC_LAMS = (1.0, 1.5, 2.0, 2.5, 3.0)


def _g(lam):
    """Strongly lambda-dependent spectrum: x3 across the band."""
    return 1.0 + (lam - 1.0)


def _lc_frames(rng, n_pass=4, scale=lambda p: 1.0, lams=LC_LAMS):
    frames = []
    for p in range(n_pass):
        for lam in lams:
            img = render((NPIX, NPIX), [(LC_X, LC_Y, 400.0 * _g(lam) * scale(p))], rng=rng)
            frames.append(make_frame(img, 60000 + 100 * p + 5 * lam, lam))
    return frames


def _naive_dmag(frames, x, y):
    """What you get WITHOUT lambda matching: divide by the co-add of all other-pass frames."""
    from backend import photometry

    out = []
    for f in frames:
        others = [g for g in frames if g.pass_idx != f.pass_idx]
        tmpl, tv = _fake_coadd(others)
        fi, _ = photometry.aperture_flux(f.data, f.var, x, y, 2, 4, 6)
        ft, _ = photometry.aperture_flux(tmpl, tv, x, y, 2, 4, 6)
        out.append(-2.5 * np.log10(fi / ft))
    return np.array(out)


def test_15_constant_star_with_colour_has_zero_dmag_and_unit_chi2(fake_process):
    """A constant star whose flux changes x3 with wavelength must give dmag ~ 0 in every pass and
    chi2_red ~ 1: the spectral shape must not leak into 'variability'. The naive (unmatched) ratio
    on the same data is far off, so this test fails if matching is silently bypassed."""
    rng = np.random.default_rng(15)
    frames = fake_process.group_by_pass(_lc_frames(rng))
    pts = motion.lambda_matched_lightcurve(frames, LC_X, LC_Y)
    assert len(pts) == len(frames)
    assert [p["t_yr"] for p in pts] == sorted(p["t_yr"] for p in pts)
    assert set(pts[0]) == {"t_yr", "lambda_um", "pass", "ratio", "err", "dmag"}
    s = motion.lightcurve_summary(pts)
    assert [d["pass"] for d in s["per_pass_dmag"]] == [0, 1, 2, 3]
    for d in s["per_pass_dmag"]:
        assert d["n"] == len(LC_LAMS)
        assert abs(d["median_dmag"]) < 0.02
    # ~1.3 median over seeds (annulus-median noise and template correlation are not in err), range 0.5-3.1;
    # a variable star gives >> 10 (test 16), so this band still separates the two.
    assert 0.3 < s["chi2_red_constant"] < 3.5
    naive = _naive_dmag(frames, LC_X, LC_Y)
    assert np.abs(naive).max() > 0.2  # >> 0.02: colour dominates without matching


def test_16_star_twice_as_bright_in_one_pass_gives_minus_0p75_mag(fake_process):
    """A real 2x brightening in pass 2 (and only there) must show up as dmag = -2.5 log10(2) in that
    pass, even with the colour term present; other passes see a template contaminated by the bright
    pass, so only the flagged pass is asserted quantitatively and chi2 must flag the variability."""
    rng = np.random.default_rng(16)
    frames = fake_process.group_by_pass(_lc_frames(rng, n_pass=2, scale=lambda p: 2.0 if p == 1 else 1.0))
    pts = motion.lambda_matched_lightcurve(frames, LC_X, LC_Y)
    s = motion.lightcurve_summary(pts)
    d = {e["pass"]: e["median_dmag"] for e in s["per_pass_dmag"]}
    assert d[1] == pytest.approx(-0.75, abs=0.05)
    assert d[0] == pytest.approx(+0.75, abs=0.05)  # the same pair seen from the other side
    assert s["chi2_red_constant"] > 10


def test_17_brightening_in_one_of_four_passes_is_isolated(fake_process):
    """With 4 passes the template for pass 2 is the mean of the three constant passes, so its dmag is
    exactly the -0.75 brightening; the others see a mildly contaminated template (dmag > 0 but small)."""
    rng = np.random.default_rng(17)
    frames = fake_process.group_by_pass(_lc_frames(rng, scale=lambda p: 2.0 if p == 2 else 1.0))
    s = motion.lightcurve_summary(motion.lambda_matched_lightcurve(frames, LC_X, LC_Y))
    d = {e["pass"]: e["median_dmag"] for e in s["per_pass_dmag"]}
    assert d[2] == pytest.approx(-0.75, abs=0.05)
    assert s["chi2_red_constant"] > 10


def test_18_frames_without_lambda_match_are_skipped_and_logged(fake_process, caplog):
    """A frame with no other-pass frame at its wavelength has no template; its raw flux would carry
    the colour into the curve, so it is dropped and the drop is counted in the log."""
    rng = np.random.default_rng(18)
    frames = _lc_frames(rng, n_pass=3, lams=(1.0, 2.0))
    frames.append(make_frame(render((NPIX, NPIX), [(LC_X, LC_Y, 400.0)], rng=rng), 60500, 7.0))  # lone lambda
    with caplog.at_level(logging.INFO, logger="backend.motion"):
        pts = motion.lambda_matched_lightcurve(frames, LC_X, LC_Y)
    assert len(pts) == len(frames) - 1
    assert all(p["lambda_um"] != 7.0 for p in pts)
    assert "n_skipped_no_match=1" in caplog.text


def test_19_faint_template_is_skipped_and_counted(fake_process, caplog):
    """With no source the template flux is ~ noise (f_t < 3 e_t): the ratio would be meaningless."""
    rng = np.random.default_rng(19)
    frames = [make_frame(render((NPIX, NPIX), [], rng=rng), 60000 + 100 * p, 1.0) for p in range(3)]
    with caplog.at_level(logging.INFO, logger="backend.motion"):
        pts = motion.lambda_matched_lightcurve(frames, LC_X, LC_Y)
    assert pts == []
    assert "n_in=3 n_out=0" in caplog.text


def test_20_summary_edge_cases():
    """chi2 needs at least 2 points; an empty curve must not crash the API."""
    assert np.isnan(motion.lightcurve_summary([])["chi2_red_constant"])
    one = [{"t_yr": 2025.0, "lambda_um": 1.0, "pass": 0, "ratio": 1.0, "err": 0.1, "dmag": 0.0}]
    s = motion.lightcurve_summary(one)
    assert np.isnan(s["chi2_red_constant"])
    assert s["per_pass_dmag"] == [{"pass": 0, "median_dmag": 0.0, "n": 1}]


def _blank_cores(frames, radius=2.5):
    yy, xx = np.indices((NPIX, NPIX))
    core = (xx - LC_X) ** 2 + (yy - LC_Y) ** 2 < radius**2
    for f in frames:
        f.data[core] = np.nan
        f.var[core] = np.nan
    return frames


def test_21_saturated_core_switches_the_light_curve_to_ring_photometry(fake_process):
    """Why: R Dra's delivered core is NaN, so the aperture light curve came back EMPTY for a headline target. With
    the core blanked the curve must switch (for ALL frames, never a mix) to PSF-wing photometry, still show a 2x
    brightening as -0.75 mag, and say so in the summary."""
    rng = np.random.default_rng(21)
    # a bright, wide-winged star (sigma 1.6 px, like R Dra's saturated PSF): the light is NOT all inside the blanked core
    frames = []
    for p in range(2):
        for lam in LC_LAMS:
            amp = 3000.0 * _g(lam) * (2.0 if p == 1 else 1.0)
            frames.append(
                make_frame(
                    render((NPIX, NPIX), [(LC_X, LC_Y, amp)], rng=rng, psf=1.6),
                    60000 + 100 * p + 5 * lam,
                    lam,
                )
            )
    frames = fake_process.group_by_pass(_blank_cores(frames))
    assert motion.lambda_matched_lightcurve(frames, LC_X, LC_Y, ring=False) == []  # the failure being fixed
    pts = motion.lambda_matched_lightcurve(frames, LC_X, LC_Y)  # auto
    assert pts and all(p.get("ring") for p in pts)
    s = motion.lightcurve_summary(pts)
    assert s["photometry"] == "ring"
    d = {e["pass"]: e["median_dmag"] for e in s["per_pass_dmag"]}
    assert d[1] == pytest.approx(-0.75, abs=0.1)


def test_22_intact_core_keeps_aperture_photometry(fake_process):
    """Ring mode is a fallback only: a normal star must keep the more accurate aperture photometry."""
    rng = np.random.default_rng(22)
    frames = fake_process.group_by_pass(_lc_frames(rng, n_pass=2))
    pts = motion.lambda_matched_lightcurve(frames, LC_X, LC_Y)
    assert pts and not any(p.get("ring") for p in pts)
    assert "photometry" not in motion.lightcurve_summary(pts)
