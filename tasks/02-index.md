# Task 02 — Local metadata index

**Model:** lower model OK. **Time:** ~1 day (the full build may run overnight). **Depends on:** Tasks 00 and 01 (R6 numbers, polygon format, filename regex, dedupe rule).

## Goal
`find_images(ra, dec, det)` returns the same images as a live TAP query, in < 50 ms p95.

## Files
- `backend/index.py`: lookup + live-TAP fallback
- `backend/ids.py`: filename parsing + dedupe
- `scripts/build_index.py`: full build
- `scripts/sync_index.py`: incremental update
- `tests/test_ids.py`, `tests/test_index.py`

## Steps

### I1. `backend/ids.py`
```python
import re
from backend.models import ImageRef

FILENAME_RE = re.compile(r"...")   # copy EXACTLY from notes/recon.md (R6)

def parse_uri(uri: str, t_mjd: float, release: str) -> ImageRef:
    """Build an ImageRef from an artifact uri. Raise ValueError (with the uri) if it doesn't match."""

def dedupe(refs: list[ImageRef]) -> list[ImageRef]:
    """Contract C2 rule: per obs_id keep DR1 over QR*, then the max version. Return sorted by t_mjd."""
```
If recon found TAP columns that give exposure_id/version/release directly, use them instead of the regex. recon.md says which.

Tests (`tests/test_ids.py`):
- Parse the example filename from contract C2 → expected fields.
- Parse every uri in `tests/data/tap_wise0855_d6.csv`: no exception. **Why:** one unparseable naming variant would silently drop frames.
- dedupe: DR1 and QR3 copies of the same obs_id → one result with release DR1. Two QR3 versions → the larger one.
- dedupe output is sorted by t_mjd.

### I2. Geometry helpers in `backend/index.py`
```python
import numpy as np

def point_in_polygon(ra: float, dec: float, poly: np.ndarray) -> bool:
    """poly: (N,2) deg vertices. Gnomonic-project the vertices about (ra,dec), then even-odd ray casting.
    Valid for polygons < ~20° across (SPHEREx footprints are ~3.5°)."""
    ra0, dec0 = np.deg2rad(ra), np.deg2rad(dec)
    r, d = np.deg2rad(poly[:, 0]), np.deg2rad(poly[:, 1])
    cosc = np.sin(dec0)*np.sin(d) + np.cos(dec0)*np.cos(d)*np.cos(r - ra0)
    if np.any(cosc <= 0):           # vertex > 90° away → definitely not containing
        return False
    x = np.cos(d)*np.sin(r - ra0) / cosc
    y = (np.cos(dec0)*np.sin(d) - np.sin(dec0)*np.cos(d)*np.cos(r - ra0)) / cosc
    # point is at origin (0,0)
    inside = False
    n = len(x)
    for i in range(n):
        x1, y1, x2, y2 = x[i], y[i], x[(i+1) % n], y[(i+1) % n]
        if (y1 > 0) != (y2 > 0):
            xi = x1 + (0 - y1) * (x2 - x1) / (y2 - y1)
            if xi > 0:
                inside = not inside
    return inside
```
Tests:
- A square of side 1° around (10, 20) contains (10, 20) and not (12, 20).
- **RA wrap:** a square around RA 0° (vertices at 359.5 and 0.5) contains (0, 0) and (359.8, 0). **Why:** naive planar tests fail at RA = 0/360.
- Near the pole: a polygon around dec 89° contains (any ra, 89.5).

### I3. Schema + build (`scripts/build_index.py`)
1. Create the tables exactly as in contract C8. Use `PRAGMA journal_mode=WAL; PRAGMA synchronous=OFF` during the build.
2. Fetch metadata in chunks **by time range** (e.g. 7 days per chunk) using an **async TAP job**, since sync queries time out on large results:
   ```python
   svc = pyvo.dal.TAPService(config.IRSA_TAP_URL)
   job = svc.submit_job(adql); job.run(); job.wait(phases=["COMPLETED","ERROR","ABORTED"], timeout=1800)
   job.raise_if_error(); tbl = job.fetch_result().to_table()
   ```
   ADQL per chunk:
   ```sql
   SELECT a.uri, p.time_bounds_lower, p.energy_bandpassname, p.poly /*, release/version cols if they exist */
   FROM spherex.artifact a JOIN spherex.plane p ON a.planeid = p.planeid
   WHERE p.time_bounds_lower >= {t0} AND p.time_bounds_lower < {t1}
     AND p.energy_bandpassname LIKE 'SPHEREx-D%'
   ```
   [Recon decides] whether a filter on `a.uri` is needed to keep only the science image artifact (and not e.g. calibration files).
3. Per row:
   - Parse with `ids.parse_uri`.
   - Parse the polygon into an (N,2) array (format from recon).
   - Insert into `images` with `INSERT OR REPLACE`, keyed by obs_id. Then apply the dedupe rule: if the existing row is DR1 and the new one is QR, keep the existing one.
4. Cells per image:
   ```python
   from astropy_healpix import HEALPix
   import astropy.units as u
   hp = HEALPix(nside=config.HEALPIX_NSIDE, order="nested")
   cra, cdec = centroid(poly)                       # mean of unit vectors → back to ra/dec
   rad = max_angular_distance(cra, cdec, poly) + hp.pixel_resolution.to_value(u.deg)
   cells = hp.cone_search_lonlat(cra * u.deg, cdec * u.deg, radius=rad * u.deg)
   ```
   Insert `(cell, image_id)` rows with `executemany`, committing every chunk.
   **Size check after the first chunk:** if the average cells per image is > 30, STOP and report. The nside or radius logic is wrong; ~10 is expected.
5. **Resumable:** store `meta.last_chunk_t1` after each chunk commit. On restart, continue from there.
6. At the end:
   - Set `meta.row_count`, `meta.max_t_mjd`, `meta.last_sync_utc`, and `meta.data_version = last_sync_utc`.
   - Run `ANALYZE`, then switch back to `PRAGMA synchronous=NORMAL`.
7. Print progress per chunk (rows, cumulative rows, elapsed, ETA).

### I4. Lookup (`backend/index.py`)
```python
class Index:
    def __init__(self, path=config.INDEX_DB): ...   # sqlite3 connect, check_same_thread=False, read-only URI mode
    def data_version(self) -> str: ...
    def find_images(self, ra: float, dec: float, det: int | None = None) -> list[ImageRef]:
        """1. cell = hp.lonlat_to_healpix(ra, dec)
           2. SELECT images.* FROM cells JOIN images ON images.id = cells.image_id
              WHERE cells.cell = ? [AND images.detector = ?]
           3. keep rows where point_in_polygon(ra, dec, poly)
           4. return ids.dedupe(...)"""
    def is_stale(self) -> bool: ...  # last_sync_utc older than INDEX_MAX_AGE_DAYS

async def find_images_live(ra, dec, det) -> list[ImageRef]:
    """Fallback: live TAP (same query as recon R2), run in a thread. Used when the index is missing, stale, or returns 0 rows."""
```
Parse each polygon JSON once per row; only ~1–2k rows per cell, so that's fine. If profiling shows a bottleneck, cache parsed polygons in an LRU keyed by image id.

### I5. `scripts/sync_index.py`
- Reads `meta.max_t_mjd` and runs the build chunks from `max_t_mjd - 3` days to now. The overlap catches late ingestion; re-inserts are idempotent through INSERT OR REPLACE + dedupe.
- Also re-queries the last 30 days with release filters, to pick up DR re-processing [recon decides whether needed].
- Updates `meta.data_version` **only if rows changed**.
- Prints the number of new/updated rows and the list of affected cells to `data/index/changed_cells.json` (used by Task 04b).

### I6. Tests (`tests/test_index.py`)
- Build a tiny index from `tests/data/tap_wise0855_d6.csv` using the same insert code path (factor `insert_rows(conn, rows)` out of `build_index.py` so it can be tested).
- `find_images(wise0855)` returns exactly the obs_ids in the CSV (after dedupe). **Why:** the index must not drop or invent frames.
- `find_images` of a point 10° away → empty.
- `@pytest.mark.network` test: for each of the 5 targets, index result obs_ids == live TAP obs_ids. Run this after the full build.
- Timing test: 100 random lookups on the full index, p95 < 50 ms. Mark it `@pytest.mark.network` (needs the full DB).

## Done when
1. `pytest -q` passes.
2. After the full build: `pytest -m network tests/test_index.py` passes. Paste the p95.
3. `python -c "from backend.index import Index; i=Index(); print(len(i.find_images(133.795,-7.245,6)))"` prints the same count as recon R5 for WISE 0855 D6.

## Do NOT
- Use a HEALPix nside other than `config.HEALPIX_NSIDE`.
- Store polygons in any format other than JSON `[[ra,dec],...]`.
- Silently skip rows that fail to parse. Count them, log the first 10, and fail the build if > 0.1%.
