# SPHEREx Blink Tool — Detailed Plan

> **ACTIVE: `tasks/SPRINT.md`. The deadline is end of day 2026-11-15.** SPRINT.md overrides this plan and cuts most of it. This file and the full task files describe the post-hackathon version.

## How to execute this plan (read first)

This file is the **rationale**. Implementation detail lives in:

| File | Purpose |
|---|---|
| `CLAUDE.md` | Rules for coding agents (auto-loaded by Claude Code) |
| `docs/contracts.md` | **Authoritative** data formats, config, API, cache keys, orientation |
| `tasks/00-setup.md` | Repo, venv (Python 3.14 + uv), config, fixtures |
| `tasks/01-recon.md` | **Strong model or human only.** Resolves every [VERIFY]; go/no-go |
| `tasks/02-index.md` | Local metadata index |
| `tasks/03-fetch-process.md` | Fetch, reprojection, co-add, encoding |
| `tasks/04-api.md` | FastAPI streaming + caches + benchmark |
| `tasks/04b-batch.md` | Precomputed catalogue |
| `tasks/05-frontend.md` | Blink viewer MVP |
| `tasks/06-features.md` | F1–F10, one per session, in value order |
| `tasks/07-deploy.md` | AWS EC2 + S3 + CloudFront |

Order: 00 → 01 → (02 ∥ start of 03 Part A/B) → 03 Part C → 04 → 05 → 04b → 07 → 06 features.
Hand a lower model **one task file per session**, with the instruction: "Follow CLAUDE.md. Do tasks/NN-*.md. Report in the format from CLAUDE.md."
If a task file and the step summaries in §6 below disagree, **the task file wins**; §6 is a summary.

Status: DRAFT for discussion. **[VERIFY]** = not yet confirmed against real data. Every [VERIFY] is resolved in Phase 1 before dependent work starts.

---

## 1. Problem

Public web tool: user enters RA/Dec (or a name) and a field of view. Tool shows a time-ordered sequence of SPHEREx cutouts of that patch as a blink/animation, so moving or variable sources are easy to spot. **It must be fast.**

Test targets:

| Target | Why | Expected signal |
|---|---|---|
| WISE 0855−0714 (08h55m10.831s, −07d14m42.53s) | Fast-moving, very cold brown dwarf | ~8″/yr. Faint at short λ, brighter ~4.5 µm |
| Barnard's Star | Highest proper motion star | ~10.4″/yr. **Very bright → possible saturation** |
| Epsilon Indi B (Ba/Bb) | Brown dwarf pair | ~4.7″/yr. Pair unresolved at SPHEREx pixel scale |
| RW Aurigae | Variable young star | Brightness change, no motion |
| R Draconis | Mira variable (~245 d) | Large brightness change, no motion |

## 2. Hard facts that shape the design

1. **Pixel scale ~6.2″ [VERIFY].** The ~1.5 yr baseline gives ~12–15″ of motion ≈ **2 px** for the best targets. Visible but subtle. Needs good blink UX, zoom and a crosshair.
2. **One detector (D1–D6) per image. Each pixel has a different wavelength** (LVF, 0.75–5.0 µm). Blinking mixed detectors shows colour change instead of motion. **Default: one sequence per detector.**
3. **Observations cluster** into ~3 passes (6-month cadence), each with many frames over days to weeks. Offer "all frames" and "co-add per pass" modes.
4. **Frames differ in rotation and pointing** → reproject onto a common north-up TAN grid centred on the input position.
5. **Data access (from IRSA docs):**
   - TAP `https://irsa.ipac.caltech.edu/TAP`: `spherex.plane` ⨝ `spherex.artifact` on `planeid`. Filter with `CONTAINS(POINT('ICRS',ra,dec), p.poly)=1`, plus `p.energy_bandpassname`. Order by `p.time_bounds_lower`.
   - Cutout: `https://irsa.ipac.caltech.edu/<a.uri>?center=<ra>,<dec>d&size=<deg>` (gzipped).
   - HDUs: `IMAGE, FLAGS, VARIANCE, ZODI, PSF, WCS-WAVE`. Wavelength via alternate WCS key `W`.
   - IRSA example: TAP took ~20 s; 84 cutouts took ~13 s with 10 workers; **some downloads fail.**
   - Releases: QR3 weekly, DR1 (Nov 2026). Cloud copy exists (IRSA cloud access page) [VERIFY bucket/format].
6. **Reference tool: WiseView** (listed in the challenge Resources). Used by Backyard Worlds volunteers for hundreds of discoveries. Judges and power users will compare against it, so:
   - Match its core UX: blink between epoch co-adds, contrast min/max, speed, zoom, FOV size, difference mode, Gaia overlay, crosshair.
   - Mirror its URL parameter names where the meaning is the same (`ra, dec, size, speed, minbright, maxbright, zoom, diff, gaia, invert` [VERIFY exact names against WiseView]), so existing users and links carry over.
   - Then beat it where SPHEREx is unique: wavelength/molecule views and spectra.
7. **Gaia:** use DR3 (Resources tab requirement). It has proper motions and parallaxes, needed for epoch propagation.

## 2a. Out of scope (explicit)

- **Asteroid/comet tracking.** They move ~0.25°/day and leave a small FOV between visits, so tracking needs ephemeris-following cutouts (JPL Horizons). Different feature. Revisit only after the MVP.
- **NEOSSat / CADC data** (CSA resources). They come from a different mission, have a different access path, and do nothing for the SPHEREx blink goal. Maybe a later overlay, but not planned.
- **Whole-sky preprocessing / mosaics.** See section 3a.

## 3. Where the time goes, and the speed strategy

Before any optimisation, the cold path for one request is:

| Stage | Naive cost | Fix |
|---|---|---|
| Name resolve | 0.3–1 s (Sesame) | Cache. Skip entirely for numeric RA/Dec |
| **Metadata query (TAP)** | **~20 s** | **Local spatial index (HEALPix → image rows). Target < 50 ms** |
| **Cutout downloads** (50–300 files) | **10–60 s** | Parallel async HTTP/2, partial reads of IMAGE+FLAGS only, cache, stream |
| Unwanted HDUs (PSF can be large [VERIFY]) | Wasted bytes | Cloud range-reads of just the needed HDUs, or verify the cutout service's size |
| Reproject + stretch | ~5–20 ms/frame at 0.1° | Process pool. Negligible next to network |
| Transfer to browser | Small (see below) | One binary stack, compressed |
| Client render | — | Canvas, all frames preloaded, stretch in JS |

**PNG vs WebP vs JPEG barely matters for speed. The network round-trips to IRSA matter.**

### Performance budgets (these are acceptance tests)

| Scenario | Target |
|---|---|
| Cached request (any user, same/nearby position) | Server < 200 ms, first frame on screen < 1 s |
| Cold request, 0.1° FOV, one detector | Metadata < 100 ms, **first frame < 3 s**, full sequence < 15 s |
| Cold request, 0.5° FOV | First frame < 5 s, progress shown, full < 60 s |
| Scrub / play / change stretch | < 16 ms per frame (60 fps), zero server calls |
| Page load (HTML+JS+CSS) | < 100 KB, no framework, no build step |

### Key speed decisions

1. **Local metadata index (biggest win).** Mirror image metadata (`obs_id, detector, time, uri, footprint polygon`) into SQLite/Parquet, indexed by HEALPix cell (nside 32 / order 5, ≈ 1.8° cells; finer cells would explode the index to ~600M rows, because footprints are ~3.5°). A lookup becomes a single-cell query plus a point-in-polygon check, in milliseconds instead of ~20 s. A weekly sync job appends new QR/DR rows, so new data stays automatic. Size: ~1–3M rows, a few hundred MB [VERIFY row count]. Fallback: live TAP if the index is stale or the cell is empty.
2. **Fetch only bytes we need.**
   - Option A: IBE cutout service as documented. Simple. Downloads all 6 HDUs.
   - Option B: cloud copy plus `astropy.io.fits.open(..., use_fsspec=True)` and `.section[...]` range reads of only the IMAGE/FLAGS pixel box. Fewer bytes, no IRSA cutout server in the loop. Best if the backend is deployed in the same cloud region.
   - Benchmark A vs B in Phase 1 and pick by measured latency, not theory.
3. **Async parallel fetch with streaming.** `httpx.AsyncClient` (HTTP/2, pooled), concurrency 16–32 (polite cap; tune), 3 retries with jittered backoff, per-request timeout. Frames are pushed to the browser **as each one finishes** (NDJSON or SSE), so the user sees the first frame in ~1–3 s while the rest stream in.
4. **Ship a numeric stack, not images.** At 0.1° a frame is ~58×58 px. 100 frames × 58×58 × 2 B (float16) ≈ **0.7 MB raw, ~0.3–0.5 MB gzipped**. The browser holds the whole stack and does the stretch in JS (canvas 2D is enough at this size; WebGL only if FOV ≥ 0.5° makes JS too slow). Benefits: instant stretch changes, client-side diff and co-add, no PNG pipeline. Large FOV: downsample server-side or quantise to uint16 to stay within a **5 MB payload cap**.
5. **Two-level cache.**
   - L1 (raw): cutout/section per `(obs_id, snapped_center, size)` on disk, LRU-capped.
   - L2 (processed): the final reprojected stack per `(snapped_center, fov, det, mode, data_version)`, served as a static file with `Cache-Control: immutable` + ETag. CDN-friendly.
   - **Snap the centre** to the pixel grid (e.g. 1 px ≈ 6″) so nearby requests share cache entries.
   - Pre-warm the 5 test targets and a list of popular objects.
6. **Measure everything.** Per-stage timing in logs plus a `Server-Timing` header. A benchmark script runs the budgets on every change. No optimisation without a number.

## 3a. Preprocessing strategy (decided: hybrid, hosted on AWS EC2)

Preprocessing the **whole sky** is not feasible. It's hundreds of TB of L2 images [VERIFY total archive size] and would effectively mean building per-pass all-sky mosaics. Instead:

| Tier | What | Latency | How |
|---|---|---|---|
| **1. Precomputed** | Curated catalogue: 5 test targets + known brown dwarfs, high-PM stars, notable variables (~1–10k objects) | **< 300 ms, served as static files** | Nightly/weekly batch job on EC2 → stacks on S3 → CloudFront |
| **2. On-demand + cache** | Any other RA/Dec | Cold: first frame < 3 s. Afterwards same as tier 1 | Live path (sections 3–4); result written to the same store, so it becomes tier 1 |
| **3. Index only** | Metadata for every image | < 50 ms | Local HEALPix index (decision 1) |

Catalogue sources [VERIFY availability/format]:
- Brown dwarfs: Backyard Worlds / UltracoolSheet.
- High-PM stars: Gaia DR3 with PM > 1″/yr.
- Variables: VSX, filtered to bright ones.
- Start with ~500 objects. Grow once the batch cost is measured.

**AWS layout:**
- **Region:** same as the IRSA SPHEREx S3 bucket (expected `us-east-1` [VERIFY]). In-region S3 reads have no egress cost and low latency, which makes Option B (range reads) the default fetch path.
- **Serving:** one EC2 instance (e.g. `t4g.medium`/`c7g.large`, ARM) running FastAPI + uvicorn behind nginx or Caddy (TLS). EBS gp3 ~50 GB holds the SQLite index and the L1 raw cache.
- **Batch:** a separate spot instance (e.g. `c7i.2xlarge`) runs `preprocess_catalog.py` and stops when done. It doesn't compete with serving.
- **Storage:** precomputed stacks go to our own S3 bucket, keyed `stacks/{data_version}/{healpix}/{ra}_{dec}_{fov}_{det}_{mode}.bin`.
- **CDN:** CloudFront in front of S3 (stacks) and EC2 (API). The static frontend is also on S3/CloudFront.
- **Cost guard:** budget alarm. At ~0.5 MB per stack × 6 detectors × 2 modes × 10k objects ≈ 60 GB, S3 cost is negligible. CloudFront egress is the variable cost.

## 4. Architecture

```
Browser (static HTML/JS, <100 KB)
  │ GET /api/sequence?ra&dec&fov&det&mode     (NDJSON stream: header, then frames)
  ▼
FastAPI (async)
  ├─ resolve   numeric parse | Sesame (cached)
  ├─ index     SQLite/Parquet HEALPix lookup → [ImageRef]   (fallback: live TAP)
  ├─ L2 cache  hit → stream stored stack immediately
  ├─ fetch     async HTTP/2 pool or fsspec range reads → L1 cache
  ├─ process   ProcessPool: reproject → flag-mask → background-sub → float16
  └─ stream    emit each frame as ready; write L2 on completion
Weekly cron: sync_index.py (new releases) → bump data_version → L2 keys invalidate naturally
```

## 5. Tech stack and layout

Python 3.14 via `uv` (3.15 lacks astropy wheels as of 2026-11-14): `astropy`, `astroquery`, `pyvo`, `reproject`, `astropy-healpix`, `numpy`, `httpx[http2]`, `fsspec`/`s3fs`, `fastapi`, `uvicorn`, `orjson`, `pytest`. Frontend: vanilla JS + canvas.

```
backend/   app.py  resolve.py  index.py  fetch.py  process.py  cache.py  stream.py  timing.py
frontend/  index.html  app.js  render.js  style.css
scripts/   build_index.py  sync_index.py  download_target.py  inspect_cutout.py  bench.py
data/      index/  cache/raw/  cache/stacks/        (gitignored)
tests/
```

---

## 6. Step-by-step

Each step has a **Done when** condition. Don't start a step until its dependencies are done.

### Phase 0 — Setup (½ day)
1. `git init`. Add a `.gitignore` covering `data/`, `.venv/`, `__pycache__/`, `*.fits*`.
2. Create the venv, install the stack, `pip freeze > requirements.txt`.
3. Write the `timing.py` helper (context manager that logs stage durations). **Done when:** importable, used from step 4 onward.

### Phase 1 — Recon, download, benchmarks (1–1.5 days). Gates everything.
3a. **Read the resources first:**
   - Work through the IRSA SPHEREx tutorial notebooks (cutouts, ePSF, source discovery demo). Reuse their query and wavelength code rather than reinventing it.
   - Use WiseView hands-on with WISE 0855 and Barnard's Star. Write `notes/wiseview_parity.md`: the feature list, URL params and what makes motion easy to see.
   - **Done when:** both notes exist and section 2 facts are corrected wherever the notebooks contradict them.
4. **`scripts/download_target.py <name | ra dec> --size 0.1 --det D1..D6`**
   - TAP query (section 2.5). Write `manifest.csv` (obs_id, det, time, uri, release).
   - Async download (concurrency 16, 3 retries). Skip files that exist. Log and print failures with a count at the end.
   - Print stage timings: TAP, total download, MB.
   - **Done when:** WISE 0855 downloads fully, or the failures are listed explicitly.
5. Run it for all 5 targets. Record per target in `notes/recon.md`: frames per detector, time span, number of passes, MB, TAP time, download time, failure rate.
6. **`scripts/inspect_cutout.py <file>`**
   - Print HDU names, shapes, byte sizes (is PSF large?), pixel scale, time keywords, units.
   - Print the wavelength at the target pixel (WCS key `W`) and the FLAGS bit definitions.
   - Check whether IMAGE is already zodi-subtracted.
7. Resolve the [VERIFY] items:
   - [ ] Pixel scale. Expected WISE 0855 shift in px.
   - [ ] Which detectors detect WISE 0855? Is Barnard's saturated, and which flag bit marks it?
   - [ ] Pass structure (time clustering).
   - [ ] DR1 vs QR3 duplicates: what's the dedupe key?
   - [ ] Size of each HDU in a cutout at 0.05°, 0.1°, 0.5°.
   - [ ] Total row count of `spherex.plane` (sizes the local index). TAP `SELECT COUNT(*)`.
   - [ ] Cloud bucket name, file format (tile-compressed?), and whether `.section` range reads work on it.
8. **Manual blink check** (notebook): reproject first and last pass co-adds for WISE 0855 at D6 and flip them. **Done when:** motion is seen by eye. **If not: stop and re-plan** (longer λ, more co-adding, larger zoom).
9. **Fetch benchmark (`scripts/bench_fetch.py`):** for 50 frames of WISE 0855, measure (A) IBE cutout and (B) fsspec range read. Report p50/p95 latency per frame, total wall time and bytes at concurrency 8/16/32. **Done when:** A or B is chosen, with numbers written into `notes/recon.md`.

### Phase 2 — Local metadata index (1 day)
10. **`scripts/build_index.py`:** page through `spherex.plane ⨝ artifact` via TAP (async TAP job, chunked by time range or HEALPix), and store `obs_id, det, t_mid, release, uri, polygon` in SQLite.
    - Precompute the HEALPix cells (order 8) each footprint touches → table `cell(cell_id, obs_id)` with an index on `cell_id`.
    - **Done when:** full build completes, and the row count matches `SELECT COUNT(*)` from step 7.
11. `index.py: find_images(ra, dec, det=None)` does a cell lookup, then an exact point-in-polygon filter, dedupes (step 7 key) and sorts by time.
    - **Test:** for the 5 targets, the result set equals the live TAP result set (same obs_ids). If they differ, the index is wrong.
    - **Done when:** p95 < 50 ms.
12. **`scripts/sync_index.py`:** incremental by `max(t_mid)`/release. It is idempotent and bumps `data_version`. Run it weekly via cron. Fall back to live TAP if the index is older than N days or a cell returns 0 rows.

### Phase 3 — Fetch + process core (1.5 days)
13. `fetch.py`: async fetch using the method chosen in step 9, with L1 disk cache, retries, timeouts and a global concurrency semaphore (shared across user requests, to protect IRSA). It returns an async iterator yielding frames as they complete.
14. `process.py` (pure functions, no I/O):
    - `target_grid(ra, dec, fov, pixscale)`: TAN WCS, north up, centre snapped to the pixel grid.
    - `reproject_frame(img, wcs, grid)` with `reproject_interp`, and `mask_flags(img, flags, bits)` → NaN.
    - `background_subtract(img)`: ZODI HDU or sigma-clipped median.
    - `group_by_pass(frames, gap_days=30)` and `coadd(frames, variances)` (inverse-variance weighted).
    - `to_float16(stack)`, plus `zscale_limits(stack)`, computed once over the whole stack and sent as defaults.
    - Run frame processing in a `ProcessPoolExecutor`, so CPU work never blocks the event loop.
15. Tests, which encode why each property matters:
    - **Alignment:** a synthetic star at a fixed RA/Dec, injected into two frames with different rotation/offset, lands on the same output pixel (±0.2 px). Without this, static stars jitter and fake motion.
    - **No flicker:** identical sky values produce identical displayed values across frames under the default stretch.
    - **Co-add SNR:** N equal-variance frames → noise drops by ≈ √N (±10%).
    - **Dedupe:** the same exposure from DR1 and QR3 appears once.

### Phase 4 — API + streaming (1 day)
16. `GET /api/sequence?ra&dec&fov&det&mode` returns NDJSON:
    - Line 1: header `{grid_wcs, n_expected, zscale, data_version, failures:0}`.
    - Then one line per frame: `{i, obs_id, t, det, lambda_um, pass}` + base64 float16, or a binary multipart.
    - L2 hit → stream the stored stack at once. Miss → stream as frames finish, then write L2.
17. `GET /api/resolve?q=` (cached). `GET /api/health`.
18. Add a `Server-Timing` header and structured per-stage logs.
19. **Done when:** `scripts/bench.py` meets the section 3 budgets for cached and cold 0.1° requests on all 5 targets.

### Phase 4b — Batch preprocessing (1 day)
19a. **`scripts/build_catalog.py`:** assemble `catalog.csv` (name, ra, dec, epoch, pm_ra, pm_dec, type, default_det) from the sources in section 3a. Dedupe within 5″. Start with ~500 rows, including the 5 test targets.
19b. **`scripts/preprocess_catalog.py`:**
   - For each object × detector × mode, run the same `fetch → process` code as the API (no separate code path, so they can't drift) and upload the stack + header JSON to S3.
   - Resumable: skip keys that already exist for the current `data_version`. Parallel across objects.
   - Logs a summary: done, failed (with reasons), MB, wall time.
19c. **Benchmark the batch** on 50 objects: seconds and $ per object. Extrapolate to 10k before scaling up. **Done when:** cost/time per object is recorded in `notes/recon.md`.
19d. After each `sync_index` run, re-preprocess **only objects whose cell got new images**. Old `data_version` keys are kept until the CDN TTL expires, then deleted.
19e. API: check the precomputed S3 key first (tier 1) → L2 cache → live path. **Test:** a test target is served from tier 1 without any IRSA call (assert the fetch mock is never called).

### Phase 5 — Frontend (1.5 days)
20. `index.html`, no framework:
    - Inputs: name or RA/Dec (sexagesimal or decimal), FOV (0.05/0.1/0.25/0.5° with an estimated MB), detector, mode.
21. `app.js`: `fetch()` the stream and parse NDJSON incrementally. **Render frame 0 the moment it arrives.** Show progress ("37/112 frames, 2 failed").
22. `render.js`:
    - Keep the stack as `Float32Array`s; apply the stretch (zscale defaults → asinh) into a reused `ImageData` via a precomputed LUT.
    - `requestAnimationFrame` playback. Use `image-rendering: pixelated` for nearest-neighbour zoom.
    - Crosshair at the input position. Frame label: date, detector, λ.
    - Keyboard: space play/pause, ←/→ step.
23. URL state sync via `history.replaceState`. Loading the URL reproduces the view.
   - Params: `ra, dec, size, det, mode, speed, minbright, maxbright, zoom, diff, lam_min, lam_max, preset, gaia, catwise` (WiseView names where applicable, see 2.6).
   - Accept both sexagesimal and decimal RA/Dec.
23a. **Embed mode:** `&embed=1` hides the input form and chrome, and shows just the player, a minimal control bar and an "open full tool" link. README documents an `<iframe>` snippet.
24. Error and empty states: no coverage, IRSA down (serve stale cache if available), partial failures.
25. **Done when:**
    - All section 3 success criteria are visually confirmed on the 5 targets.
    - The stretch slider holds 60 fps (DevTools perf trace).
    - Page weight < 100 KB.

### Phase 6 — Features, in value order (cut from the bottom)
All arithmetic runs client-side on the float stack, so there are no extra server calls. Each frame already carries `lambda_um` (λ at the target pixel) and `pass`.

26. **Time arithmetic:**
   - Presets: frame − median(stack), and pass N − pass 1.
   - **Custom A − B:** the user ticks frames or passes into set A and set B, and the tool shows sum(A) − sum(B), or mean(A) − mean(B) for unequal counts. A diverging colormap shows movers as +/− pairs.
   - Plain "sum selected frames" as well, for SNR.
   - **Test:** a static synthetic star cancels to < 3σ noise in A − B; a moving one leaves a +/− pair.
27. **Gaia DR3 + CatWISE2020 overlay** (promoted; this is how users tell "known mover" from "candidate new object"):
   - Backend proxy with a cached cone search: Gaia via astroquery/TAP, CatWISE via IRSA TAP.
   - Positions are propagated to **each frame's epoch** using pm_ra/pm_dec, so markers move with the star in the blink.
   - CatWISE is included because it has proper motions for faint red objects that Gaia misses (brown dwarfs).
   - Toggle and click-for-details (G mag, PM, parallax). Precompute for catalogue objects in batch (19b).
   - **Test:** for Barnard's Star, the propagated Gaia marker lies within 1 px of the source centroid in each pass co-add.
28. **Spectrum on the fly** (promoted; "extra kudos" in the brief):
   - Click a position → forced aperture photometry in every frame across all 6 detectors.
   - Each frame gives one (λ, flux, σ) point → 102-band spectrum, coloured by pass. Variability shows up as pass-to-pass offsets.
   - Cold path: lazily fetch the missing detectors (~6× bytes), with a progress bar. Catalogue objects are precomputed in batch.
   - Download as CSV.
   - **Test:** a synthetic flat-spectrum source yields a flat recovered spectrum within the noise.
29. **Wavelength filter:** a λ-range slider (`lam_min, lam_max`) that keeps only frames whose `lambda_um` falls in range. This uses all 102 bands instead of just 6 detectors. Co-add per pass within the selected range.
30. **Molecule-band presets** (the "creative use of 102 bands" answer, based on real astrophysics):

   | Preset | Window | Use |
   |---|---|---|
   | CH₄ | 3.2–3.4 µm vs continuum 3.9–4.1 µm | Brown dwarfs dark in CH₄, bright at ~4 µm |
   | H₂O ice | 2.9–3.1 µm | Icy bodies, comets, YSOs |
   | CO₂ | 4.2–4.35 µm | Comets, ices |
   | CO | 4.6–4.75 µm | Comets, cool stars |
   | Continuum | user-defined | Baseline |

   Display a single window, or a **ratio image** (band / continuum).
   - [VERIFY] each window has frames at typical positions after Phase 1.
   - **Test:** WISE 0855 stands out in the CH₄-ratio image relative to field stars.
31. **"Cold object" colour view:** short-λ (D1–D2) vs long-λ (D6) pass co-adds as a ratio, or mapped to RGB (D2/D4/D6). Very red sources stand out as brown dwarf candidates.
32. **Flags overlay + metadata panel:** contamination and saturation flag bits (toggle per bit), plus obs time, detector, λ, processing version and release (DR1/QR3).
33. **Moving-source hint (simple, not ML):**
   - Detect blobs in each pass co-add (sigma threshold) and match them across passes.
   - Flag a blob that shifts by ≥ 1 px between passes *and* has no Gaia/CatWISE counterpart at its propagated position.
   - Label it "candidate, inspect" and never "discovery".
34. **Aladin Lite context panel** (lazy-loaded): surrounding sky, and a 2MASS/WISE HiPS layer as an extra blink layer for a longer time baseline.
35. **GIF export**, client-side from the canvas frames.

WiseView parity check: after Phase 6, tick every item in `notes/wiseview_parity.md`, or record why it was dropped.

### Phase 7 — Deploy + harden (1 day)
36. Dockerfile (ARM + x86). Launch a serving EC2 in the SPHEREx bucket's region. Attach EBS gp3 for the index and L1 cache. Use an IAM role with read access to the public SPHEREx bucket and read/write access to our stacks bucket. No static keys on disk.
37. Create the S3 stacks bucket and CloudFront distribution: `/stacks/*` → S3 (immutable, versioned keys), `/api/*` → EC2 (no cache, except `/api/resolve`), `/` → S3 static frontend.
38. Batch: a spot-instance launch template + a cron (EventBridge) that runs `sync_index.py` → `preprocess_catalog.py --changed-only` weekly, then terminates.
39. Embeddability headers:
   - Frontend responses send `Content-Security-Policy: frame-ancestors *`, and **no** `X-Frame-Options: DENY`.
   - API sends CORS `Access-Control-Allow-Origin: *` for GET, so other projects can call it directly.
   - **Test:** an iframe on a different origin loads and plays.
40. Ops: CloudWatch logs + alarm on API p95 latency and 5xx. AWS Budgets alarm. EBS snapshot of the index weekly.
41. Guard rails:
    - Max FOV 0.5°.
    - Per-IP rate limit.
    - Global IRSA concurrency cap.
    - Cache size cap with LRU eviction.
42. Pre-warm the 5 targets and a popular-objects list after each `sync_index` run.
43. README: usage, URL params, embed snippet (`<iframe src="...?ra=..&dec=..">`), and how new data flows in (weekly sync).
44. Demo: record GIFs for each target, run `bench.py` against production, and include the numbers.

---

## 7. Success criteria

- [ ] WISE 0855 shift visible between the first and last pass in D5/D6 co-adds.
- [ ] Barnard's Star shift visible, or saturation flagged and handled.
- [ ] RW Aur and R Dra brightness change visible under a fixed shared stretch.
- [ ] Static stars aligned to < 0.5 px across frames.
- [ ] No stretch flicker.
- [ ] URL reproduces the view; embeddable via iframe.
- [ ] All section 3 performance budgets met, as measured by `bench.py`.
- [ ] New releases appear after the weekly sync with zero code change.

## 8. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Motion only ~2 px | Underwhelming demo | Pass co-adds, difference mode, zoom, crosshair; gate at step 8 |
| Full index build too slow or TAP-limited | No fast metadata | Build in chunks/async jobs; fall back to live TAP + cache. Start the build early, it can run overnight |
| Cloud range reads unsupported or slow | Lose Option B | Option A + caching still meets cached budgets. Cold budget may slip; measure in step 9 |
| IRSA latency spikes or failures | Slow or partial sequences | Streaming, retries, stale-cache serving, failure count visible |
| Barnard's saturation / WISE 0855 invisible at short λ | Confusing frames | Per-detector availability shown; sensible default detector per target |
| WCS/SIP quirks | Jitter | Alignment test (step 15) + field-star check |
| DR1/QR3 duplicates | Doubled frames | Dedupe key from step 7, tested |
| Scope creep | Nothing ships | Phase 6 cut from the bottom; performance budgets are fixed and not cut |

## 9. Open questions

1. Solo or team? Hard deadline?
2. ~~Hosting~~ → decided: AWS EC2 + S3 + CloudFront. Still open: AWS account limits/budget? Is a domain name needed?
2b. Catalogue scope for precomputation: is ~500 objects enough for the demo, or do you want ~10k?
3. Priority: public polish vs scientific features (spectra, difference)?
4. Is GIF/video export required, or is the in-page player enough?
5. Are the budgets in section 3 the right definition of "fast" for you? If you mean something else (e.g. instant for *any* position on the sky), that changes the design. It would need pre-computed per-pass sky mosaics, i.e. a tile pyramid after all, which is much heavier.

## 10. Immediate next action

Phase 0 → Phase 1 steps 4–9. Start the full index build (step 10) as soon as the row count is known, since it can run in the background while the rest of Phase 1 continues.

## Sources
- IRSA SPHEREx overview: https://irsa.ipac.caltech.edu/data/SPHEREx/docs/overview_qr.html
- Cutout tutorial: https://caltech-ipac.github.io/irsa-tutorials/spherex-cutouts/
- IBE cutout syntax: https://irsa.ipac.caltech.edu/ibe/cutouts.html
- SPHEREx data access docs: https://caltech-ipac.github.io/spherex-archive-documentation/spherex-data-access/
- Challenge Resources tab: IRSA SPHEREx resources, SPHEREx tutorial notebooks, WiseView, Gaia DR3, CSA NEOSSat/CADC (out of scope)
