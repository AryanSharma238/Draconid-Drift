"""GET /api/spectrum (F3, contract C6) over stubbed index + fetch. No network.

Scenes are synthetic: a Gaussian point source (sigma = config.PSF_SIGMA_PX) on tiny seeded noise. Frame k of a
detector samples a different wavelength, like the real instrument, so a flat F_nu source must give a flat spectrum.
"""

import httpx
import numpy as np
import pytest

from backend import cache, config, fetch, index, sequence
from backend.app import app
from backend.models import Frame, ImageRef, make_grid
from backend.sequence import aperture_position, spectrum_result

RA, DEC, SIZE = 133.76, -7.24, 0.1
GRID = make_grid(RA, DEC, SIZE)
NPIX = GRID.npix
C = (NPIX - 1) / 2  # centre pixel, internal coords (row 0 = south, +x = west)
FLUX = 100.0
NOISE = 0.05
APERTURE_NPIX = 13  # pixels within r = 2
MAS_PER_PX = GRID.pixscale_arcsec * 1000
FLAG_SOURCE = 1 << 21  # SOURCE: informational, not in FLAG_BITS_BAD, so it survives masking
Q = {"ra": RA, "dec": DEC, "size": SIZE}


def mjd_at(dt_yr: float) -> float:
    """MJD that is dt_yr after config.SPHEREX_MID_EPOCH (Julian years, J2000 = MJD 51544.5)."""
    return 51544.5 + (config.SPHEREX_MID_EPOCH + dt_yr - 2000.0) * 365.25


def gauss(x0: float, y0: float, flux: float = FLUX, sigma: float = config.PSF_SIGMA_PX) -> np.ndarray:
    yy, xx = np.mgrid[0:NPIX, 0:NPIX]
    g = np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * sigma**2))
    return flux * g / g.sum()


def make_refs(det: int, mjds: list[float]) -> list[ImageRef]:
    return [
        ImageRef(f"e{i}_D{det}", f"e{i}", det, m, "dr1", "l2b-v1-2026-001", f"k/{det}/{i}.fits")
        for i, m in enumerate(mjds)
    ]


class SpecStubs:
    """index.find_images / fetch.get_frames for several detectors. scene(ref, k) -> (image, lambda_um, flags | None);
    k is the exposure number. `missing` detectors raise FileNotFoundError like a not-yet-built index."""

    def __init__(self, mjds: list[float], scene, dets=config.DETECTORS, missing=()):
        self.mjds, self.scene, self.missing = mjds, scene, set(missing)
        self.dets = [d for d in dets]
        self.fetch_calls = 0

    async def find_images(self, ra, dec, det):
        if det in self.missing:
            raise FileNotFoundError(f"s3_footprints_D{det}.csv.gz")
        return make_refs(det, self.mjds) if det in self.dets else []

    async def get_frames(self, refs, grid):
        self.fetch_calls += 1
        for i, ref in enumerate(refs):
            img, lam, flags = self.scene(ref, int(ref.exposure_id[1:]))
            noise = np.random.default_rng(i + 100 * ref.detector).normal(0, NOISE, img.shape)
            yield (
                i,
                Frame(
                    ref,
                    (img + noise).astype(np.float32),
                    np.full(img.shape, NOISE**2, np.float32),
                    np.zeros(img.shape, np.uint32) if flags is None else flags,
                    lam,
                ),
                None,
            )


def install(monkeypatch, tmp_path, stubs: SpecStubs) -> SpecStubs:
    monkeypatch.setattr(config, "STACK_CACHE_DIR", tmp_path / "stacks")
    monkeypatch.setattr(index, "find_images", stubs.find_images)
    monkeypatch.setattr(fetch, "get_frames", stubs.get_frames)
    return stubs


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


PASS_MJDS = [mjd_at(dt) + 0.1 * k for dt in (-0.4, 0.0, 0.4) for k in range(3)]  # 3 passes x 3 exposures


def flat_scene(ref, k):
    # the source has the SAME flux in every frame; each (detector, exposure) samples its own wavelength
    return gauss(C, C), 0.75 + 0.5 * ref.detector + 0.07 * k, None


async def test_flat_spectrum_source_is_flat_within_errors(monkeypatch, tmp_path, client):
    # why (F3): proves the pipeline injects no wavelength- (or detector-) dependent bias into forced photometry.
    # A real SED would otherwise be indistinguishable from a processing artefact.
    install(monkeypatch, tmp_path, SpecStubs(PASS_MJDS, flat_scene, dets=(3, 6), missing=(1, 2, 4, 5)))
    r = await client.get("/api/spectrum", params=Q)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    j = r.json()
    pts = j["points"]
    assert j["dets"] == [3, 6] and j["missing_dets"] == [1, 2, 4, 5]
    assert j["n_frames"] == len(pts) == 2 * len(PASS_MJDS) and j["n_failed"] == 0
    lams = [p["lambda_um"] for p in pts]
    assert lams == sorted(lams) and len(set(lams)) == len(lams) and 2.0 < lams[0] and lams[-1] < 4.5
    yy, xx = np.mgrid[0:NPIX, 0:NPIX]
    expect = float(
        gauss(C, C)[(xx - C) ** 2 + (yy - C) ** 2 <= 4.0].sum()
    )  # r = 2 truncates 0.5 % of the PSF
    assert FLUX * 0.99 < expect < FLUX
    fluxes = np.array([p["flux"] for p in pts])
    errs = np.array([p["err"] for p in pts])
    assert errs == pytest.approx(
        NOISE * np.sqrt(APERTURE_NPIX)
    )  # sqrt(sum of the variance map in the aperture)
    assert np.all(np.abs(fluxes - expect) < 4 * errs)  # flat within errors, every point
    assert np.polyfit(lams, fluxes, 1)[0] == pytest.approx(0.0, abs=4 * errs[0] / (max(lams) - min(lams)))
    assert abs(float(np.mean(fluxes)) - expect) < 0.1  # no net bias: error of the mean is ~0.04


async def test_point_fields_unit_pass_and_flags(monkeypatch, tmp_path, client):
    def scene(ref, k):
        flags = np.zeros((NPIX, NPIX), np.uint32)
        flags[int(C), int(C)] = FLAG_SOURCE  # at the aperture-centre pixel only
        flags[0, 0] = 1  # elsewhere: must not leak into the point
        return gauss(C, C), 3.0 + 0.1 * k, flags

    install(monkeypatch, tmp_path, SpecStubs(PASS_MJDS, scene, dets=(6,), missing=(1, 2, 3, 4, 5)))
    j = (await client.get("/api/spectrum", params=Q)).json()
    assert set(j) == {"points", "unit", "n_frames", "n_failed", "dets", "missing_dets"}
    assert j["unit"] == sequence.SPECTRUM_UNIT and "aperture sum" in j["unit"]  # flux is a sum, not MJy/sr
    p = j["points"][0]
    assert set(p) == {"lambda_um", "flux", "err", "t_mjd", "det", "pass", "flags"}
    assert {q["flags"] for q in j["points"]} == {FLAG_SOURCE}
    by_t = {}
    for q in j["points"]:
        by_t.setdefault(q["pass"], set()).add(q["t_mjd"])
    assert sorted(by_t) == [0, 1, 2] and all(len(v) == 3 for v in by_t.values())
    assert {q["det"] for q in j["points"]} == {6}


# ------------------------------------------------------------------ proper motion: the aperture must follow the source
PM_DT = (-0.6, 0.0, 0.7)
PMRA, PMDEC = -20000.0, 15000.0  # mas/yr: east-positive pmra < 0 = moving WEST = +x; pmdec > 0 = NORTH = +y


def source_xy(dt: float) -> tuple[float, float]:
    """Independent statement of the convention (tasks/08-motion.md): +x = west, +y = north, pmra includes cos(dec)."""
    east_px = PMRA * dt / MAS_PER_PX
    north_px = PMDEC * dt / MAS_PER_PX
    return C - east_px, C + north_px


def pm_scene(ref, k):
    dt = PM_DT[k // 3]  # k = exposure number, 3 per pass
    x, y = source_xy(dt)
    flags = np.zeros((NPIX, NPIX), np.uint32)
    flags[round(y), round(x)] = FLAG_SOURCE  # marks the pixel the aperture centre must land on
    return gauss(x, y), 4.0 + 0.1 * k, flags


@pytest.fixture
def pm_stubs(monkeypatch, tmp_path):
    mjds = [mjd_at(dt) + 0.05 * k for dt in PM_DT for k in range(3)]
    return install(monkeypatch, tmp_path, SpecStubs(mjds, pm_scene, dets=(6,)))  # D1-D5: indexed, no coverage


def moved_fluxes(j: dict) -> list[float]:
    """Flux of the points of the passes where the source is far from the grid centre (dt != 0)."""
    return [p["flux"] for p in j["points"] if p["pass"] in (0, 2)]


async def test_pm_aperture_follows_the_source_with_correct_signs(pm_stubs, client):
    # why: the whole point of pmra/pmdec. At dt = -0.6 / +0.7 yr the source is 3-4 px from the centre; only an
    # aperture displaced in the RIGHT direction on BOTH axes recovers it. Flipping either sign loses it.
    good = (await client.get("/api/spectrum", params={**Q, "pmra": PMRA, "pmdec": PMDEC})).json()
    assert len(good["points"]) == 9
    assert all(f > 0.97 * FLUX for f in moved_fluxes(good)), moved_fluxes(good)
    assert {p["flags"] for p in good["points"]} == {FLAG_SOURCE}  # aperture-centre pixel follows too

    for label, pm in {
        "no pm": (0.0, 0.0),
        "pmra sign flipped": (-PMRA, PMDEC),
        "pmdec sign flipped": (PMRA, -PMDEC),
        "both flipped": (-PMRA, -PMDEC),
    }.items():
        bad = (await client.get("/api/spectrum", params={**Q, "pmra": pm[0], "pmdec": pm[1]})).json()
        assert max(moved_fluxes(bad)) < 0.5 * FLUX, (label, moved_fluxes(bad))


def test_aperture_position_conventions():
    # +x = west: a source with positive pmra (moving east) sits at SMALLER x later; positive pmdec -> larger y
    t_later = mjd_at(1.0)
    x, y = aperture_position(
        GRID, t_later, MAS_PER_PX, MAS_PER_PX
    )  # exactly 1 px/yr each, 1 yr after MID_EPOCH
    assert (x, y) == pytest.approx((C - 1.0, C + 1.0), abs=1e-3)
    assert aperture_position(GRID, mjd_at(0.0), 5e4, -5e4) == pytest.approx(
        (C, C), abs=1e-3
    )  # grid centre epoch
    assert aperture_position(GRID, t_later, 0.0, 0.0) == (C, C)


async def test_different_pm_is_a_different_cache_entry(pm_stubs):
    # why: the PM moves the aperture, so a PM-less cached spectrum must not be served for a PM request
    r0 = await spectrum_result(RA, DEC, SIZE, 0.0, 0.0)
    assert r0.cacheable and pm_stubs.fetch_calls == 1  # only D6 has frames
    r1 = await spectrum_result(RA, DEC, SIZE, PMRA, PMDEC)
    assert r1.body != r0.body and pm_stubs.fetch_calls == 2
    again = await spectrum_result(RA, DEC, SIZE, PMRA, PMDEC)
    assert again.source == "cache" and again.body == r1.body and pm_stubs.fetch_calls == 2
    key = cache.spectrum_key(GRID, PMRA, PMDEC, index.data_version())
    assert key.startswith("spectrum/") and cache.json_get(key) == r1.body


# ------------------------------------------------------------------ missing detectors, failures
async def test_missing_d4_index_lists_d1_and_d4_and_is_not_an_error(monkeypatch, tmp_path, client):
    # why: D1/D4 share the D4 index file, which is built on the EC2 box; locally it does not exist. The
    # spectrum from the other detectors must still be served (200), with the gap named, never a 500.
    install(monkeypatch, tmp_path, SpecStubs(PASS_MJDS, flat_scene, missing=(1, 4)))
    r = await client.get("/api/spectrum", params=Q)
    assert r.status_code == 200
    j = r.json()
    assert j["missing_dets"] == [1, 4] and j["dets"] == [2, 3, 5, 6]
    assert j["n_frames"] == 4 * len(PASS_MJDS)
    assert "error" not in j
    # a partial result must not be cached: the index may exist next time
    assert "max-age" not in r.headers["cache-control"] and r.headers["cache-control"] == "no-store"
    assert cache.json_get(cache.spectrum_key(GRID, 0.0, 0.0, index.data_version())) is None


async def test_all_indexes_missing_is_503_json(monkeypatch, tmp_path, client):
    install(monkeypatch, tmp_path, SpecStubs(PASS_MJDS, flat_scene, missing=config.DETECTORS))
    r = await client.get("/api/spectrum", params=Q)
    assert r.status_code == 503 and "error" in r.json()


async def test_complete_result_cached_and_served_without_fetching(monkeypatch, tmp_path, client):
    s = install(monkeypatch, tmp_path, SpecStubs(PASS_MJDS, flat_scene))
    r1 = await client.get("/api/spectrum", params=Q)
    assert r1.json()["dets"] == [1, 2, 3, 4, 5, 6] and r1.json()["missing_dets"] == []
    assert "max-age" in r1.headers["cache-control"] and s.fetch_calls == 6
    r2 = await client.get("/api/spectrum", params=Q)
    assert s.fetch_calls == 6 and r2.content == r1.content
    assert cache.json_get(cache.spectrum_key(GRID, 0.0, 0.0, index.data_version())) == r1.content


async def test_nonfinite_photometry_is_skipped_and_counted(monkeypatch, tmp_path, client):
    # why: a masked (NaN) aperture has no flux; a silent 0 or a partial sum would be a fake spectral point
    def scene(ref, k):
        img = gauss(C, C)
        if k == 0:
            img[int(C), int(C)] = np.nan
        return img, 3.0 + 0.1 * k, None

    install(monkeypatch, tmp_path, SpecStubs(PASS_MJDS, scene, dets=(6,), missing=(1, 2, 3, 4, 5)))
    j = (await client.get("/api/spectrum", params=Q)).json()
    assert j["n_failed"] == 1 and j["n_frames"] == len(PASS_MJDS) - 1
    assert all(np.isfinite(p["flux"]) and np.isfinite(p["err"]) for p in j["points"])


@pytest.mark.parametrize(
    "bad", [{"size": 0.33}, {"dec": 95}, {"ra": "abc"}, {"pmra": "nan"}, {"pmdec": "inf"}]
)
async def test_invalid_params_422(client, bad):
    r = await client.get("/api/spectrum", params={**Q, **bad})
    assert r.status_code == 422 and isinstance(r.json()["error"], str)
