# Task 03 — Fetch + processing core

**Model:** lower model OK. **Time:** ~1.5 days. **Depends on:** Tasks 00, 01 (FETCH_METHOD, flag bits, SUBTRACT_ZODI, HDU names, wavelength code), and 02 (ImageRef).

## Goal
Given `list[ImageRef]` + `Grid`, asynchronously produce aligned, masked, background-subtracted `Frame`s, quickly and with failures reported. All processing functions are pure and unit-tested on synthetic data.

## Files
- `backend/fetch.py`: async download/range-read + L1 cache
- `backend/process.py`: pure numpy/astropy functions
- `backend/encode.py`: Frame → NDJSON line (contract C5)
- `tests/test_process.py`, `tests/test_fetch.py`, `tests/test_encode.py`

---

## Part A — `backend/process.py` (pure, no I/O)

```python
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from astropy.stats import sigma_clipped_stats
from astropy.visualization import ZScaleInterval
from reproject import reproject_interp
from backend import config
from backend.models import Frame, Grid, ImageRef

def wavelength_at(hdul: fits.HDUList, ra: float, dec: float) -> float:
    """µm at the pixel containing (ra, dec). Implementation: copy from scripts/spx_util.py (recon R1)."""

def bad_mask(flags: np.ndarray, bits: tuple[int, ...] = config.FLAG_BITS_BAD) -> np.ndarray:
    """True where any of `bits` is set."""
    m = np.zeros(flags.shape, bool)
    for b in bits:
        m |= (flags.astype(np.uint32) >> np.uint32(b)) & np.uint32(1) == 1
    return m

def process_cutout(fits_bytes: bytes, ref: ImageRef, grid: Grid) -> Frame:
    """The ONLY entry point used by fetch/API. Runs in a worker process.
    1. hdul = fits.open(io.BytesIO(fits_bytes)); read IMAGE, FLAGS, VARIANCE (if present), ZODI (if present).
    2. img = IMAGE.astype(float32); if config.SUBTRACT_ZODI and ZODI present: img -= ZODI
    3. img[bad_mask(FLAGS)] = NaN
    4. in_wcs = WCS(IMAGE.header)   # includes SIP; do NOT strip it for the spatial WCS
    5. data, foot = reproject_interp((img, in_wcs), grid.wcs(), shape_out=(grid.npix, grid.npix), order="bilinear")
       data[foot == 0] = NaN
    6. var: same reprojection on VARIANCE (bilinear) if present
    7. flags: reproject_interp((FLAGS.astype(float64), in_wcs), ..., order="nearest-neighbor") → nan_to_num → uint32
    8. background: _, med, _ = sigma_clipped_stats(data, sigma=3, maxiters=5); data -= med
    9. lambda_um = wavelength_at(hdul, grid.ra, grid.dec)
    10. return Frame(ref, data.astype(float32), var, flags, lambda_um)"""

def group_by_pass(frames: list[Frame], gap_days: float = config.PASS_GAP_DAYS) -> list[Frame]:
    """Sort by t_mjd. pass_idx starts at 0 and increments when t[i]-t[i-1] > gap_days. Sets frame.pass_idx in place and returns the sorted list."""

def coadd(frames: list[Frame]) -> tuple[np.ndarray, np.ndarray | None]:
    """Inverse-variance weighted mean, ignoring NaN.
    w = 1/var where var finite & > 0, else (if any var is None) equal weights.
    num = Σ w·x over finite x; den = Σ w over finite x; out = num/den, NaN where den == 0.
    Returns (data, var_out = 1/den)."""

def coadd_by_pass(frames: list[Frame]) -> list[Frame]:
    """group_by_pass, then one Frame per pass: coadd data/var, flags = bitwise OR of inputs,
    t_mjd = mean, lambda_um = mean, obs_id = f"pass{p}_D{det}". Build a synthetic ImageRef for it."""

def zscale_limits(stack: list[np.ndarray], max_samples: int = config.ZSCALE_MAX_SAMPLES,
                  seed: int = 0) -> tuple[float, float]:
    """Concatenate finite pixels of ALL frames, uniformly subsample to max_samples (fixed seed),
    then ZScaleInterval().get_limits(sample). Contract C9."""
```

### Required tests (`tests/test_process.py`), using `tests/fixtures.make_fake_cutout`
Encode each test's **why** in its docstring.

1. **Alignment.** Star S at (ra0+0.01, dec0+0.005), flux 1000. Make cutout A with rot 0° and cutout B with rot 37° and a centre offset of 0.004°. Process both onto the same grid. The brightest pixel location matches in A and B, and the flux-weighted centroids in a 5×5 box agree within 0.2 px. *Why: misalignment makes static stars appear to move, which is a false discovery.*
2. **Motion is preserved.** Star at P1 in A, and at P1 + 2 px east in B (same WCS). Processed centroids differ by 2 ± 0.2 px in x, with **x decreasing** (east = left = lower column, per contract C4, after the flip in encode). *Why: the tool's entire purpose.*
3. **Flag masking.** Set bit `FLAG_BITS_BAD[0]` on a 3×3 block in FLAGS → the corresponding output pixels are NaN. Skip if `FLAG_BITS_BAD` is empty, with an explicit `pytest.skip("flag bits not configured")`.
4. **Zodi subtraction.** zodi=5, background=10 → after processing, the sigma-clipped median of data ≈ 0 ± 0.1.
5. **Pass grouping.** t = [0, 1, 2, 200, 201, 400] → pass_idx = [0, 0, 0, 1, 1, 2].
6. **Co-add SNR.** 16 frames, noise σ=1, equal var → std of the coadd ≈ 0.25 ± 0.03. *Why: the co-add mode is pointless if it doesn't raise SNR.*
7. **Co-add weighting.** Frame A value 0 with var 1, frame B value 10 with var 100 → coadd ≈ 0.099. *Why: noisy frames must not dominate.*
8. **NaN handling in co-add.** A pixel that is NaN in all inputs → NaN out. NaN in some → the mean of the rest.
9. **zscale is stack-wide.** Two frames with different noise levels get ONE (lo, hi) pair, and identical inputs produce identical limits (deterministic seed). *Why: per-frame limits cause flicker.*

---

## Part B — `backend/encode.py` (contract C5)

```python
import base64, orjson, numpy as np
from astropy.time import Time

def b64_f16(a: np.ndarray) -> str:
    """flipud (row0 = north, contract C4) → float16 little-endian → base64."""
    return base64.b64encode(np.flipud(a).astype("<f2").tobytes()).decode("ascii")

def b64_u32(a: np.ndarray) -> str:
    return base64.b64encode(np.flipud(a).astype("<u4").tobytes()).decode("ascii")

def header_line(grid, det, mode, n_expected, data_version, source, zscale) -> bytes: ...
def frame_line(i: int, f: Frame, include_flags: bool) -> bytes: ...   # date = Time(t_mjd, format="mjd").isot[:19]+"Z"
def zscale_line(lo: float, hi: float) -> bytes: ...
def error_line(obs_id: str, reason: str) -> bytes: ...
def done_line(n_frames: int, n_failed: int, elapsed_ms: int) -> bytes: ...
# every function returns orjson.dumps(obj) + b"\n"
```
Float16 caveat: values beyond ±65504 become inf. Before encoding, clip data to ±60000 and log a warning if clipping happened. (Units are probably MJy/sr with values far below that; recon confirms BUNIT.)

Tests (`tests/test_encode.py`):
- Round trip: an encoded 5×5 array, decoded back (np.frombuffer + flipud), equals the original within float16 precision. NaN is preserved.
- **Orientation:** a pixel at FITS row 0 (south) appears in the LAST row of the decoded wire array. *Why: contract C4. Getting this wrong flips the sky.*
- Every line ends with `\n` and parses as JSON; `type` is set.

---

## Part C — `backend/fetch.py`

```python
import asyncio, gzip, logging, os, random
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
import httpx
from backend import config, process
from backend.models import Frame, Grid, ImageRef

log = logging.getLogger("fetch")
_GLOBAL_SEM = asyncio.Semaphore(config.FETCH_CONCURRENCY)   # shared across all requests
_POOL = ProcessPoolExecutor(max_workers=config.PROCESS_WORKERS)

def cutout_url(ref: ImageRef, grid: Grid) -> str:
    return f"{config.IRSA_BASE}{ref.uri}?center={grid.ra:.6f},{grid.dec:.6f}d&size={grid.size_deg}"

def l1_path(ref: ImageRef, grid: Grid) -> Path:  # contract C7

async def _download(client: httpx.AsyncClient, url: str) -> bytes:
    """GET with FETCH_RETRIES retries on httpx.TransportError, timeouts, and 5xx.
    Backoff: 0.5 * 2**attempt + random.uniform(0, 0.3). 4xx (except 429) → raise immediately.
    Gzip magic b'\\x1f\\x8b' → gzip.decompress. Validate: starts with b'SIMPLE' else raise ValueError."""

async def _read_s3_section(ref: ImageRef, grid: Grid) -> bytes:
    """Only if config.FETCH_METHOD == 's3'. Open via fsspec (anon), compute the pixel box covering the grid
    (+3 px pad) from the full IMAGE header WCS, read .section for IMAGE/FLAGS/VARIANCE/ZODI,
    and build a small in-memory HDUList with a WCS header adjusted for the box (CRPIX -= offsets).
    Return its bytes. Run the blocking fsspec calls with asyncio.to_thread."""

async def get_frames(refs: list[ImageRef], grid: Grid) -> AsyncIterator[tuple[int, Frame | None, str | None]]:
    """Yields (i, frame, error) as each finishes, i = index into refs (refs are already sorted by t_mjd).
    For each ref:
      1. L1 hit → read bytes from disk.
      2. Miss → async with _GLOBAL_SEM: download (or s3 section) → atomic write to L1.
      3. frame = await loop.run_in_executor(_POOL, process.process_cutout, data, ref, grid)
    Any exception → yield (i, None, f"{type(e).__name__}: {e}") and log with obs_id + url. Never raise out of the iterator.
    Use asyncio.as_completed over one task per ref. Client: httpx.AsyncClient(http2=True,
    timeout=config.FETCH_TIMEOUT_S, limits=httpx.Limits(max_connections=config.FETCH_CONCURRENCY))."""
```

Tests (`tests/test_fetch.py`):
- Use `httpx.MockTransport` (inject the client via a module-level factory you can monkeypatch). Serve the bytes of `make_fake_cutout(...).writeto(BytesIO)`, gzipped on even requests.
- All refs are yielded exactly once, with a correct i.
- **Retry:** the mock returns 503 twice, then 200 → the frame succeeds, and the mock saw 3 calls.
- **Permanent failure:** 404 → yields an error string, the other frames still arrive. *Why: one bad file must not kill a sequence.*
- **L1 cache:** a second call with the same refs makes zero HTTP calls.
- `@pytest.mark.network`: 10 real WISE 0855 D6 refs (from `tests/data/tap_wise0855_d6.csv`) → 10 frames or reported errors; the first frame arrives in < 3 s.
- Monkeypatch `process.wavelength_at` to return 4.5 for the fake cutouts (they have no spectral WCS).

## Done when
1. `pytest -q`: all pass. Any skips are listed with reasons.
2. `pytest -m network tests/test_fetch.py` passes. Paste the time-to-first-frame.
3. Script check:
   ```bash
   python -c "import asyncio; from scripts.targets import TARGETS; ..."
   ```
   (Write `scripts/try_fetch.py`: find_images + get_frames for WISE 0855 D6 at 0.1°, printing per-frame ms and total.) Paste the output.

## Do NOT
- Process in the event loop thread (CPU work goes to `_POOL`).
- Create a semaphore per request (it must be global).
- Strip SIP from the spatial WCS.
- Use per-frame zscale anywhere.
