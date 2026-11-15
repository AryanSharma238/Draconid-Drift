"""FastAPI app: /api/sequence (C5 stream, L2 cache), /api/motion, /api/lightcurve, /api/resolve, /api/health, static frontend.

Run with ONE worker (the fetch semaphore and process pool are per-process):
    uvicorn backend.app:app --host 0.0.0.0 --port 8000 --workers 1
"""

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend import cache, config, fetch, index, resolve
from backend.models import make_grid
from backend.sequence import (
    MODES,
    JsonResult,
    group_refs_by_pass,
    lightcurve_result,
    load_frames,
    motion_result,
    sequence_stream,
    spectrum_result,
    warm_worker,
)
from backend.timing import Timer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")

NDJSON = "application/x-ndjson"
# Not in config.py (Track A owns it): ask to move.


async def _warm_pool(name: str, pool: Any, n: int, s3: bool) -> None:
    """One blocking-ish warm_worker job per worker: while a job runs its worker is busy, so the executor
    spawns a new process for the next job and all `n` workers exist (and have imported astropy) afterwards."""
    futs = [asyncio.wrap_future(pool.submit(warm_worker, s3=s3)) for _ in range(n)]
    pids = await asyncio.gather(*futs)
    log.info("warm-up: %s pool ready, %d distinct workers", name, len(set(pids)))


async def warm_up() -> float:
    """Spawn both fetch process pools' workers and load the footprint index(es), so the first real request
    does not pay process spawn + imports (~3-4 s on EC2). Never raises: a failure is logged loudly and the
    app starts anyway (the first request is then just slow). Returns the duration in seconds."""
    t0 = time.perf_counter()

    async def pools() -> None:
        await asyncio.gather(
            _warm_pool("process", fetch._get_pool(), config.PROCESS_WORKERS, s3=False),
            _warm_pool("s3-read", fetch._get_read_pool(), config.S3_READ_WORKERS, s3=True),
        )

    async def indexes() -> None:
        for path in dict.fromkeys(config.S3_INDEX_PATHS.values()):  # D6 first, then D4/D5 where built
            try:
                await asyncio.to_thread(index._load, path)
            except FileNotFoundError:
                log.warning(
                    "warm-up: footprint index %s not present (detectors using it are 'missing')", path
                )

    try:
        await asyncio.wait_for(asyncio.gather(pools(), indexes()), timeout=config.WARMUP_TIMEOUT_S)
    except Exception:  # startup must survive any warm-up failure; logged with traceback
        log.exception(
            "warm-up FAILED after %.1f s: the first request will pay process spawn", time.perf_counter() - t0
        )
    dt = time.perf_counter() - t0
    log.info("warm-up done in %.2f s", dt)
    return dt


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await warm_up()
    yield


app = FastAPI(title="SPHEREx Blink Tool", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


def _err(status: int, msg: str, **kw: Any) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status, **kw)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    e = exc.errors()[0]
    where = ".".join(str(x) for x in e["loc"] if x != "query")
    return _err(422, f"invalid parameter {where}: {e['msg']}")


def _validate(
    ra: float, dec: float, size: float, det: int, mode: str
) -> tuple[float, float, float, str | None]:
    """(ra normalised to [0,360), dec, canonical size, error message or None)."""
    if not (math.isfinite(ra) and math.isfinite(dec) and math.isfinite(size)):
        return ra, dec, size, "ra, dec and size must be finite numbers"
    if not -90.0 <= dec <= 90.0:
        return ra, dec, size, f"dec must be in [-90, 90], got {dec}"
    canon = next((s for s in config.FOV_CHOICES_DEG if math.isclose(s, size, abs_tol=1e-9)), None)
    if canon is None:
        return ra, dec, size, f"size must be one of {list(config.FOV_CHOICES_DEG)}, got {size}"
    if det not in config.DETECTORS:
        return ra, dec, canon, f"det must be one of {list(config.DETECTORS)}, got {det}"
    if det not in index.INDEXED_DETS:
        return ra, dec, canon, f"det {det} is not indexed yet (available: {list(index.INDEXED_DETS)})"
    if mode not in MODES:
        return ra, dec, canon, f"mode must be one of {list(MODES)}, got {mode!r}"
    return ra % 360.0, dec, canon, None


@app.get("/api/sequence")
async def api_sequence(
    ra: float, dec: float, size: float = config.DEFAULT_FOV_DEG, det: int = 6, mode: str = "all"
) -> Any:
    t0 = time.perf_counter()
    ra, dec, size, msg = _validate(ra, dec, size, det, mode)
    if msg:
        return _err(422, msg)
    grid = make_grid(ra, dec, size)
    path = cache.l2_get(cache.stack_key(grid, det, mode, index.data_version()))
    if path is not None:
        log.info("sequence ra=%.5f dec=%.5f size=%s det=%d mode=%s source=cache", ra, dec, size, det, mode)
        return FileResponse(
            path,
            media_type=NDJSON,
            headers={
                "Content-Encoding": "gzip",
                "Cache-Control": f"public, max-age={config.L2_MAX_AGE_S}",
                "Server-Timing": f"l2;dur={(time.perf_counter() - t0) * 1000:.0f}",
            },
        )
    return StreamingResponse(
        sequence_stream(ra, dec, size, det, mode, Timer()),
        media_type=NDJSON,
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/counts")
async def api_counts(ra: float, dec: float, size: float = config.DEFAULT_FOV_DEG, det: int = 6) -> Any:
    """How many frames each mode will stream, from the footprint index alone (no S3 reads, ms).

    `n_exposures` is the header's n_expected in mode all/lmdiff; `n_passes` is n_expected in mode pass.
    The page calls this at load so both counts (and the "k of N" counter) are known before frames arrive."""
    t0 = time.perf_counter()
    ra, dec, size, msg = _validate(ra, dec, size, det, "all")
    if msg:
        return _err(422, msg)
    try:
        refs = sorted(await index.find_images(ra, dec, det), key=lambda r: r.t_mjd)
    except FileNotFoundError as e:
        log.error("counts: footprint index missing for det %d: %s", det, e)
        return _err(503, f"index for detector {det} is not available")
    passes = group_refs_by_pass(refs)
    return JSONResponse(
        {"det": det, "n_exposures": len(refs), "n_passes": (max(passes) + 1) if passes else 0},
        headers={
            "Cache-Control": f"public, max-age={config.L2_MAX_AGE_S}",
            "Server-Timing": f"index;dur={(time.perf_counter() - t0) * 1000:.0f}",
        },
    )


def _json_route(res: JsonResult, t0: float, name: str) -> Response:
    headers = {"Server-Timing": f"{name};dur={(time.perf_counter() - t0) * 1000:.0f}"}
    # Only complete results may be browser-cached (same short max-age as L2 stacks); empty/error ones never.
    headers["Cache-Control"] = f"public, max-age={config.L2_MAX_AGE_S}" if res.cacheable else "no-store"
    return Response(res.body, status_code=res.status, media_type="application/json", headers=headers)


@app.get("/api/motion")
async def api_motion(ra: float, dec: float, size: float = config.DEFAULT_FOV_DEG, det: int = 6) -> Any:
    """C11 motion JSON; cached in L2 as motion/{stack_key}.json."""
    t0 = time.perf_counter()
    ra, dec, size, msg = _validate(ra, dec, size, det, "all")
    if msg:
        return _err(422, msg)
    return _json_route(await motion_result(ra, dec, size, det), t0, "motion")


@app.get("/api/lightcurve")
async def api_lightcurve(ra: float, dec: float, size: float = config.DEFAULT_FOV_DEG, det: int = 6) -> Any:
    """E4 lambda-matched light curve at the request centre; 501 until motion.lambda_matched_lightcurve exists."""
    t0 = time.perf_counter()
    ra, dec, size, msg = _validate(ra, dec, size, det, "all")
    if msg:
        return _err(422, msg)
    return _json_route(await lightcurve_result(ra, dec, size, det), t0, "lightcurve")


@app.get("/api/spectrum")
async def api_spectrum(
    ra: float,
    dec: float,
    size: float = config.DEFAULT_FOV_DEG,
    pmra: float = 0.0,
    pmdec: float = 0.0,
) -> Any:
    """F3 forced-photometry spectrum over all 6 detectors (C6); cached in L2 as spectrum/{key}.json."""
    t0 = time.perf_counter()
    ra, dec, size, msg = _validate(ra, dec, size, config.DETECTORS[-1], "all")
    if msg:
        return _err(422, msg)
    if not (math.isfinite(pmra) and math.isfinite(pmdec)):
        return _err(422, "pmra and pmdec must be finite numbers (mas/yr)")
    return _json_route(await spectrum_result(ra, dec, size, pmra, pmdec), t0, "spectrum")


@app.get("/api/resolve")
async def api_resolve(q: str) -> JSONResponse:
    t0 = time.perf_counter()
    info = await asyncio.to_thread(resolve.resolve, q)
    timing = {"Server-Timing": f"resolve;dur={(time.perf_counter() - t0) * 1000:.0f}"}
    if info is None:
        return _err(404, "not found", headers=timing)
    return JSONResponse(
        info, headers={**timing, "Cache-Control": f"public, max-age={config.RESOLVE_MAX_AGE_S}"}
    )


@app.get("/api/health")
async def api_health() -> JSONResponse:
    try:
        idx = await asyncio.to_thread(index._load, config.S3_INDEX_PATH)
    except FileNotFoundError:
        log.error("health: footprint index missing at %s", config.S3_INDEX_PATH)
        return JSONResponse(
            {
                "ok": False,
                "error": f"index missing: {config.S3_INDEX_PATH.name}",
                "data_version": index.data_version(),
            },
            status_code=503,
        )
    return JSONResponse({"ok": True, "data_version": index.data_version(), "index_rows": len(idx.rows)})


async def load_frames_capped(grid, det: int, max_frames: int):
    """Like load_frames, but reads at most max_frames exposures, spread evenly in time (deep fields have hundreds)."""
    import contextlib

    import numpy as np

    from backend import index
    from backend.sequence import Loaded, fetch_interleaved, group_refs_by_pass

    refs = sorted(await index.find_images(grid.ra, grid.dec, det), key=lambda r: r.t_mjd)
    if len(refs) > max_frames:
        keep = np.unique(np.linspace(0, len(refs) - 1, max_frames).round().astype(int))
        refs = [refs[i] for i in keep]
    pass_of = group_refs_by_pass(refs)
    got, failed = {}, []
    if refs:
        async with contextlib.aclosing(fetch_interleaved(refs, grid, pass_of)) as it:
            async for i, frame, err in it:
                if err is not None or frame is None:
                    failed.append((refs[i].obs_id, err or "no frame"))
                else:
                    frame.pass_idx = pass_of[i]
                    got[i] = frame
    return Loaded(refs, [got[i] for i in sorted(got)], failed)


@app.get("/api/cube")
async def api_cube(
    ra: float,
    dec: float,
    size: float = config.DEFAULT_FOV_DEG,
    det: int = config.DETECTORS[-1],
    max_frames: int = 0,
    dtype: str = "f4",
) -> Response:
    """Fast binary float32 cube endpoint for Draconid Drift UI over SSH tunnel or local backend."""
    import io
    import numpy as np

    t0 = time.perf_counter()
    if not (math.isfinite(ra) and math.isfinite(dec) and math.isfinite(size)):
        return _err(422, "ra, dec and size must be finite numbers")
    if not -90.0 <= dec <= 90.0:
        return _err(422, f"dec must be in [-90, 90], got {dec}")
    if not 0.005 <= size <= config.MAX_FOV_DEG:
        return _err(422, f"size must be in [0.005, {config.MAX_FOV_DEG}], got {size}")
    if det not in config.DETECTORS:
        return _err(422, f"det must be one of {list(config.DETECTORS)}, got {det}")
    ra = ra % 360.0
    grid = make_grid(ra, dec, size)
    try:
        loaded = await (load_frames_capped(grid, det, max_frames) if max_frames > 0 else load_frames(grid, det))
    except FileNotFoundError as e:
        return _err(503, f"footprint index missing ({e.filename})")
    refs = loaded.refs
    frames = loaded.frames
    n_failed = len(loaded.failed)
    if not frames:
        return _err(404, "no usable frames at this position")
    cube_arr = np.stack([f.data for f in frames]).astype(np.float32)
    var_arr = np.stack([
        f.var if f.var is not None else np.ones_like(f.data)
        for f in frames
    ]).astype(np.float32)
    mjd_arr = np.array([f.ref.t_mjd for f in frames], dtype=np.float64)
    wl_arr = np.array([f.lambda_um for f in frames], dtype=np.float64)
    pass_arr = np.array([f.pass_idx for f in frames], dtype=np.int32)
    if dtype == "f2":                     # half the bytes on the wire; SPHEREx counts fit float16 comfortably
        cube_arr, var_arr = cube_arr.astype(np.float16), var_arr.astype(np.float16)
    buf = io.BytesIO()
    np.savez(
        buf,
        cube=cube_arr,
        var=var_arr,
        mjd=mjd_arr,
        wl=wl_arr,
        pass_idx=pass_arr,
        n_found=len(refs),
        n_failed=n_failed,
        ra=grid.ra,
        dec=grid.dec,
        size_deg=grid.size_deg,
        pixscale_arcsec=grid.pixscale_arcsec,
    )
    dur_ms = (time.perf_counter() - t0) * 1000
    return Response(
        content=buf.getvalue(),
        media_type="application/octet-stream",
        headers={"Server-Timing": f"cube;dur={dur_ms:.0f}"},
    )


# Static frontend: mounted LAST so /api/* wins.
_static_dir = config.ROOT / "frontend" if (config.ROOT / "frontend").is_dir() else config.ROOT / "web"
if not (_static_dir / "index.html").is_file() and (_static_dir / "sky_map.html").is_file():
    @app.get("/", include_in_schema=False)
    async def root_html() -> FileResponse:
        return FileResponse(_static_dir / "sky_map.html", media_type="text/html")

if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="frontend")
