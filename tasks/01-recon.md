# Task 01 — Data reconnaissance, download, benchmarks

**Model: strong model (Opus) or a human.** Not a lower model. This task resolves every [VERIFY] and sets the constants all later tasks depend on. A wrong value here silently breaks everything downstream.
**Time:** 1–1.5 days. **Depends on:** Task 00.

## Goal
1. Real cutouts for all 5 test targets are on disk.
2. Every [VERIFY] in `backend/config.py` and `docs/contracts.md` is resolved.
3. `notes/recon.md` is filled in.
4. A go/no-go answer on "is motion visible?"
5. A measured choice of fetch method.

## Test targets (decimal ICRS, J2000 positions; objects have moved since)

| name | ra | dec | notes |
|---|---|---|---|
| WISE 0855−0714 | 133.79513 | −7.24515 | from the challenge brief |
| Barnard's Star | resolve "Barnard's Star" | | |
| Epsilon Indi B | resolve "eps Ind B" | | |
| RW Aur | resolve "RW Aur" | | |
| R Dra | resolve "R Dra" | | |

Resolve with `SkyCoord.from_name(name)`, then write the resulting decimal values into `scripts/targets.py` as a dict `TARGETS = {"wise0855": (ra, dec), ...}`.

**Proper motion matters.** These stars have moved 10–30″ from J2000, which is 2–5 px. Use the Gaia DR3 position propagated to 2025.9 for the cutout centre. Use astroquery Gaia (`Gaia.cone_search`) on the J2000 position with radius 60″, pick the source matching the expected PM, and propagate with `SkyCoord(..., pm_ra_cosdec, pm_dec, obstime=Time(2016.0, format='jyear')).apply_space_motion(new_obstime=Time(2025.9, format='jyear'))`. WISE 0855 is not in Gaia: use its 2025 position from SIMBAD/CatWISE, or the brief's position with a larger FOV.

## Steps

### R1. Read the official tutorials (reuse their code)
- IRSA SPHEREx tutorials: https://caltech-ipac.github.io/irsa-tutorials/ — the cutouts, ePSF and source-discovery demos.
- Copy their TAP query and their wavelength-from-`W`-WCS code into `scripts/spx_util.py`, with attribution comments.

### R2. `scripts/download_target.py`
CLI: `python scripts/download_target.py wise0855 --size 0.1 --det 1,2,3,4,5,6`

1. Look up `(ra, dec)` from `scripts/targets.py`.
2. TAP query (sync, `pyvo.dal.TAPService(config.IRSA_TAP_URL).search(adql)`):
   ```sql
   SELECT a.uri, p.planeid, p.time_bounds_lower, p.time_bounds_upper,
          p.energy_bandpassname, p.poly
   FROM spherex.artifact a JOIN spherex.plane p ON a.planeid = p.planeid
   WHERE 1 = CONTAINS(POINT('ICRS', {ra}, {dec}), p.poly)
     AND p.energy_bandpassname IN ('SPHEREx-D1', ..., 'SPHEREx-D6')
   ORDER BY p.time_bounds_lower
   ```
   **Also run `SELECT TOP 5 * FROM spherex.plane` and `... spherex.artifact`**, and record ALL column names in `notes/recon.md`. Columns for release, version and exposure id may exist, which is better than parsing filenames.
3. Save `data/recon/<target>/manifest.csv`.
4. Download cutouts: `IRSA_BASE + uri + f"?center={ra:.6f},{dec:.6f}d&size={size}"`.
   - Use `httpx.AsyncClient(http2=True)`, a semaphore of 16, and 3 retries with backoff `0.5*2**k + random()*0.3`.
   - **The response may be a gzipped FITS:** if `content[:2] == b"\x1f\x8b"`, `gzip.decompress`.
   - Save `data/recon/<target>/<filename>.fits`. Skip files that exist.
5. Print a summary: TAP seconds, n rows, n downloaded, n failed (list them), MB, wall seconds.

### R3. Run R2 for all 5 targets at size 0.1. Also run WISE 0855 at sizes 0.05 and 0.5, for size/latency scaling.

### R3a. WiseView hands-on
- Open WiseView (link in the challenge Resources) for WISE 0855 and Barnard's Star.
- Write `notes/wiseview_parity.md`: every control, what it does, the URL param names (copy a real URL), and what makes motion easy to see.
- Update contract C10 to WiseView names where the meaning matches, and record the change in the contracts change log.

### R4. `scripts/inspect_cutout.py <file.fits>`
Print:
- For each HDU: index, EXTNAME, shape, dtype, bytes. **Record whether PSF dominates the file size.**
- Key IMAGE header cards: everything matching `DATE*, MJD*, EXPTIME, BUNIT, CD*, CDELT*, PC*, CTYPE*, A_ORDER, *W` (alternate WCS).
- Pixel scale computed from the CD matrix: `np.sqrt(abs(np.linalg.det(cd)))*3600`.
- FLAGS header: bit definitions (likely `MP_*`/`HIERARCH` cards). [VERIFY] Copy the exact bit→name map.
- Wavelength at the cutout centre via the tutorial code.
- `median(IMAGE)`, `median(ZODI)`, and whether `median(IMAGE) ≈ median(ZODI)` (not subtracted) or `≈ 0` (subtracted). → `SUBTRACT_ZODI`.

### R5. Coverage statistics (per target), into `notes/recon.md`:

| target | det | n_frames | first date | last date | n_passes (gap > 30 d) | λ min–max at target (µm) |

Also, for WISE 0855 at D1–D6: how many frames fall in each molecule window (CH₄ 3.2–3.4, H₂O 2.9–3.1, CO₂ 4.2–4.35, CO 4.6–4.75 µm)? **This decides whether Phase 6 F5 presets are viable.**

### R6. Global numbers
- `SELECT COUNT(*) FROM spherex.plane` → expected index size. If > 5M rows, flag it.
- Which releases are present, and do the same exposures appear in both DR1 and QR3? Find one duplicate and record how to detect it (which columns match). → dedupe rule C2.
- Verify the filename regex on 20 random URIs. Write the regex into `notes/recon.md`.
- `time_bounds_lower`: confirm it's MJD (value ~60800–61400).
- Polygon format returned by pyvo for `p.poly` (string? array?). Paste an example.

### R7. Cloud (S3) access
- From the IRSA cloud access page: bucket name, region, key layout for L2 images, and whether the files are tile-compressed.
- Test from your laptop (anonymous):
  ```python
  from astropy.io import fits
  with fits.open("s3://<bucket>/<key>", use_fsspec=True, fsspec_kwargs={"anon": True}) as h:
      print([x.name for x in h]); sec = h["IMAGE"].section[1000:1060, 1000:1060]
  ```
  Record whether `.section` works, and the bytes transferred (watch with `fsspec` logging or a time comparison).

### R8. Manual blink check (go/no-go)
In `notebooks/blink_check.ipynb`, for WISE 0855 and Barnard's Star:
1. Reproject every frame of D6 (and D5) onto `make_grid(ra, dec, 0.1)` with `reproject_interp`.
2. Group into passes (gap > 30 d) and take the median co-add of each pass.
3. Show the first and last pass side by side and as a 2-frame blink (matplotlib animation, or save 2 PNGs to `notes/`).
4. Measure the source centroid shift in px (`photutils` is optional; a weighted centroid in a 5×5 box is fine).
   - **GO** if the shift is ≥ 1 px and visible by eye.
   - **NO-GO:** write down what you'd change (longer λ, more co-adding, bigger zoom) and stop. Discuss before Phase 2.
5. Check static field stars: their centroids differ by < 0.5 px between passes. If not, the WCS/reprojection has a problem. Investigate SIP first.

### R9. Fetch benchmark `scripts/bench_fetch.py`
- On WISE 0855 D6, 50 frames, size 0.1, compare:
  - (A) IBE cutout URL;
  - (B) S3 `.section` read of IMAGE + FLAGS (+ VARIANCE). Compute the pixel box from the full header WCS, padded by 3 px.
- Run at concurrency 8/16/32. Report p50/p95 per frame, total wall time and total bytes.
- Also report time-to-first-complete-frame.
- **Decision:** set `FETCH_METHOD` in config. Note that from a laptop, S3 is cross-region; it will be faster from EC2 in the bucket's region. If B is within 1.5× of A from a laptop, choose B.

### R10. Save test fixtures for later tasks (no network in unit tests)
- `tests/data/tap_wise0855_d6.csv` (manifest).
- `tests/data/cutout_wise0855_d6_0.fits` (one real cutout, smallest size) — **exempt it in .gitignore** (`!tests/data/*.fits`).
- `tests/data/tap_poly_example.txt`.

### R11. Fill in and commit
1. Replace every `[VERIFY]` in `backend/config.py` and `docs/contracts.md` with the real value, or write `# CONFIRMED <date>: <evidence>`.
2. Add a change-log entry.
3. `grep -rn "VERIFY" backend docs` must return nothing (or only items explicitly deferred, each listed in `notes/recon.md` under "Deferred").

## Done when
- `notes/recon.md` contains: the R4 HDU table, R5 coverage table, R6 numbers, R7 result, R8 verdict + images, R9 benchmark table + decision.
- `grep -rn "VERIFY" backend docs` → empty, or deferred items only.
- R8 verdict is **GO**.

## Do NOT
- Build the index, API or frontend.
- Hand this task to a lower model.
