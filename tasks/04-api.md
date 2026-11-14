# Task 04 — HTTP API + streaming + caching

**Model:** lower model OK. **Time:** ~1 day. **Depends on:** Tasks 02 and 03.

## Goal
A FastAPI app that streams contract-C5 NDJSON, serves cache hits instantly, and meets the plan.md §3 budgets.

## Files
- `backend/app.py`: FastAPI app, routes
- `backend/resolve.py`: name/coordinate resolution
- `backend/sequence.py`: orchestration (index → cache → fetch → encode)
- `backend/cache.py`: L2 local + S3 precomputed lookups
- `scripts/bench.py`: budget checker
- `tests/test_api.py`, `tests/test_resolve.py`

## Steps

### A1. `backend/resolve.py`
```python
from functools import lru_cache
def parse_coords(q: str) -> tuple[float, float] | None: ...   # contract C3, exactly
@lru_cache(maxsize=4096)
def resolve_name(name: str) -> tuple[float, float] | None:
    """SkyCoord.from_name(name) (Sesame). Return None on NameResolveError. Call it via asyncio.to_thread from routes."""
```
Tests:
- C3 required test.
- `"133.795, -7.245"` → decimal.
- `"10:00:00 +20:00:00"` → (150.0, 20.0).
- `"Barnard"` → None (it's a name).
- Network test: `resolve_name("R Dra")` is not None.

### A2. `backend/cache.py`
```python
def stack_key(grid, det, mode, data_version) -> str: ...      # contract C7
def l2_get(key) -> Path | None: ...
def l2_put_stream() -> "L2Writer": ...                         # .write(line: bytes), .commit(), .abort(); gzip, atomic rename on commit
def s3_url(key) -> str | None: ...                             # if STACKS_BUCKET is configured: HEAD the object (cached in memory 5 min), return the CloudFront URL or None
```
Add `STACKS_BUCKET = ""` and `STACKS_PUBLIC_BASE = ""` (the CloudFront URL) to `config.py`. Empty means disabled.

### A3. `backend/sequence.py`
```python
async def sequence_stream(ra, dec, size, det, mode, timer) -> AsyncIterator[bytes]:
    """
    grid = make_grid(ra, dec, size)
    with timer.stage("index"): refs = index.find_images(grid.ra, grid.dec, det)  (fallback find_images_live if stale/empty)
    key = stack_key(grid, det, mode, index.data_version())
    L2 hit → handled by the route (FileResponse), never reaches here.
    yield header_line(..., n_expected=len(refs) if mode=="all" else <n_passes estimated from refs>, source="live", zscale=None)
    writer = l2_put_stream(); writer.write(<header line with final zscale – see note>)
    mode == "all":
        collect frames as they arrive; yield frame_line immediately;
        after the 10th successful frame → yield zscale_line(zscale_limits(first10))
        errors → yield error_line
    mode == "pass":
        frames must be grouped, so a pass frame can only be emitted once ALL its inputs are done.
        Emit each pass as soon as its last input completes (track remaining count per pass, using pass grouping
        computed from ref times BEFORE fetching: group refs by the gap rule).
    at the end: final zscale over all frames → yield zscale_line; yield done_line.
    L2 write: write the header with the final zscale and the frames sorted by i (buffer in memory; at 0.5° that's
    <= ~5 MB, fine). Commit only if n_failed / n_expected < 0.2. Otherwise abort (don't cache a bad result).
    """
```
Pass grouping before fetch: add `process.group_refs_by_pass(refs) -> list[int]` (same gap rule on `ref.t_mjd`), with a test that it agrees with `group_by_pass` on frames.

### A4. `backend/app.py`
```python
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

@app.get("/api/sequence")
async def api_sequence(ra: float, dec: float, size: float = config.DEFAULT_FOV_DEG,
                       det: int = 6, mode: Literal["all","pass"] = "all"):
    # validate: -90<=dec<=90, 0<=ra<360 (normalise ra % 360), size in FOV_CHOICES_DEG, det in DETECTORS → else 422 JSON
    # 1. key → s3_url(key) → 307 redirect (CloudFront), Cache-Control: no-store on the redirect
    # 2. l2_get(key) → FileResponse(path, media_type="application/x-ndjson",
    #        headers={"Content-Encoding":"gzip","Cache-Control":"public, max-age=31536000, immutable"})
    # 3. StreamingResponse(sequence_stream(...), media_type="application/x-ndjson",
    #        headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})
    # Server-Timing header: for streams, put the index timing in the header (known before streaming starts)
    #   and the full timings in the done line.

@app.get("/api/resolve")  ...   # Cache-Control: public, max-age=86400
@app.get("/api/health")   ...
# Static frontend: app.mount("/", StaticFiles(directory="frontend", html=True)) — mounted LAST
```
Run: `uvicorn backend.app:app --host 0.0.0.0 --port 8000 --workers 1`. **One worker**, because the global semaphore and process pool are per-process. Scale with uvicorn workers only after making the semaphore cross-process (not needed for the MVP).

Logging: `logging.basicConfig(level=INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")`. Each request logs one summary line: ra, dec, size, det, mode, source, n, failed, ms.

### A5. Tests (`tests/test_api.py`)
Use `httpx.AsyncClient(transport=ASGITransport(app))`, with the index monkeypatched to return fake refs and fetch to return fake frames.
- 422 for `size=0.33`, `det=7`, `dec=95`.
- A stream starts with header, ends with done, and the frame count equals the n_expected minus the errors.
- The second identical request is served from L2 (`source` in the header is "cache", or the response is a FileResponse) and calls fetch zero times. *Why: the cache is the main speed mechanism.*
- A failure rate ≥ 20% means nothing is written to L2. *Why: don't cache broken results for a year.*
- mode=pass: the number of frames equals the number of passes, and each has `n_coadd ≥ 1`.
- CORS header present.

### A6. `scripts/bench.py`
CLI: `python scripts/bench.py --base http://localhost:8000 [--cold]`
- For each target in `scripts/targets.py` and size in (0.1, 0.5): request `/api/sequence?det=6&mode=all`.
- Measure: TTFB, time to the header, **time to the first frame line**, time to done, bytes.
- `--cold`: delete the matching L2/L1 files first.
- Print a table and **PASS/FAIL against the plan.md §3 budgets**:
  - cached: first frame < 1 s;
  - cold 0.1°: first frame < 3 s, done < 15 s;
  - cold 0.5°: first frame < 5 s, done < 60 s.
- Exit code 1 if anything fails.

## Done when
1. `pytest -q` passes.
2. With the server running locally and the index built: `python scripts/bench.py --cold`, then `python scripts/bench.py`. Paste both tables. Cached budgets must PASS locally. Cold budgets may fail from a laptop; record the numbers anyway. They're re-checked on EC2 in Task 07.
3. `curl -N "localhost:8000/api/sequence?ra=133.795&dec=-7.245&size=0.1&det=6" | head -c 400` shows header + frame lines.

## Do NOT
- Run uvicorn with more than 1 worker.
- Buffer the whole live response before sending (the first frame must stream).
- Cache responses with ≥ 20% failures.
