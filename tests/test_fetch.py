import asyncio
import concurrent.futures
import contextlib
import io

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from backend import config, fetch, process
from backend.models import ImageRef, make_grid
from tests.fixtures import make_fake_cutout

RA0, DEC0 = 133.76, -7.24
STAR = (RA0 + 0.01, DEC0 + 0.005, 1000.0)


def _sip_cutout(rot=20.0, seed=0, star=STAR):
    """Full frame with SIP distortion (like the real data) and a star."""
    h = make_fake_cutout(RA0, DEC0, npix=200, rot_deg=rot, stars=[star], noise=0.01, seed=seed)
    for hdu in h[1:]:
        hdr = hdu.header
        hdr["CTYPE1"], hdr["CTYPE2"] = "RA---TAN-SIP", "DEC--TAN-SIP"
        hdr["A_ORDER"] = hdr["B_ORDER"] = 2
        hdr["A_2_0"], hdr["A_0_2"], hdr["A_1_1"] = 2e-5, -1e-5, 3e-5
        hdr["B_2_0"], hdr["B_0_2"], hdr["B_1_1"] = -1e-5, 2e-5, 1e-5
    return h


def _bytes(hdul):
    b = io.BytesIO()
    hdul.writeto(b)
    return b.getvalue()


def _crop(hdul, grid):
    """crop_hdul needs a file-backed HDUList (.section), like S3 gives."""
    with fits.open(io.BytesIO(_bytes(hdul))) as h:
        return fetch.crop_hdul(h, grid)


def _ref(i, key=None):
    return ImageRef(f"e{i}_D6", f"e{i}", 6, 60000.0 + i, "dr1", "l2b-v1-2026-001", key or f"k/{i}.fits")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(process, "wavelength_at", lambda h, ra, dec: 4.5)
    monkeypatch.setattr(config, "RAW_CACHE_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "FETCH_RETRIES", 3)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    yield
    if fetch._pool is not None:
        fetch._pool.shutdown()
        fetch._pool = None


@pytest.fixture
def s3(monkeypatch):
    store: dict[str, bytes] = {}
    calls: list[str] = []
    fail_first: dict[str, int] = {}

    @contextlib.contextmanager
    def fake_open(key, grid=None):
        calls.append(key)
        if fail_first.get(key, 0) > 0:
            fail_first[key] -= 1
            raise OSError("transient")
        if key not in store:
            raise FileNotFoundError(key)
        with fits.open(io.BytesIO(store[key])) as hdul:
            yield hdul

    monkeypatch.setattr(fetch, "_open_s3", fake_open)
    # Production reads run in a process pool, which would not see this monkeypatch (and would hit
    # real S3). Same executor interface, in-process, so the fake store is used.
    reads = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    monkeypatch.setattr(fetch, "_get_read_pool", lambda: reads)
    return store, calls, fail_first


def test_cropped_header_world_to_pixel_matches_full():
    """CRPIX shift must keep SIP valid: sky->pixel must differ by exactly the box offset."""
    grid = make_grid(RA0, DEC0, 0.1)
    full = _sip_cutout()
    crop = fits.open(io.BytesIO(_crop(full, grid)))
    w_full, w_crop = WCS(full["IMAGE"].header), WCS(crop["IMAGE"].header)
    x0, _, y0, _ = fetch.grid_box(w_full, grid, 200, 200)
    ra = RA0 + np.array([-0.04, 0.0, 0.03, 0.045])
    dec = DEC0 + np.array([-0.04, 0.0, 0.03, -0.045])
    xf, yf = w_full.world_to_pixel_values(ra, dec)
    xc, yc = w_crop.world_to_pixel_values(ra, dec)
    np.testing.assert_allclose(xf - x0, xc, atol=1e-6)
    np.testing.assert_allclose(yf - y0, yc, atol=1e-6)
    assert crop["IMAGE"].data.shape[0] < 200  # actually cropped
    assert "EPSF" not in crop


def test_cropped_processing_equals_full_processing():
    """Reading only a box must not change the science output (alignment, background, flux)."""
    grid = make_grid(RA0, DEC0, 0.1)
    full = _sip_cutout()
    ref = _ref(0)
    a = process.process_cutout(_bytes(full), ref, grid)
    b = process.process_cutout(_crop(full, grid), ref, grid)
    both = np.isfinite(a.data) & np.isfinite(b.data)
    assert both.mean() > 0.95
    np.testing.assert_allclose(a.data[both], b.data[both], atol=0.05)
    assert a.lambda_um == b.lambda_um == 4.5


def test_lambda_um_taken_from_full_frame_header(monkeypatch):
    """The W WCS only works on full-frame pixels: process must trust LAMBDA_UM, not recompute."""
    monkeypatch.setattr(process, "wavelength_at", lambda h, ra, dec: 3.21)
    grid = make_grid(RA0, DEC0, 0.1)
    data = _crop(_sip_cutout(), grid)
    assert fits.getheader(io.BytesIO(data), "IMAGE")["LAMBDA_UM"] == 3.21

    def boom(*a):
        raise AssertionError("wavelength_at must not run on a cropped cutout")

    monkeypatch.setattr(process, "wavelength_at", boom)
    assert process.process_cutout(data, _ref(0), grid).lambda_um == pytest.approx(3.21)


def test_grid_outside_image_raises():
    """A footprint miss must be a reported failure, not an empty frame."""
    grid = make_grid(RA0 + 5.0, DEC0, 0.1)
    with pytest.raises(ValueError, match="outside"):
        _crop(_sip_cutout(), grid)


def _run(refs, grid):
    async def go():
        return [r async for r in fetch.get_frames(refs, grid)]

    return asyncio.run(go())


def test_all_refs_once_failure_isolated_and_l1_cache(s3):
    """One missing D3 file must not kill the sequence; second call must not touch S3 (L1)."""
    store, calls, _ = s3
    grid = make_grid(RA0, DEC0, 0.1)
    refs = [_ref(i) for i in range(4)]
    for i in (0, 1, 3):
        store[refs[i].uri] = _bytes(_sip_cutout(seed=i))
    out = _run(refs, grid)
    assert sorted(i for i, _, _ in out) == [0, 1, 2, 3]
    by_i = {i: (f, e) for i, f, e in out}
    assert by_i[2][0] is None and "FileNotFoundError" in by_i[2][1]
    for i in (0, 1, 3):
        assert by_i[i][1] is None and by_i[i][0].data.shape == (grid.npix, grid.npix)
        assert by_i[i][0].lambda_um == 4.5
    assert len(calls) == 4  # missing key not retried
    calls.clear()
    out2 = _run([refs[0], refs[1], refs[3]], grid)
    assert calls == [] and all(e is None for _, _, e in out2)
    assert not list(config.RAW_CACHE_DIR.glob("*.tmp"))


def test_transient_error_retried(s3):
    """S3 hiccups happen; the frame must still arrive after retries."""
    store, calls, fail_first = s3
    grid = make_grid(RA0, DEC0, 0.1)
    ref = _ref(0)
    store[ref.uri] = _bytes(_sip_cutout())
    fail_first[ref.uri] = 2
    ((_i, f, e),) = _run([ref], grid)
    assert e is None and f is not None and len(calls) == 3


def test_retries_exhausted_reports_error(s3):
    store, calls, fail_first = s3
    ref = _ref(0)
    store[ref.uri] = _bytes(_sip_cutout())
    fail_first[ref.uri] = 99
    ((_i, f, e),) = _run([ref], make_grid(RA0, DEC0, 0.1))
    assert f is None and "RuntimeError" in e and len(calls) == config.FETCH_RETRIES


@pytest.mark.network
def test_real_s3_frames_timing_and_bytes():
    """5 real frames from the S3 mirror: report seconds and bytes per frame."""
    import time

    from backend import index

    ra, dec = 133.759993, -7.242201  # WISE 0855 at 2025.9
    refs = asyncio.run(index.find_images(ra, dec, 6))[:5]
    if not refs:
        pytest.skip("no WISE 0855 coverage in the local index subset")
    grid = make_grid(ra, dec, 0.1)
    t0 = time.monotonic()
    out = _run(refs, grid)
    dt = time.monotonic() - t0
    nbytes = sum(p.stat().st_size for p in config.RAW_CACHE_DIR.glob("*.fits"))
    print(f"{len(out)} frames in {dt:.1f}s ({dt / len(out):.2f}s/frame), L1 bytes {nbytes}")
    assert all(f is not None or e for _, f, e in out)
