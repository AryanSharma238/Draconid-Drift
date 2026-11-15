# Contracts — single source of truth

Every module and task MUST follow this file. If code and this file disagree, this file wins. Change this file only through a deliberate decision recorded in "Change log" at the bottom. Never change it silently inside a coding task.

Values marked **[VERIFY]** are placeholders until Phase 1 (`tasks/01-recon.md`) fills them in. A coding task that depends on an unresolved [VERIFY] must STOP and report. It must not guess.

---

## C1. Configuration — `backend/config.py`

All tunables live here and nowhere else. No magic numbers in other modules.

```python
from pathlib import Path

# --- paths ---
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
INDEX_DB = DATA_DIR / "index" / "spherex.sqlite"
RAW_CACHE_DIR = DATA_DIR / "cache" / "raw"        # L1
STACK_CACHE_DIR = DATA_DIR / "cache" / "stacks"   # L2

# --- IRSA ---
IRSA_BASE = "https://irsa.ipac.caltech.edu/"
IRSA_TAP_URL = "https://irsa.ipac.caltech.edu/TAP"
SPHEREX_S3_BUCKET = "nasa-irsa-spherex"   # CONFIRMED 2026-11-14
SPHEREX_S3_REGION = "us-east-1"     # CONFIRMED 2026-11-14 (x-amz-bucket-region)
FETCH_METHOD = "cutout"             # "cutout" | "s3"  — decided in recon step R9

# --- instrument ---
PIXSCALE_ARCSEC = 6.15              # CONFIRMED 2026-11-14 (Expl. Supplement; header 6.157)
DETECTORS = (1, 2, 3, 4, 5, 6)
BANDPASS_NAME = "SPHEREx-D{det}"    # value of p.energy_bandpassname

# --- flags (bit numbers in FLAGS HDU) ---
FLAG_BITS_BAD = (0, 2, 6, 7, 9, 10, 11, 14, 17, 19, 25, 27)   # CONFIRMED 2026-11-14; rationale in config.py
FLAG_BIT_NAMES = {0: "TRANSIENT", ...}   # CONFIRMED 2026-11-14: Expl. Supplement Table 16; full map in config.py

# --- processing ---
SUBTRACT_ZODI = True                # CONFIRMED 2026-11-14: IMAGE is NOT zodi-subtracted
PASS_GAP_DAYS = 30.0                # gap that starts a new survey pass
ASINH_A = 0.1                       # default asinh softening
ZSCALE_MAX_SAMPLES = 100_000

# --- request limits ---
FOV_CHOICES_DEG = (0.05, 0.1, 0.25, 0.5)
DEFAULT_FOV_DEG = 0.1
MAX_FOV_DEG = 0.5
COORD_SNAP_DEG = 0.001              # snap request centre for cache sharing (3.6″ < 1 px)

# --- fetching ---
FETCH_CONCURRENCY = 16              # global, across all requests
FETCH_RETRIES = 3
FETCH_TIMEOUT_S = 30.0
PROCESS_WORKERS = 4

# --- index ---
HEALPIX_NSIDE = 32                  # order 5, ~1.8° cells. DO NOT use finer: footprints are ~3.5°
INDEX_MAX_AGE_DAYS = 10             # older → also query live TAP
```

## C2. Python data models — `backend/models.py`

```python
from dataclasses import dataclass
import numpy as np

@dataclass(frozen=True)
class ImageRef:
    obs_id: str        # f"{exposure_id}_D{detector}", unique per exposure+detector
    exposure_id: str   # parsed from filename, e.g. "2025W19_2B_0346_1"  (regex confirmed, see below)
    detector: int      # 1..6
    t_mjd: float       # p.time_bounds_lower, MJD  [VERIFY units]
    release: str       # "DR1" | "QR3" | ...
    version: str       # processing version parsed from filename, e.g. "l2b-v20-2025-247"
    uri: str           # S3 key in SPHEREX_S3_BUCKET, e.g. "dr1/level2/2025W17_4B/l2b-v27-2026-196/6/level2_..._D6_....fits" (2026-11-14; was: path relative to IRSA_BASE)

@dataclass(frozen=True)
class Grid:
    ra: float          # snapped centre, deg
    dec: float         # snapped centre, deg
    size_deg: float
    npix: int          # = ceil(size_deg*3600 / PIXSCALE_ARCSEC), forced odd so centre is a pixel
    pixscale_arcsec: float
    # method: wcs() -> astropy.wcs.WCS  (TAN, CRVAL=(ra,dec), CRPIX=((npix+1)/2,)*2,
    #         CDELT=(-pixscale/3600, +pixscale/3600), north up, east left)

@dataclass
class Frame:
    ref: ImageRef
    data: np.ndarray       # float32 (npix, npix), FITS orientation (row 0 = south), NaN = masked/no data
    var: np.ndarray | None # float32 same shape, or None
    flags: np.ndarray      # uint32 same shape (nearest-neighbour reprojected)
    lambda_um: float       # wavelength at the grid centre
    pass_idx: int = -1     # assigned by group_by_pass, 0-based
```

### obs_id parsing and dedupe

- Example filename: `level2_2025W19_2B_0346_1D2_spx_l2b-v20-2025-247.fits` → exposure_id `2025W19_2B_0346_1`, detector `2`, version `l2b-v20-2025-247`.
- Regex (CONFIRMED 2026-11-14 on 56 real filenames, DR1/QR2/QR3, from the S3 mirror):
  `^level2_(?P<exposure_id>\d{4}W\d{2}_\d[A-Z]_\d{4}_\d)D(?P<det>[1-6])_spx_(?P<version>l2b(?:_retry)?-v\d+-\d{4}-\d{3})\.fits$`
  Note the `l2b_retry-` version variant (e.g. `l2b_retry-v27-2026-197`).
- Dedupe rule: if two refs share `obs_id`, keep the higher release priority **DR1 > QR3 > QR2**. Within the same release, keep the largest version comparing the parsed tuple `(vNN as int, yyyy, ddd)`; tiebreak retry over non-retry. **Never compare version strings lexicographically** (`_retry` sorts after `-` on punctuation alone).

## C3. Coordinate rules

- All internal coordinates: ICRS degrees, `float`.
- Snap: `ra_s = round(ra / COORD_SNAP_DEG) * COORD_SNAP_DEG % 360`, `dec_s = round(dec / COORD_SNAP_DEG) * COORD_SNAP_DEG`, clamped to [-90, 90].
- Parse input (function `parse_coords(q: str) -> tuple[float,float] | None`):
  - Two decimal numbers (`"133.795 -7.245"`, `"133.795,-7.245"`) → degrees.
  - Anything containing `h`, `m`, `s`, `d`, `:` or 6 numeric tokens → `SkyCoord(q, unit=(u.hourangle, u.deg))`.
  - Otherwise → `None`, and the caller treats it as an object name (Sesame).
  - Required test: `"08h55m10.831s -07d14m42.53s"` → `(133.79513, -7.24515)` ± 1e-5.

## C4. Image orientation on the wire

- The server sends arrays with **row 0 = NORTH (top), column 0 = EAST (left)**, i.e. `np.flipud(frame.data)`. The grid already has east left because CDELT1 < 0.
- The client draws row 0 at the top of the canvas, with no further flipping.
- The crosshair (input position) sits at pixel `((npix-1)/2, (npix-1)/2)`.

## C5. Wire format — sequence stream (NDJSON)

`Content-Type: application/x-ndjson`. One JSON object per line, UTF-8, `\n` terminated. Message order: `header`, then zero or more `frame`/`error`/`zscale` messages in any order, then `done` last.

```jsonc
{"type":"header","v":1,"ra":133.795,"dec":-7.245,"size_deg":0.1,"npix":59,
 "pixscale_arcsec":6.2,"det":6,"mode":"all","n_expected":112,
 "data_version":"2026-11-14T00:00:00Z","source":"live",       // source: "live" | "cache" | "precomputed"
 "zscale":[lo,hi]}                                             // null in live mode until known

{"type":"zscale","lo":-0.8,"hi":12.4}                          // live mode only: after the first <=10 frames, and again (final) before "done"

{"type":"frame","i":0,"obs_id":"2025W19_2B_0346_1_D6","t_mjd":60812.31,
 "date":"2025-05-10T07:26:24Z","det":6,"lambda_um":4.512,"pass":0,
 "release":"DR1","n_coadd":1,"kind":"single",                  // "single" | "pass" (Task 09 E2: in stored stacks all "pass" lines come first)
 "data":"<base64 float16 LE, npix*npix, row-major, row0=north>",
 "flags":"<base64 uint32 LE, same layout>"}                     // flags omitted when mode="pass"

{"type":"error","obs_id":"...","reason":"timeout after 3 retries"}

{"type":"done","n_frames":108,"n_failed":4,"elapsed_ms":8123}
```

Rules:
- `i` is the index after sorting by `t_mjd`. In live mode, frames can arrive out of order, so the client inserts them by `i`. The server must know the full sorted ref list before streaming, so it assigns `i` up front.
- `mode="pass"`: one frame per (pass, det), the inverse-variance co-add. `obs_id = f"pass{p}_D{det}"`, `n_coadd` = number of inputs, `t_mjd` = mean of the inputs, `lambda_um` = mean of the inputs.
- NaN is encoded as float16 NaN (`0x7E00`).
- `{"type":"status","msg":"..."}` lines may come **before** `header` (sprint S2). Clients show `msg` and ignore unknown types.
- `done` carries `"zscale":[lo,hi]` (or null); there are no live `zscale` lines in the sprint build.
- **Pass previews** (`mode="pass"`, live only): a pass co-add may be sent early with `"preview":true` once `PASS_PREVIEW_FRAMES` of its inputs have arrived, and is sent again with the same `i` (no `preview` key) when the pass is complete. The client **replaces** frame `i`. Stored stacks contain only final frames. Live fetch order interleaves passes (round-robin), so every pass gets a preview early.
- **Stored stacks (L2 and S3)** are the exact same NDJSON bytes, gzip-compressed, named `.ndjson.gz`. They are served with `Content-Encoding: gzip` and `Content-Type: application/x-ndjson`. One format for both live and stored.

## C6. HTTP API

| Method + path | Params | Response |
|---|---|---|
| `GET /api/sequence` | `ra`, `dec` (deg, required); `size` (deg, one of FOV_CHOICES, default 0.1); `det` (1–6, default 6); `mode` (`all`\|`pass`, default `all`) | NDJSON stream (C5). 422 on invalid params, with JSON `{"error": "..."}`. Zero coverage → `header` with `n_expected:0` then `done`. |
| `GET /api/resolve` | `q` (name or coordinates) | `{"name","ra","dec","epoch","pmra","pmdec","ra_now","dec_now","epoch_now"}` (Task 09 E1; pm in mas/yr, 0 if unknown), or 404 `{"error":"not found"}` |
| `GET /api/counts` | `ra`, `dec`, `size`, `det` | `{"det":6,"n_exposures":N,"n_passes":n}` from the footprint index only (no S3 reads). `n_exposures` = `n_expected` in mode all/lmdiff, `n_passes` = `n_expected` in mode pass. 503 if the detector's index is missing. |
| `GET /api/lightcurve` | `ra`, `dec`, `size`, `det` | Task 09 E4. `summary.photometry == "ring"` (and `"ring": true` on each point) when the aperture is unusable for most frames (saturated core): PSF-wing photometry, still λ-matched. |
| `GET /api/catalog` | `ra`, `dec`, `size`, `epochs` (comma list of MJD) | Phase 6, see `tasks/06-features.md` F2 |
| `GET /api/spectrum` | `ra`, `dec`, `size`; optional `pmra`, `pmdec` (mas/yr; the aperture follows the PM from `config.SPHEREX_MID_EPOCH`, the epoch of the grid centre) | JSON `{"points":[{"lambda_um","flux","err","t_mjd","det","pass","flags"}], "unit":"<BUNIT>", "n_frames":int, "n_failed":int, "dets":[...]}`, sorted by λ. `flags` = uint32 FLAGS value at the aperture-centre pixel. Detectors whose index is missing are listed in `"missing_dets"`, not an error. `photometry` is `"aperture"` (2 px aperture, annulus background) or `"ring"`: when fewer than `config.RING_AUTO_APERTURE_FRAC` of all frames have a finite aperture (saturated core delivered as NaN, e.g. R Dra, Barnard's), the WHOLE spectrum switches to PSF-wing photometry (sum over r = `config.RING_R_IN`..`RING_R_OUT` px; adds `"ring_radii_px"`, and `unit` says so). Ring flux is relative only: the ring's share of the total flux changes with wavelength. Only fetch failures count against the 20% cache limit; non-finite points do not. Cached in L2 as `spectrum/{key}.json`. See F3. |
| `GET /api/health` | — | `{"ok":true,"data_version":..,"index_rows":..}` |

Every response carries a `Server-Timing` header (e.g. `index;dur=12, fetch;dur=3400, process;dur=800`) and `Access-Control-Allow-Origin: *`.

## C7. Cache keys and storage layout

```
stack_key = f"{data_version}/{ra_s:.3f}_{dec_s:+.3f}_{size_deg}_{det}_{mode}"
L1 raw:   RAW_CACHE_DIR / f"{obs_id}_{ra_s:.3f}_{dec_s:+.3f}_{size_deg}.fits"     (decompressed FITS)
L2:       STACK_CACHE_DIR / f"{stack_key}.ndjson.gz"
S3:       s3://<STACKS_BUCKET>/stacks/{stack_key}.ndjson.gz
```
- Writes are atomic: write to `*.tmp`, then `os.replace`.
- L2/S3 lookup order in the API: S3 precomputed → L2 local → live.

## C8. Index database — SQLite `INDEX_DB`

```sql
CREATE TABLE images (
  id          INTEGER PRIMARY KEY,
  obs_id      TEXT UNIQUE NOT NULL,
  exposure_id TEXT NOT NULL,
  detector    INTEGER NOT NULL,
  t_mjd       REAL NOT NULL,
  release     TEXT NOT NULL,
  version     TEXT NOT NULL,
  uri         TEXT NOT NULL,
  poly        TEXT NOT NULL            -- JSON [[ra,dec],...] vertices, deg, ICRS
);
CREATE TABLE cells (cell INTEGER NOT NULL, image_id INTEGER NOT NULL);
CREATE INDEX cells_cell ON cells(cell);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);  -- data_version, last_sync_utc, max_t_mjd, row_count
```
- Cells: HEALPix `nside=HEALPIX_NSIDE`, `order="nested"`, ICRS. Each image is registered in every cell overlapped by the cone around its footprint centroid, with radius = max vertex distance + cell size (a superset is fine because lookup does an exact test).
- Lookup: cell of the point → candidate ids → exact point-in-polygon → dedupe (C2) → sort by `t_mjd`.

## C9. Stretch (client and server MUST match)

- Default limits: `lo, hi = ZScaleInterval().get_limits(sample)`. The sample is up to ZSCALE_MAX_SAMPLES finite pixels drawn uniformly from the **whole stack** (not per frame). The server sends them as `header.zscale = [lo, hi]`.
- Display value: `v = clamp((x - lo) / (hi - lo), 0, 1)`; `y = asinh(v / a) / asinh(1 / a)`; byte = `round(255 * y)`.
- NaN pixels → neutral grey with a diagonal hatch ("no data", not dark sky). Client-only; changed 2026-11-14 from RGB (40, 0, 0).
- Difference images: symmetric limits `±k·σ`, where σ = 1.4826·MAD of the finite pixels and k = 5 by default. Diverging colormap: blue (neg) → black (0) → orange (pos).
- Live streams: see the `zscale` message in C5. The client always uses the most recent `zscale` it has received, unless the user set `minbright`/`maxbright`.
- Stored stacks: `header.zscale` is the final value, and no `zscale` messages are present.

## C10. Frontend URL parameters

| Param | Type | Default | Notes |
|---|---|---|---|
| `ra`, `dec` | deg or sexagesimal (URL-encoded) | — | Required unless `q` is given |
| `q` | object name | — | Resolved via `/api/resolve` |
| `size` | deg | 0.1 | One of FOV_CHOICES |
| `det` | 1–6 | 6 | |
| `mode` | `all`\|`pass`\|`lmdiff` | `pass` | |
| `speed` | ms per frame | 500 | |
| `minbright`, `maxbright` | float | zscale | Override stretch limits |
| `zoom` | int CSS scale | auto-fit | |
| `diff` | `none`\|`median`\|`first` | `none` | |
| `gaia`, `catwise` | 0/1 | 0 | Phase 6 |
| `lam_min`, `lam_max` | µm | — | F4 band filter, client-side over `frame.lambda_um` (mode `all`; per-pass co-adds of the selected frames are computed in the client) |
| `preset` | `ch4`\|`h2o`\|`co2`\|`co` | — | Phase 6 |
| `embed` | 0/1 | 0 | Minimal chrome |
| `pmra`, `pmdec`, `epoch` | mas/yr, mas/yr, jyear | 0, 0, 2000 | Catalogue PM for the moving crosshair (Task 09 E1) |
| `epoch_now` | jyear | `/api/resolve`'s `epoch_now` | Epoch of the grid centre; the crosshair offset is `pm · (t_frame − epoch_now)`. Written by resolve so a shared link does not depend on a client constant |
| `flags` | 0/1 | 0 | Flag overlay (C1 `FLAG_BIT_NAMES`) |
| `maxnan` | percent | 20 | Hide frames with more than this % NaN pixels (mode `all` / `lmdiff`, client-side; omitted from the URL when 20) |
| `hl` | int | — | Highlight mover index (gallery links, Task 09 E6) |

[VERIFY] WiseView's names in recon step R3a. Where WiseView uses a different name for the same meaning, **use WiseView's name** and update this table.

## C11. Motion API — `GET /api/motion?ra&dec&size&det` (Task 08)

```jsonc
{"v":1, "ra":..,"dec":..,"size_deg":..,"det":6,"npix":59,"t0_yr":2025.93,
 "passes":[{"t_yr":2025.41,"n":34,"dx":0.0,"dy":0.0,"sigma":0.12}, ...],
 "registration_ok": true,
 "grid":{"vmax":4.0,"step":0.33,"n":25},
 "elapsed_ms":{"stack":120,"search":340,"measure":15},
 "movers":[{"x0":..,"y0":..,"ra0":..,"dec0":..,"pmra_masyr":..,"pmdec_masyr":..,
            "epmra_masyr":..,"epmdec_masyr":..,"pm_total_arcsec_yr":..,"snr_stack":..,
            "motion_sig":..,"n_pass_used":3,
            "track":[[t_yr, x, y], ...]}]}      // x,y in FITS orientation (row0 = south). Client converts: wire_y = npix-1-y
```
API modes (C6 `/api/sequence` `mode`): add `"lmdiff"` → C5 frames whose data are wavelength-matched residuals (Task 08 M1).
- No result (no frames, < 2 passes, search failed): HTTP 200 with the same keys, `movers: []`, `registration_ok: false`, and `"error": "<reason>"`. Clients MUST show `error` when present (it is not the same as "no motion candidates"). Missing index → 503 `{"error"}`.

---

## Change log
- 2026-11-14 (Track A, late): `/api/spectrum` and `/api/lightcurve` fall back to PSF-wing ring photometry for saturated cores (`photometry`, `ring_radii_px`, `ring`); spectrum caching ignores non-finite points; `/api/resolve` answers known targets locally (`backend/known_targets.py`); FLAG_BITS_BAD no longer includes TRANSIENT/SUR_ERROR.
- 2026-11-14 (Track A, evening): C5 `status` before `header`, `done.zscale`, pass previews (`preview:true`, replace by `i`, round-robin fetch); C6 `/api/spectrum` shape (F3) with PM-following aperture; C9 NaN = grey hatch; C10 `lmdiff`, `epoch_now`, `flags`, `lam_min/lam_max` client-side; C11 `error` key. All 6 detectors indexed via pointing pairs (`config.S3_INDEX_PAIRS`).
- 2026-11-14 (Track A, IRSA outage): `ImageRef.uri` is now the S3 key; `release` is the lowercase S3 prefix (`dr1`/`qr3`/`qr2`), priority per `config.RELEASE_PRIORITY`. Image discovery uses the S3 footprint index (`config.S3_INDEX_PATH`), not TAP. FETCH_METHOD = "s3".
- 2026-11-14 (Track A recon): C2 filename regex confirmed; dedupe rule changed: release priority DR1 > QR3 > QR2 (QR2/QR3 overlap in time), numeric version compare instead of lexicographic (breaks on `l2b_retry-`). C1 instrument values filled from a real DR1 D6 file + Explanatory Supplement v2.1 (see `backend/config.py` comments for evidence).
- 2026-11-14: added C11 (motion) and mode `lmdiff`.
- 2026-11-14: frame `kind`, PM fields in resolve, URL `pmra/pmdec/epoch/hl`, `/api/lightcurve` (Task 09).
- 2026-11-14: initial version. HEALPix nside fixed at 32 (an order-8 index would be ~600M rows).
