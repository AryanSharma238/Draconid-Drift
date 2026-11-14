"""mode=lmdiff, /api/motion and /api/lightcurve over stubbed index + fetch (stubs from test_sequence). No network.

The scenes are synthetic: the real backend.motion algorithms run on them, except the two E4 light-curve
functions (Track D, may not be merged) which are monkeypatched where a test needs them.
"""

import httpx
import numpy as np
import orjson
import pytest

from backend import cache, config, fetch, index, motion
from backend.app import app
from backend.models import Frame, make_grid
from backend.sequence import motion_result
from tests.test_sequence import DEC, DET, MJDS, RA, SIZE, Stubs, drain, f16, make_refs

Q = {"ra": RA, "dec": DEC, "size": SIZE, "det": DET}
GRID = make_grid(RA, DEC, SIZE)
NPIX = GRID.npix
C = (NPIX - 1) // 2  # centre pixel (internal coords)
PASS_START = [0, 3, 7]


def gauss(x0: float, y0: float, amp: float, sigma: float = config.PSF_SIGMA_PX) -> np.ndarray:
    yy, xx = np.mgrid[0:NPIX, 0:NPIX]
    return amp * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * sigma**2))


class SceneStubs(Stubs):
    """Stubs whose frames come from `scene(ref, grid) -> (image, lambda_um)`; noise is tiny and seeded."""

    def __init__(self, refs, scene, fail=frozenset()):
        super().__init__(refs, fail)
        self.scene = scene

    async def get_frames(self, refs, grid):
        self.fetch_calls += 1
        for i in reversed(range(len(refs))):
            if i in self.fail:
                yield i, None, "TimeoutError: stub"
                continue
            img, lam = self.scene(refs[i], grid)
            noise = np.random.default_rng(i).normal(0, 0.05, img.shape)
            shape = img.shape
            yield (
                i,
                Frame(
                    refs[i],
                    (img + noise).astype(np.float32),
                    np.full(shape, 0.0025, np.float32),
                    np.zeros(shape, np.uint32),
                    lam,
                ),
                None,
            )


def install(monkeypatch, tmp_path, stubs: Stubs) -> Stubs:
    monkeypatch.setattr(config, "STACK_CACHE_DIR", tmp_path / "stacks")
    monkeypatch.setattr(index, "find_images", stubs.find_images)
    monkeypatch.setattr(fetch, "get_frames", stubs.get_frames)
    return stubs


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


# ------------------------------------------------------------------ lmdiff
def pass_k(ref) -> tuple[int, int]:
    i = int(ref.exposure_id[1:])
    p = max(q for q in range(3) if i >= PASS_START[q])
    return p, i - PASS_START[p]


def star_scene(ref, grid):
    """A STATIC star whose flux depends on wavelength only (SPHEREx: each frame samples a different lambda).
    Frame k of every pass has lambda = 1 + k, so every frame but pass 1's 4th has a match in the other passes."""
    _, k = pass_k(ref)
    lam = 1.0 + k
    return gauss(C, C, 50.0 * lam), lam


@pytest.fixture
def star_stubs(monkeypatch, tmp_path):
    return install(monkeypatch, tmp_path, SceneStubs(make_refs(), star_scene))


def star_values(msgs: list[dict]) -> list[float]:
    return [float(f16(m["data"], NPIX)[NPIX - 1 - C, C]) for m in msgs if m["type"] == "frame"]


async def test_lmdiff_removes_the_static_star_that_mode_all_shows(star_stubs):
    # why: this is the point of the mode. Flux differences between frames are colour (lambda), not variability;
    # a static star must vanish in the lambda-matched residuals while it swings by 100 counts in mode=all.
    raw = star_values(await drain("all"))
    assert max(raw) - min(raw) > 90  # 50*lambda, lambda = 1..4
    msgs = await drain("lmdiff")
    resid = star_values(msgs)
    assert resid and max(abs(v) for v in resid) < 1.0  # float16 + 0.05 noise; the star itself is 50..200


async def test_lmdiff_frame_fields_and_exact_n_expected(star_stubs):
    msgs = await drain("lmdiff")
    types_ = [m["type"] for m in msgs]
    assert types_[0] == "status" and types_.index("header") > 0 and types_[-1] == "done"
    hdr, done = next(m for m in msgs if m["type"] == "header"), msgs[-1]
    frames = [m for m in msgs if m["type"] == "frame"]
    # the 4th frame of pass 1 (lambda = 4) has no match in another pass and is dropped; n_expected says so
    assert hdr["mode"] == "lmdiff" and hdr["n_expected"] == len(MJDS) - 1 == len(frames) == done["n_frames"]
    assert hdr["zscale"] is None and done["zscale"] is None  # diffs: symmetric stretch is client-side (C9)
    assert [m["i"] for m in frames] == list(range(len(frames)))
    assert [m["t_mjd"] for m in frames] == sorted(m["t_mjd"] for m in frames)
    assert all(m["kind"] == "single" and "flags" in m and m["n_coadd"] == 1 for m in frames)
    assert {m["pass"] for m in frames} == {0, 1, 2}
    assert {m["obs_id"] for m in frames} <= {r.obs_id for r in star_stubs.refs}


async def test_lmdiff_served_from_l2_second_time(star_stubs, client):
    r1 = await client.get("/api/sequence", params={**Q, "mode": "lmdiff"})
    assert r1.status_code == 200 and star_stubs.fetch_calls == 1
    r2 = await client.get("/api/sequence", params={**Q, "mode": "lmdiff"})
    assert star_stubs.fetch_calls == 1 and r2.headers["content-encoding"] == "gzip"
    assert orjson.loads(r2.text.splitlines()[0])["source"] == "cache"


async def test_lmdiff_single_pass_gives_no_frames_and_is_not_cached(monkeypatch, tmp_path):
    # why: with one pass there is no other-pass template; emitting raw frames as "residuals" would be a lie
    s = install(monkeypatch, tmp_path, SceneStubs(make_refs(MJDS[:3]), star_scene))
    msgs = await drain("lmdiff")
    hdr = next(m for m in msgs if m["type"] == "header")
    assert hdr["n_expected"] == 0 and msgs[-1]["n_frames"] == 0 and s.fetch_calls == 1
    assert not config.STACK_CACHE_DIR.exists() or not list(config.STACK_CACHE_DIR.rglob("*.gz"))


async def test_lmdiff_missing_index_is_error_line(monkeypatch, tmp_path):
    async def boom(ra, dec, det):
        raise FileNotFoundError("s3_footprints.csv.gz")

    monkeypatch.setattr(config, "STACK_CACHE_DIR", tmp_path / "stacks")
    monkeypatch.setattr(index, "find_images", boom)
    msgs = await drain("lmdiff")
    assert [m["type"] for m in msgs] == ["status", "header", "error", "done"]


# ------------------------------------------------------------------ /api/motion
MOVER_T = [60000.0, 60000.5, 60200.0, 60200.5, 60400.0, 60400.5]
MOVER_X0, MOVER_Y0 = 20.0, 30.0
MOVER_VX, MOVER_VY = 2.0, 0.0  # px/yr, +x = west => pmra < 0
STARS = [(8 + 9 * i, 8 + 7 * (i % 5)) for i in range(5)] + [(10 + 9 * i, 50 - 5 * (i % 3)) for i in range(5)]


def mover_xy(t_mjd: float) -> tuple[float, float]:
    dt = (t_mjd - 60200.25) / 365.25
    return MOVER_X0 + MOVER_VX * dt, MOVER_Y0 + MOVER_VY * dt


def mover_scene(ref, grid):
    img = sum(gauss(x, y, 150.0) for x, y in STARS)  # bright static stars: registration + control
    mx, my = mover_xy(ref.t_mjd)
    return img + gauss(mx, my, 40.0), 4.5


@pytest.fixture
def mover_stubs(monkeypatch, tmp_path):
    return install(monkeypatch, tmp_path, SceneStubs(make_refs(MOVER_T), mover_scene))


C11_KEYS = {"v", "ra", "dec", "size_deg", "det", "npix", "t0_yr", "passes", "registration_ok", "grid",
            "elapsed_ms", "movers"}  # fmt: skip
MOVER_KEYS = {"x0", "y0", "ra0", "dec0", "pmra_masyr", "pmdec_masyr", "epmra_masyr", "epmdec_masyr",
              "pm_total_arcsec_yr", "snr_stack", "motion_sig", "n_pass_used", "track"}  # fmt: skip


async def test_motion_finds_synthetic_mover_in_c11_shape(mover_stubs, client):
    r = await client.get("/api/motion", params=Q)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    j = r.json()
    assert set(j) == C11_KEYS and j["v"] == 1 and j["det"] == DET and j["npix"] == NPIX
    assert len(j["passes"]) == 3 and all(set(p) == {"t_yr", "n", "dx", "dy", "sigma"} for p in j["passes"])
    assert [p["n"] for p in j["passes"]] == [2, 2, 2]
    assert j["registration_ok"] is True  # 10 bright static stars: the offsets were measured, not defaulted
    assert set(j["grid"]) == {"vmax", "step", "n"} and j["grid"]["vmax"] == pytest.approx(
        config.VMAX_PX_PER_YR
    )
    assert set(j["elapsed_ms"]) == {"stack", "search", "measure"}
    # the mover, and only the mover: the 10 static stars must not be reported as moving
    movers = j["movers"]
    assert len(movers) == 1 and set(movers[0]) == MOVER_KEYS
    m = movers[0]
    t_mid = 60200.25
    assert m["x0"] == pytest.approx(mover_xy(t_mid)[0], abs=0.5) and m["y0"] == pytest.approx(
        MOVER_Y0, abs=0.5
    )
    # +x is west: pmra (east-positive) must be NEGATIVE for vx > 0. Truth: -2 px/yr * 6150 mas/px
    truth = -MOVER_VX * config.PIXSCALE_ARCSEC * 1000
    assert m["pmra_masyr"] == pytest.approx(truth, rel=0.15) and abs(m["pmdec_masyr"]) < 0.15 * abs(truth)
    assert m["pm_total_arcsec_yr"] == pytest.approx(abs(truth) / 1000, rel=0.15)
    assert m["n_pass_used"] == 3 and len(m["track"]) == 3 and all(len(t) == 3 for t in m["track"])
    assert m["track"][0][1] < m["track"][-1][1]  # x increases with time


async def test_motion_second_call_served_from_l2_without_fetching(mover_stubs, client):
    # why: motion takes seconds; the demo targets must answer instantly from precompute
    r1 = await client.get("/api/motion", params=Q)
    assert mover_stubs.fetch_calls == 1 and "max-age" in r1.headers["cache-control"]
    key = cache.json_key("motion", GRID, DET, index.data_version())
    assert cache.json_path(key).is_file() and key.startswith("motion/")
    r2 = await client.get("/api/motion", params=Q)
    assert mover_stubs.fetch_calls == 1 and mover_stubs.find_calls == 1
    assert r2.json() == r1.json()


async def test_motion_one_pass_is_an_empty_c11_result_not_a_500(monkeypatch, tmp_path, client):
    s = install(monkeypatch, tmp_path, SceneStubs(make_refs(MOVER_T[:2]), mover_scene))
    r = await client.get("/api/motion", params=Q)
    j = r.json()
    assert r.status_code == 200 and set(j) == C11_KEYS | {"error"}
    assert j["movers"] == [] and j["registration_ok"] is False and ">=2 passes" in j["error"]
    assert r.headers["cache-control"] == "no-store"  # an empty result must not be pinned in browsers
    await client.get("/api/motion", params=Q)
    assert s.fetch_calls == 2  # ... nor in L2: a later index update could make it computable


async def test_motion_no_coverage_is_an_empty_result(monkeypatch, tmp_path, client):
    install(monkeypatch, tmp_path, SceneStubs([], mover_scene))
    r = await client.get("/api/motion", params=Q)
    assert r.status_code == 200 and r.json()["movers"] == [] and "error" in r.json()


async def test_motion_missing_index_is_json_503(monkeypatch, tmp_path, client):
    async def boom(ra, dec, det):
        raise FileNotFoundError("s3_footprints.csv.gz")

    monkeypatch.setattr(config, "STACK_CACHE_DIR", tmp_path / "stacks")
    monkeypatch.setattr(index, "find_images", boom)
    r = await client.get("/api/motion", params=Q)
    assert r.status_code == 503 and "s3_footprints" in r.json()["error"]


async def test_motion_unexpected_failure_is_json_500_and_not_cached(mover_stubs, monkeypatch, client):
    def explode(frames):
        raise RuntimeError("boom")

    monkeypatch.setattr(motion, "pass_stack", explode)
    r = await client.get("/api/motion", params=Q)
    assert r.status_code == 500 and "boom" in r.json()["error"]
    assert not list(config.STACK_CACHE_DIR.rglob("*.json"))


async def test_motion_too_many_failed_frames_computes_but_does_not_cache(mover_stubs):
    mover_stubs.fail = {0, 1}  # 2/6 >= 20 %
    res = await motion_result(RA, DEC, SIZE, DET)
    assert res.status == 200 and res.error is None and not res.cacheable and res.n_failed == 2
    assert not list(config.STACK_CACHE_DIR.rglob("*.json"))


@pytest.mark.parametrize("bad", [{"size": 0.33}, {"det": 7}, {"dec": 95}])
async def test_motion_and_lightcurve_validate_params(client, mover_stubs, bad):
    for route in ("/api/motion", "/api/lightcurve"):
        r = await client.get(route, params={**Q, **bad})
        assert r.status_code == 422 and isinstance(r.json()["error"], str)
    assert mover_stubs.find_calls == 0


# ------------------------------------------------------------------ /api/lightcurve
def fake_lc(calls: list):
    def lambda_matched_lightcurve(frames, x, y, dlam_frac=config.LAMBDA_MATCH_FRAC):
        calls.append((len(frames), x, y, {f.pass_idx for f in frames}))
        return [
            {"t_yr": 2025.0 + i, "lambda_um": 1.0 + i, "pass": i % 3, "ratio": 1.0, "err": 0.02, "dmag": 0.0}
            for i in range(4)
        ]

    def lightcurve_summary(points):
        return {
            "per_pass_dmag": [{"pass": 0, "median_dmag": 0.0, "n": len(points)}],
            "chi2_red_constant": 1.1,
        }

    return lambda_matched_lightcurve, lightcurve_summary


async def test_lightcurve_shape_and_centre_pixel(monkeypatch, tmp_path, client):
    s = install(monkeypatch, tmp_path, SceneStubs(make_refs(), star_scene))
    calls: list = []
    f, g = fake_lc(calls)
    monkeypatch.setattr(motion, "lambda_matched_lightcurve", f, raising=False)
    monkeypatch.setattr(motion, "lightcurve_summary", g, raising=False)
    r = await client.get("/api/lightcurve", params=Q)
    j = r.json()
    assert r.status_code == 200 and set(j) == {"points", "summary"}
    assert len(j["points"]) == 4 and set(j["points"][0]) == {
        "t_yr",
        "lambda_um",
        "pass",
        "ratio",
        "err",
        "dmag",
    }
    assert set(j["summary"]) == {"per_pass_dmag", "chi2_red_constant"}
    # why: x, y are INTERNAL pixel coords (C4, row 0 = south), 0-based, of the request centre = the grid centre
    n, x, y, passes = calls[0]
    assert n == len(MJDS) and x == pytest.approx(C, abs=1e-6) and y == pytest.approx(C, abs=1e-6)
    assert passes == {0, 1, 2}  # pass_idx assigned before the call
    r2 = await client.get("/api/lightcurve", params=Q)  # L2
    assert s.fetch_calls == 1 and r2.json() == j
    assert cache.json_path(cache.json_key("lightcurve", GRID, DET, index.data_version())).is_file()


async def test_lightcurve_501_when_motion_functions_missing(monkeypatch, tmp_path, client):
    # why: Track D may not be merged; the failure must be loud and specific, not a 500 with a traceback
    s = install(monkeypatch, tmp_path, SceneStubs(make_refs(), star_scene))
    monkeypatch.delattr(motion, "lambda_matched_lightcurve", raising=False)
    monkeypatch.delattr(motion, "lightcurve_summary", raising=False)
    r = await client.get("/api/lightcurve", params=Q)
    assert r.status_code == 501 and r.json() == {"error": "lightcurve not available"}
    assert s.fetch_calls == 0 and r.headers["cache-control"] == "no-store"
    assert not list(config.STACK_CACHE_DIR.rglob("*.json"))


async def test_lightcurve_without_points_is_empty_not_cached(monkeypatch, tmp_path, client):
    install(monkeypatch, tmp_path, SceneStubs(make_refs(), star_scene))
    _, g = fake_lc([])
    monkeypatch.setattr(motion, "lambda_matched_lightcurve", lambda frames, x, y: [], raising=False)
    monkeypatch.setattr(motion, "lightcurve_summary", g, raising=False)
    r = await client.get("/api/lightcurve", params=Q)
    j = r.json()
    assert r.status_code == 200 and j["points"] == [] and j["summary"]["chi2_red_constant"] is None
    assert "error" in j and not list(config.STACK_CACHE_DIR.rglob("*.json"))
