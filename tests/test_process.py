import io

import numpy as np
import pytest
from astropy.stats import sigma_clipped_stats

from backend import config, process
from backend.models import Frame, ImageRef, make_grid
from tests.fixtures import make_fake_cutout

RA0, DEC0 = 133.795, -7.245


def _ref(t=60000.0, det=6, obs="e_D6"):
    return ImageRef(obs, "e", det, t, "DR1", "v1", "x")


def _frame(data, var=None, t=60000.0, lam=4.5):
    return Frame(_ref(t), np.asarray(data, np.float32), var, np.zeros(np.shape(data), np.uint32), lam)


def _bytes(hdul):
    b = io.BytesIO()
    hdul.writeto(b)
    return b.getvalue()


@pytest.fixture(autouse=True)
def _lam(monkeypatch):
    monkeypatch.setattr(process, "wavelength_at", lambda h, ra, dec: 4.5)


def _centroid(a, box=5):
    a = np.nan_to_num(a)
    y0, x0 = np.unravel_index(np.argmax(a), a.shape)
    h = box // 2
    sub = a[y0 - h : y0 + h + 1, x0 - h : x0 + h + 1]
    yy, xx = np.mgrid[y0 - h : y0 + h + 1, x0 - h : x0 + h + 1]
    return (xx * sub).sum() / sub.sum(), (yy * sub).sum() / sub.sum(), (x0, y0)


def test_alignment():
    """Misalignment makes static stars appear to move, which is a false discovery."""
    grid = make_grid(RA0, DEC0, 0.1)
    star = [(RA0 + 0.01, DEC0 + 0.005, 1000.0)]
    a = make_fake_cutout(RA0, DEC0, npix=64, stars=star, noise=0.01)
    b = make_fake_cutout(RA0 + 0.004, DEC0, npix=64, rot_deg=37, stars=star, noise=0.01, seed=1)
    fa = process.process_cutout(_bytes(a), _ref(), grid)
    fb = process.process_cutout(_bytes(b), _ref(), grid)
    xa, ya, pa = _centroid(fa.data)
    xb, yb, pb = _centroid(fb.data)
    assert pa == pb
    assert abs(xa - xb) < 0.2 and abs(ya - yb) < 0.2


def test_motion_preserved():
    """The tool's entire purpose: real motion must survive processing, east = lower column."""
    grid = make_grid(RA0, DEC0, 0.1)
    s = config.PIXSCALE_ARCSEC / 3600
    dec_s = DEC0 + 0.005
    ra1 = RA0 + 0.005
    ra2 = ra1 + 2 * s / np.cos(np.deg2rad(dec_s))  # 2 px east
    a = make_fake_cutout(RA0, DEC0, npix=64, stars=[(ra1, dec_s, 1000.0)], noise=0.01)
    b = make_fake_cutout(RA0, DEC0, npix=64, stars=[(ra2, dec_s, 1000.0)], noise=0.01, seed=1)
    xa, ya, _ = _centroid(process.process_cutout(_bytes(a), _ref(), grid).data)
    xb, yb, _ = _centroid(process.process_cutout(_bytes(b), _ref(), grid).data)
    assert xb - xa == pytest.approx(-2.0, abs=0.2)  # east is lower column in FITS orientation
    assert yb - ya == pytest.approx(0.0, abs=0.2)


def test_flag_masking():
    """Flagged pixels must not leak into blinks or co-adds."""
    if not config.FLAG_BITS_BAD:
        pytest.skip("flag bits not configured")
    grid = make_grid(RA0, DEC0, 0.1)
    h = make_fake_cutout(RA0, DEC0, npix=64)
    h["FLAGS"].data[30:33, 30:33] = 1 << config.FLAG_BITS_BAD[0]
    f = process.process_cutout(_bytes(h), _ref(), grid)
    c = grid.npix // 2
    assert np.isnan(f.data[c - 1 : c + 2, c - 1 : c + 2]).all()


def test_bad_mask_explicit_bits():
    """bad_mask works with explicit bits even while config.FLAG_BITS_BAD is [VERIFY]-empty."""
    fl = np.zeros((4, 4), np.uint32)
    fl[1, 1] = 1 << 3
    fl[2, 2] = 1 << 5
    m = process.bad_mask(fl, (3,))
    assert m[1, 1] and not m[2, 2] and m.sum() == 1


def test_flag_masking_monkeypatched(monkeypatch):
    """Same masking path as config, exercised with a patched bit set."""
    monkeypatch.setattr(config, "FLAG_BITS_BAD", (4,))
    grid = make_grid(RA0, DEC0, 0.1)
    h = make_fake_cutout(RA0, DEC0, npix=64)
    h["FLAGS"].data[30:33, 30:33] = 1 << 4
    f = process.process_cutout(_bytes(h), _ref(), grid)
    c = grid.npix // 2
    assert np.isnan(f.data[c - 1 : c + 2, c - 1 : c + 2]).all()


def test_zodi_subtraction():
    """Background of 10 plus zodi 5: subtracting zodi then the median must leave ~0."""
    grid = make_grid(RA0, DEC0, 0.1)
    h = make_fake_cutout(RA0, DEC0, npix=64, background=10.0, zodi=5.0)
    f = process.process_cutout(_bytes(h), _ref(), grid)
    _, med, _ = sigma_clipped_stats(f.data, sigma=3)
    assert abs(med) < 0.1


def test_pass_grouping():
    """Passes are separated by gaps; wrong grouping would co-add different epochs."""
    ts = [201, 0, 2, 1, 400, 200]
    out = process.group_by_pass([_frame(np.zeros((2, 2)), t=t) for t in ts])
    assert [f.ref.t_mjd for f in out] == [0, 1, 2, 200, 201, 400]
    assert [f.pass_idx for f in out] == [0, 0, 0, 1, 1, 2]


def test_coadd_snr():
    """The co-add mode is pointless if it doesn't raise SNR."""
    rng = np.random.default_rng(0)
    fr = [_frame(rng.normal(0, 1, (100, 100)), var=np.ones((100, 100), np.float32)) for _ in range(16)]
    out, var = process.coadd(fr)
    assert out.std() == pytest.approx(0.25, abs=0.03)
    assert var[0, 0] == pytest.approx(1 / 16)


def test_coadd_weighting():
    """Noisy frames must not dominate."""
    a = _frame(np.zeros((2, 2)), var=np.ones((2, 2), np.float32))
    b = _frame(np.full((2, 2), 10.0), var=np.full((2, 2), 100.0, np.float32))
    out, _ = process.coadd([a, b])
    assert out[0, 0] == pytest.approx(10 / 101, abs=1e-4)


def test_coadd_nan_handling():
    """All-NaN pixel stays NaN; partial NaN averages the rest."""
    d1 = np.array([[np.nan, np.nan], [4.0, 2.0]])
    d2 = np.array([[np.nan, 6.0], [np.nan, 4.0]])
    v = np.ones((2, 2), np.float32)
    out, var = process.coadd([_frame(d1, var=v), _frame(d2, var=v)])
    assert np.isnan(out[0, 0]) and np.isnan(var[0, 0])
    assert out[0, 1] == 6 and out[1, 0] == 4 and out[1, 1] == 3


def test_coadd_without_variance_equal_weights():
    """Missing variance falls back to a plain mean instead of crashing."""
    out, var = process.coadd([_frame(np.zeros((2, 2))), _frame(np.full((2, 2), 4.0))])
    assert var is None and out[0, 0] == 2


def test_coadd_by_pass():
    """One frame per pass with mean time and OR'd flags, as contract C5 mode=pass requires."""
    f = [_frame(np.full((2, 2), float(i)), t=t) for i, t in enumerate([0, 2, 100])]
    f[0].flags[0, 0] = 1
    f[1].flags[0, 0] = 2
    out = process.coadd_by_pass(f)
    assert [o.ref.obs_id for o in out] == ["pass0_D6", "pass1_D6"]
    assert out[0].ref.t_mjd == 1.0 and out[0].flags[0, 0] == 3 and out[0].data[0, 0] == 0.5


def test_zscale_stack_wide():
    """Per-frame limits cause flicker: one pair for the stack, deterministic."""
    rng = np.random.default_rng(1)
    st = [rng.normal(0, 1, (200, 200)), rng.normal(0, 10, (200, 200))]
    a = process.zscale_limits(st, max_samples=5000)
    b = process.zscale_limits([s.copy() for s in st], max_samples=5000)
    assert a == b and a[0] < a[1]
    assert a != process.zscale_limits(st[:1], max_samples=5000)


def test_transient_and_sur_error_do_not_blank_a_star():
    """Regression (flag-mask experiments 2026-11-14): the pipeline sets TRANSIENT/SUR_ERROR on a bright star's own
    core, so masking them erased Barnard's and RW Aur (their spectra and light curves came back empty). They must
    survive masking, while a genuinely bad pixel (HOT) in the same place must still be masked."""
    grid = make_grid(RA0, DEC0, 0.1)
    c = grid.npix // 2
    for bit_name in ("TRANSIENT", "SUR_ERROR"):
        bit = next(b for b, n in config.FLAG_BIT_NAMES.items() if n == bit_name)
        assert bit not in config.FLAG_BITS_BAD, bit_name
        h = make_fake_cutout(RA0, DEC0, npix=64)
        h["FLAGS"].data[30:33, 30:33] = 1 << bit
        f = process.process_cutout(_bytes(h), _ref(), grid)
        assert np.isfinite(f.data[c - 1 : c + 2, c - 1 : c + 2]).all(), bit_name
    hot = next(b for b, n in config.FLAG_BIT_NAMES.items() if n == "HOT")
    assert hot in config.FLAG_BITS_BAD
    h = make_fake_cutout(RA0, DEC0, npix=64)
    h["FLAGS"].data[30:33, 30:33] = 1 << hot
    f = process.process_cutout(_bytes(h), _ref(), grid)
    assert np.isnan(f.data[c - 1 : c + 2, c - 1 : c + 2]).all()
