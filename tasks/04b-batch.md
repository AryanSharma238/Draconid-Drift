# Task 04b — Batch preprocessing (precomputed catalogue)

**Model:** lower model OK. **Time:** ~1 day. **Depends on:** Task 04 (`sequence_stream` must exist; the batch reuses it).

## Goal
Precompute stacks for a curated object list and upload them to S3, so those objects load in < 300 ms. This uses **the same code path as the live API**, so batch and live output can't drift.

## Files
- `scripts/build_catalog.py` → `data/catalog.csv`
- `scripts/preprocess_catalog.py`
- `tests/test_batch.py`

## Steps

### B1. `scripts/build_catalog.py`
Output: `data/catalog.csv` with columns `name,ra,dec,type,default_det,source`. ra/dec are epoch ~2025.9 positions (PM-propagated where PM is known).
Sources, in order. Stop adding when you reach `--max` (default 500):
1. The 5 test targets from `scripts/targets.py` (always included, `type=test`).
2. **High-PM stars:** Gaia DR3 via `astroquery.gaia`:
   ```sql
   SELECT TOP 300 source_id, ra, dec, pmra, pmdec, phot_g_mean_mag
   FROM gaiadr3.gaia_source WHERE sqrt(pmra*pmra + pmdec*pmdec) > 1000
   ORDER BY sqrt(pmra*pmra + pmdec*pmdec) DESC
   ```
   Propagate to 2025.9 (code as in recon). `type=highpm`, `default_det=3`.
3. **Brown dwarfs:** the UltracoolSheet CSV (public; find the URL in its README, download it into `data/external/`, and record the URL and date in `notes/sources.md`). Take objects with spectral type ≥ T0 and known PM. `type=bd`, `default_det=6`.
4. **Variables:** VSX via astroquery Vizier (`B/vsx/vsx`), the brightest Mira (M) and Orion (IN*) types, with max mag < 9. `type=var`, `default_det=3`.
- Dedupe: any two objects within 5″ → keep the first.
- Log the count per source.

### B2. `scripts/preprocess_catalog.py`
CLI: `python scripts/preprocess_catalog.py --catalog data/catalog.csv [--changed-only] [--limit N] [--concurrency 4] [--upload]`
For each object × det in (default_det, and all 6 if `--all-dets`) × mode in ("pass", "all") × size in (0.1,):
1. Compute the key (contract C7) with the current `data_version`.
2. If `--upload` and the S3 object exists → skip. If it exists in L2 → skip.
3. Drain `sequence_stream(...)` completely (discard the yielded bytes; the L2 writer stores the result).
4. If `--upload`: upload the L2 file to `s3://STACKS_BUCKET/stacks/{key}.ndjson.gz` with `ContentType=application/x-ndjson`, `ContentEncoding=gzip` and `CacheControl=public, max-age=31536000, immutable`. Use `boto3` (add it to requirements.txt).
5. Objects run concurrently, limited to `--concurrency`. The global fetch semaphore still caps IRSA load.
6. `--changed-only`: only objects whose HEALPix cell (nside from config) appears in `data/index/changed_cells.json`.
7. Resumable by construction (step 2).
8. At the end, print and write `data/batch_report.json`: n_objects, n_done, n_skipped, n_failed (with names and reasons), MB, wall s, s/object.

### B3. Catalogue manifest for the frontend
Write `data/catalog_public.json` (`[{name, ra, dec, type}]`) and upload it to the S3 root. The frontend uses it for a "pick a known object" dropdown and for name autocompletion without calling Sesame.

### B4. Tests (`tests/test_batch.py`)
- With fetch mocked: running preprocess on 2 objects creates 2 × (modes) L2 files. Running again creates nothing new and makes zero fetch calls. *Why: the weekly batch must be resumable and cheap.*
- Cell filter: an object whose cell isn't in changed_cells is skipped under `--changed-only`.
- Precomputed bytes == live bytes for the same request, apart from the `source` field. Compare the frames by decoding them. *Why: one code path, no drift.*

### B5. Cost benchmark (required before scaling past 500)
Run on 50 objects on EC2 (Task 07) or your laptop. Record s/object and MB/object in `notes/recon.md`. Extrapolate to 10k objects, both time and S3 storage. **Discuss with the user before running > 500.**

## Done when
- `pytest -q` passes.
- `python scripts/preprocess_catalog.py --limit 5` completes. Paste the report.
- The bench for a precomputed target shows first frame < 300 ms (once S3/CloudFront is up in Task 07; locally < 200 ms from L2).

## Do NOT
- Write a second processing path. Call `sequence_stream`.
- Run the full catalogue before B5 is discussed.
