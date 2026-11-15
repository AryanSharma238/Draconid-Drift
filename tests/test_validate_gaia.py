import asyncio
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import validate_gaia as vg

from backend import config
from backend.models import make_grid
from backend.motion import Mover

EPOCH = config.SPHEREX_MID_EPOCH


def truth_star(sid=1, ra=150.0, dec=20.0, pmra=-2000.0, pmdec=1500.0, g=12.0) -> dict:
    return {
        "source_id": sid, "ra": ra, "dec": dec, "pmra": pmra, "pmdec": pmdec,
        "phot_g_mean_mag": g, "ref_epoch": 2016.0,
    }  # fmt: skip


def mover_at(grid, ra, dec, pmra=0.0, pmdec=0.0, t0=EPOCH, ep=100.0) -> Mover:
    x, y = grid.wcs().world_to_pixel_values(ra, dec)
    return Mover(
        x0=float(x), y0=float(y), ra0=ra, dec0=dec, t0_yr=t0, vx=0, vy=0, evx=0, evy=0,
        pmra_masyr=pmra, pmdec_masyr=pmdec, epmra_masyr=ep, epmdec_masyr=ep,
        pm_total_arcsec_yr=math.hypot(pmra, pmdec) / 1000, snr_stack=10, motion_sig=5, n_pass_used=3,
    )  # fmt: skip


def test_pos_at_sign_east_and_north():
    # Why: pmra > 0 must move the star to larger RA (east), pmdec > 0 to larger Dec.
    t = truth_star(pmra=1000.0, pmdec=1000.0)
    ra, dec = vg.pos_at(t, 2026.0)
    assert ra > t["ra"] and dec > t["dec"]


def test_sample_spread_caps_per_cell_and_is_deterministic():
    rng = np.random.default_rng(0)
    rows = [
        {"source_id": i, "ra": float(rng.uniform(0, 5)), "dec": float(rng.uniform(0, 5))} for i in range(200)
    ]  # all crammed into very few nside=4 cells
    a = vg.sample_spread(rows, n=150, per_cell=3)
    assert a == vg.sample_spread(rows, n=150, per_cell=3)
    import astropy.units as u
    from astropy_healpix import HEALPix

    hp = HEALPix(nside=4, order="nested")
    cells = hp.lonlat_to_healpix([r["ra"] for r in a] * u.deg, [r["dec"] for r in a] * u.deg)
    assert max(np.bincount(cells)) <= 3
    assert len(a) < 150  # the cap actually bit


def test_build_row_found_with_pm_errors():
    t = truth_star()
    ra, dec = vg.pos_at(t, EPOCH)
    grid = make_grid(ra, dec, 0.1)
    m = mover_at(grid, *vg.pos_at(t, EPOCH), pmra=-1900.0, pmdec=1550.0, ep=50.0)
    row = vg.build_row(t, [m], grid, gaia_rows=[])
    assert row["found"] == 1 and row["n_false"] == 0
    assert row["dpmra"] == pytest.approx(100.0) and row["dpmdec"] == pytest.approx(50.0)
    assert row["sig"] == pytest.approx(2.0)  # max(|100|/50, |50|/50)


def test_truth_is_matched_at_its_propagated_position_not_its_gaia_position():
    # Why: Gaia's position is epoch 2016; matching without propagation would miss by the whole PM
    # displacement (~25" = 4 px here) and report every fast star as unrecovered.
    t = truth_star()
    ra, dec = vg.pos_at(t, EPOCH)
    grid = make_grid(ra, dec, 0.1)
    at_gaia_epoch = mover_at(grid, t["ra"], t["dec"])
    assert vg.build_row(t, [at_gaia_epoch], grid, [])["found"] == 0
    assert vg.build_row(t, [mover_at(grid, ra, dec)], grid, [])["found"] == 1


def test_false_movers_exclude_gaia_counterparts():
    t = truth_star()
    ra, dec = vg.pos_at(t, EPOCH)
    grid = make_grid(ra, dec, 0.1)
    matched = mover_at(grid, ra, dec)
    neighbour = {"source_id": 2, "ra": ra + 0.01, "dec": dec + 0.01, "pmra": float("nan"),
                 "pmdec": float("nan"), "phot_g_mean_mag": 15.0, "ref_epoch": 2016.0}  # fmt: skip
    has_counterpart = mover_at(grid, ra + 0.01, dec + 0.01)
    orphan = mover_at(grid, ra - 0.02, dec - 0.01)
    row = vg.build_row(t, [matched, has_counterpart, orphan], grid, [neighbour])
    assert row["n_movers"] == 3 and row["n_false"] == 1  # only the orphan


def test_missing_mover_is_recorded_as_not_found():
    t = truth_star()
    grid = make_grid(*vg.pos_at(t, EPOCH), 0.1)
    row = vg.build_row(t, [], grid, [])
    assert row["found"] == 0 and "dpmra" not in row


def _fake_measure(fail_ids=(), calls=None):
    async def measure(ra, dec, size, det):
        grid = make_grid(ra, dec, size)
        if calls is not None:
            calls.append((ra, dec))
        if any(abs(ra - vg.pos_at(t, EPOCH)[0]) < 1e-9 for t in fail_ids):
            raise RuntimeError("IRSA 502")
        return [mover_at(grid, ra, dec, pmra=-2000.0, pmdec=1500.0)], grid

    return measure


def test_run_fields_is_resumable_and_retries_errors(tmp_path):
    truths = [truth_star(sid=i, ra=100.0 + 10 * i) for i in range(3)]
    res = tmp_path / "results.csv"
    cone = lambda ra, dec, r: []

    c1 = asyncio.run(vg.run_fields(truths, _fake_measure(fail_ids=[truths[1]]), cone, results_path=res))
    assert c1 == {"skipped": 0, "ok": 2, "error": 1}
    assert vg.done_ids(res) == {"0", "2"}  # the errored field is NOT done

    calls: list = []
    c2 = asyncio.run(vg.run_fields(truths, _fake_measure(calls=calls), cone, results_path=res))
    assert c2 == {"skipped": 2, "ok": 1, "error": 0}
    assert len(calls) == 1  # only the failed field was re-measured
    latest = vg.latest_results(res)
    assert len(latest) == 3 and all(r["status"] == "ok" for r in latest)


def test_error_row_carries_the_reason(tmp_path):
    res = tmp_path / "r.csv"
    t = truth_star(sid=7)
    asyncio.run(vg.run_fields([t], _fake_measure(fail_ids=[t]), lambda *a: [], results_path=res))
    (row,) = vg.read_csv(res)
    assert row["status"] == "error" and "IRSA 502" in row["error"]


def test_summarize_reports_recovery_and_false_movers(tmp_path):
    res = tmp_path / "r.csv"
    ts = [truth_star(sid=i, g=10.0 + i) for i in range(4)]

    async def measure(ra, dec, size, det):
        grid = make_grid(ra, dec, size)
        return ([mover_at(grid, ra, dec, pmra=-2000.0, pmdec=1500.0)] if measure.n.pop() else []), grid

    measure.n = [1, 1, 1, 0]  # 3 of 4 recovered
    asyncio.run(vg.run_fields(ts, measure, lambda *a: [], results_path=res, concurrency=1))
    text = vg.summarize(vg.latest_results(res))
    assert "3/4 = 0.75" in text
    assert "False movers per field" in text
