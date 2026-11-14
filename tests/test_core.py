"""Run with:  python -m pytest -q   (from the project folder)."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from drift import clean, demo, pipeline, targets, verdict  # noqa: E402

WISE = targets.PRESETS["wise0855"]


def test_wise_epoch_correction_moves_about_130_arcsec_west():
    ra1, dec1 = targets.propagate(WISE.ra, WISE.dec, WISE.pmra, WISE.pmdec, WISE.epoch, 2026.3)
    d_ra = (ra1 - WISE.ra) * np.cos(np.radians(WISE.dec)) * 3600
    assert -135 < d_ra < -125          # west (RA decreases), ~16 yr x 8.1"/yr
    assert 8 < (dec1 - WISE.dec) * 3600 < 14


def test_cleaning_removes_zodi_and_reprojects_north_up():
    raw, ra, dec = demo.synthetic_raw(WISE, detector="SPHEREx-D6")
    cube = clean.build_cube(raw, ra, dec, WISE.fov_arcsec, "SPHEREx-D6", "SYNTHETIC")
    assert cube.cube.shape[0] == len(raw)
    med = np.nanmedian(cube.cube)
    assert abs(med) < 0.5               # ~40 of zodi + offset removed
    assert 0.9 < np.isfinite(cube.cube).mean() <= 1.0


def _cube(seed, peak):
    raw, ra, dec = demo.synthetic_raw(WISE, detector="SPHEREx-D6", seed=seed, mover_peak=peak)
    return clean.build_cube(raw, ra, dec, WISE.fov_arcsec, "SPHEREx-D6", "SYNTHETIC")


def _expected(cube):
    f = lambda ep: cube.wcs.world_to_pixel_values(*targets.target_position(WISE, ep))
    m = targets.mjd_to_year(cube.mjd.mean())
    a, b, c = f(m + .5), f(m - .5), f(m)
    return (float(a[0] - b[0]), float(a[1] - b[1])), (float(c[0]), float(c[1]))


def test_injected_mover_is_detected():
    c = _cube(3, 2.0)
    v, xy = _expected(c)
    r = verdict.assess(c, "target", v0=v, center=xy, n_null=200)
    assert r["confidence"] > 0.97 and r["moving"]
    assert r["checks"]["halves"]["ok"] and r["checks"]["leave_one_out"]["ok"]


def test_noise_only_rarely_flags():
    hits = 0
    for s in range(20):
        c = _cube(500 + s, 0.0)
        v, xy = _expected(c)
        hits += verdict.assess(c, "target", v0=v, center=xy, n_null=100, seed=s)["fap"] < 0.05
    assert hits <= 3                    # expect ~5% at most


def test_static_star_is_not_called_a_mover():
    c = _cube(4, 0.0)
    r = verdict.assess(c, "target", v0=(0.0, 0.0), center=(c.cube.shape[2] // 2,) * 2, n_null=100)
    assert r["confidence"] < 0.99


def test_full_pipeline_demo_all_presets():
    for t in targets.PRESETS.values():
        out = pipeline.run(pipeline.spec_from_target(t), demo=True, n_null=60)
        assert out["payload"]["views"]["frames"] and out["verdict"]["n_frames"] >= 4
        assert out["payload"]["views"]["frames"][0]["raws"]
        assert out["payload"]["views"]["lmdiff"]
        assert out["payload"]["traj_best"] is not None
        assert out["payload"]["spectrum"] is not None



def test_saturated_core_reconstruction_preserves_bad_pixels():
    img = np.zeros((10, 10))
    # Bright star at (5, 5) with NaN core surrounded by bright wings (> 5 sigma)
    img[4:7, 4:7] = 12.0
    img[5, 5] = np.nan
    # Isolated bad detector pixel at (1, 1) surrounded by ~0 sky
    img[1, 1] = np.nan
    rec, sat_core, rem_bad = clean_or_render_reconstruct(img)
    assert sat_core[5, 5] and np.isfinite(rec[5, 5]) and rec[5, 5] > 12.0
    assert rem_bad[1, 1] and not sat_core[1, 1]


def clean_or_render_reconstruct(img):
    from drift.render import reconstruct_saturated_cores
    return reconstruct_saturated_cores(img, 1.0)

