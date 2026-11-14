"""Orchestration: index -> fetch -> encode, as a C5 NDJSON stream (Task 04 A3 + SPRINT S2).

Differences from the task file, all from SPRINT.md:
- an early `status` line is sent first (before `header`); clients ignore unknown types except to show `msg`;
- no live `zscale` lines: the final stack-wide zscale goes in `done.zscale` and in the stored L2 header;
- image discovery is the local S3 footprint index (index.find_images), not TAP.
"""

import asyncio
import contextlib
import dataclasses
import importlib
import logging
import math
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import numpy as np
import orjson

from backend import cache, config, fetch, index, process
from backend.encode import done_line, error_line, frame_line, header_line
from backend.models import Frame, Grid, ImageRef, make_grid
from backend.process import zscale_limits
from backend.timing import Timer

log = logging.getLogger("sequence")

MODES = ("all", "pass", "lmdiff")
# Not in config.py (Track A owns it): ask to move.
STATUS_MSG = "finding images"
STATUS_MSG_FRAMES = "fetching frames"
LIGHTCURVE_UNAVAILABLE = "lightcurve not available"
# F3 spectrum (Task 06): apertures are config.SPECTRUM_* (same radii as E4).
# process.py never rescales the IMAGE HDU (it only subtracts ZODI and a median), so the unit stays the one in
# the IMAGE header (BUNIT = MJy/sr, notes/recon.md); Frame does not carry BUNIT. `flux` is an aperture SUM
# (background-subtracted), so the honest unit is surface brightness x pixels, not MJy/sr.
SPECTRUM_UNIT = "MJy/sr x px (aperture sum)"
MAS_PER_ARCSEC = 1000.0
WARM_MODULES = ("backend.fetch", "backend.process")  # imported in every pool worker by warm_worker


def group_refs_by_pass(refs: list[ImageRef], gap_days: float = config.PASS_GAP_DAYS) -> list[int]:
    """Pass index per ref, for refs sorted by t_mjd. Same rule as process.group_by_pass (gap > gap_days)."""
    out: list[int] = []
    p = 0
    for i, r in enumerate(refs):
        if i > 0 and r.t_mjd - refs[i - 1].t_mjd > gap_days:
            p += 1
        out.append(p)
    return out


def interleave_order(pass_of: list[int]) -> list[int]:
    """Indices into the t_mjd-sorted refs, round-robin across passes: p0f0, p1f0, p2f0, p0f1, ...

    Why: with time-ordered submission the last pass is fetched last, so a pass blink cannot start until the
    whole stack is nearly done. Interleaved, every pass has its first inputs after ~n_passes fetches.
    """
    n_passes = pass_of[-1] + 1 if pass_of else 0
    by_pass: list[list[int]] = [[] for _ in range(n_passes)]
    for i, p in enumerate(pass_of):
        by_pass[p].append(i)
    depth = max((len(g) for g in by_pass), default=0)
    return [g[k] for k in range(depth) for g in by_pass if k < len(g)]


async def fetch_interleaved(
    refs: list[ImageRef], grid: Grid, pass_of: list[int]
) -> AsyncIterator[tuple[int, Frame | None, str | None]]:
    """fetch.get_frames over the round-robin order; yields (i, frame, err) with `i` indexing `refs` (the
    t_mjd-sorted list, C5 rule), not the reordered submission list get_frames sees."""
    order = interleave_order(pass_of)
    async with contextlib.aclosing(fetch.get_frames([refs[k] for k in order], grid)) as it:
        async for j, frame, err in it:
            yield order[j], frame, err


def warm_worker(modules: tuple[str, ...] = WARM_MODULES, s3: bool = False) -> int:
    """No-op for a pool worker: import what the real jobs import (astropy, reproject, fsspec) so the first
    request does not pay it. s3=True also builds the (lazy, no network) anonymous S3 filesystem. Returns the pid."""
    for m in modules:
        importlib.import_module(m)
    if s3:
        import fsspec

        fsspec.filesystem("s3", anon=True)
    return os.getpid()


def _status_line(msg: str) -> bytes:
    return orjson.dumps({"type": "status", "msg": msg}) + b"\n"


def _done_with_zscale(
    n_frames: int, n_failed: int, elapsed_ms: int, zscale: tuple[float, float] | None
) -> bytes:
    """encode.done_line has no zscale argument (and its signature is frozen), so extend its output here."""
    obj = orjson.loads(done_line(n_frames, n_failed, elapsed_ms))
    obj["zscale"] = None if zscale is None else [float(zscale[0]), float(zscale[1])]
    return orjson.dumps(obj) + b"\n"


def _pass_frame(p: int, grp: list[Frame]) -> Frame:
    """Inverse-variance co-add of one pass (pass membership comes from the refs, not re-derived from t_mjd).

    Same output as process.coadd_by_pass for a complete pass; that function would re-split a pass whose
    gap-bridging frame failed to load, giving wrong pass indices.
    """
    data, var = process.coadd(grp)
    flags = np.zeros_like(grp[0].flags, dtype=np.uint32)
    for f in grp:
        flags |= f.flags.astype(np.uint32)
    det = grp[0].ref.detector
    ref = dataclasses.replace(
        grp[0].ref,
        obs_id=f"pass{p}_D{det}",
        exposure_id=f"pass{p}",
        t_mjd=float(np.mean([f.ref.t_mjd for f in grp])),
    )
    return Frame(ref, data, var, flags, float(np.mean([f.lambda_um for f in grp])), pass_idx=p)


async def sequence_stream(
    ra: float, dec: float, size_deg: float, det: int, mode: str, timer: Timer
) -> AsyncIterator[bytes]:
    """Yields C5 NDJSON lines (bytes). Always computes live; L2 hits are served by the route.

    Fills L2 (stack_key) when it finishes with < config.MAX_FAIL_FRAC failed inputs. Index/fetch problems never
    raise: they become `error` lines + a log record.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if mode == "lmdiff":
        async for line in _lmdiff_stream(ra, dec, size_deg, det, timer):
            yield line
        return
    t0 = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    grid = make_grid(ra, dec, size_deg)
    dv = index.data_version()
    n_frames = n_failed = n_refs = 0
    cached = False
    try:
        yield _status_line(STATUS_MSG)
        try:
            with timer.stage("index"):
                refs = sorted(await index.find_images(grid.ra, grid.dec, det), key=lambda r: r.t_mjd)
        except (FileNotFoundError, ValueError) as e:
            log.error(
                "index lookup failed ra=%.5f dec=%.5f det=%d: %s: %s", ra, dec, det, type(e).__name__, e
            )
            yield header_line(grid, det, mode, 0, dv, "live", None)
            yield error_line("index", f"{type(e).__name__}: {e}")
            n_failed = 1
            yield _done_with_zscale(0, 1, elapsed_ms(), None)
            return

        n_refs = len(refs)
        pass_of = group_refs_by_pass(refs)
        n_passes = pass_of[-1] + 1 if pass_of else 0
        n_expected = n_refs if mode == "all" else n_passes
        yield header_line(grid, det, mode, n_expected, dv, "live", None)
        if not refs:
            log.info("no coverage ra=%.5f dec=%.5f det=%d", ra, dec, det)
            yield _done_with_zscale(0, 0, elapsed_ms(), None)
            return

        remaining = [pass_of.count(p) for p in range(n_passes)]
        collected: dict[int, list[Frame]] = {p: [] for p in range(n_passes)}
        stored: dict[int, bytes] = {}  # frame lines by i, written to L2 sorted
        errors: list[bytes] = []
        arrays: list[np.ndarray] = []  # for the stack-wide zscale

        with timer.stage("stream"):
            async with contextlib.aclosing(fetch_interleaved(refs, grid, pass_of)) as it:
                async for i, frame, err in it:
                    p = pass_of[i]
                    remaining[p] -= 1
                    out: list[tuple[int, bytes]] = []
                    got_input = False
                    if err is not None or frame is None:
                        n_failed += 1
                        line = error_line(refs[i].obs_id, err or "no frame")
                        errors.append(line)
                        yield line
                    else:
                        got_input = True
                        frame.pass_idx = p
                        if mode == "all":
                            out.append((i, frame_line(i, frame, include_flags=True)))
                            arrays.append(frame.data)
                        else:
                            collected[p].append(frame)
                    if mode == "pass" and remaining[p] == 0 and collected[p]:
                        grp = collected.pop(p)
                        pf = _pass_frame(p, grp)
                        out.append((p, frame_line(p, pf, include_flags=False, n_coadd=len(grp))))
                        arrays.append(pf.data)
                    elif (
                        mode == "pass"
                        and got_input
                        and remaining[p] > 0
                        and len(collected[p]) == config.PASS_PREVIEW_FRAMES
                    ):
                        # C5 preview: same `i` as the final co-add, which will replace it. Not stored, not
                        # counted in n_frames, not in the zscale sample.
                        grp = collected[p]
                        pf = _pass_frame(p, grp)
                        yield _as_preview(frame_line(p, pf, include_flags=False, n_coadd=len(grp)))
                    for k, line in out:
                        n_frames += 1
                        stored[k] = line
                        yield line

        try:
            zscale = zscale_limits(arrays) if arrays else None
        except ValueError:
            log.warning("no finite pixels for zscale ra=%.5f dec=%.5f det=%d mode=%s", ra, dec, det, mode)
            zscale = None
        done = _done_with_zscale(n_frames, n_failed, elapsed_ms(), zscale)

        # L2 BEFORE the final yield: a client that disconnects on `done` must not cancel the write.
        if n_frames > 0 and n_failed / n_refs < config.MAX_FAIL_FRAC:
            key = cache.stack_key(grid, det, mode, dv)
            lines = [header_line(grid, det, mode, n_expected, dv, "cache", zscale)]
            lines += [stored[k] for k in sorted(stored)] + errors + [done]
            try:
                await _write_l2(key, lines)
                cached = True
            except OSError as e:  # disk full / permissions: the stream itself is still good
                log.error("L2 write failed key=%s: %s: %s", key, type(e).__name__, e)
        else:
            log.warning(
                "not caching ra=%.5f dec=%.5f det=%d mode=%s: %d/%d inputs failed, %d frames",
                ra, dec, det, mode, n_failed, n_refs, n_frames,
            )  # fmt: skip
        yield done
    finally:
        log.info(
            "sequence ra=%.5f dec=%.5f size=%s det=%d mode=%s source=live n=%d failed=%d cached=%s ms=%d %s",
            ra, dec, size_deg, det, mode, n_frames, n_failed, cached, elapsed_ms(), timer.header(),
        )  # fmt: skip


def _as_preview(line: bytes) -> bytes:
    """Add `"preview":true` to an encoded frame line (encode.frame_line has no such argument)."""
    if not line.endswith(b"}\n"):
        raise ValueError("not a single JSON object line")
    return line[:-2] + b',"preview":true}\n'


async def _write_l2(key: str, lines: list[bytes]) -> None:
    def write() -> None:
        w = cache.l2_put_stream(key)
        try:
            for ln in lines:
                w.write(ln)
            w.commit()
        except BaseException:
            w.abort()
            raise

    await asyncio.to_thread(write)


# ------------------------------------------------------------------ shared frame loading (lmdiff, motion, lightcurve)
@dataclass
class Loaded:
    refs: list[ImageRef]  # sorted by t_mjd
    frames: list[Frame]  # successfully loaded, in refs order, pass_idx set from the refs
    failed: list[tuple[str, str]] = field(default_factory=list)  # (obs_id, reason)


async def load_frames(grid: Grid, det: int) -> Loaded:
    """All frames of the stack (L1-cached by fetch.get_frames). Raises FileNotFoundError/ValueError from the index."""
    refs = sorted(await index.find_images(grid.ra, grid.dec, det), key=lambda r: r.t_mjd)
    pass_of = group_refs_by_pass(refs)
    got: dict[int, Frame] = {}
    failed: list[tuple[str, str]] = []
    if refs:
        async with contextlib.aclosing(fetch_interleaved(refs, grid, pass_of)) as it:
            async for i, frame, err in it:
                if err is not None or frame is None:
                    failed.append((refs[i].obs_id, err or "no frame"))
                else:
                    frame.pass_idx = pass_of[i]
                    got[i] = frame
    return Loaded(refs, [got[i] for i in sorted(got)], failed)


def _too_many_failed(loaded: Loaded) -> bool:
    return bool(loaded.refs) and len(loaded.failed) / len(loaded.refs) >= config.MAX_FAIL_FRAC


# ------------------------------------------------------------------ mode "lmdiff" (Task 08 M6)
async def _lmdiff_stream(
    ra: float, dec: float, size_deg: float, det: int, timer: Timer
) -> AsyncIterator[bytes]:
    """Wavelength-matched residual frames as C5 frame lines (kind "single"; the diff stretch is client-side).

    Not streamable: the templates need every frame, so the header is sent AFTER the residuals exist (status
    lines first). That lets n_expected be exact: frames with no wavelength match in another pass are dropped.
    `i` is the index in t_mjd order among the emitted frames. Header/done zscale are null (C9: diffs use a
    symmetric client-side stretch).
    """
    from backend import motion  # at call time: Track D owns it

    t0 = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    grid = make_grid(ra, dec, size_deg)
    dv = index.data_version()
    n_frames = n_failed = 0
    cached = False
    try:
        yield _status_line(STATUS_MSG)
        try:
            with timer.stage("fetch"):
                loaded = await load_frames(grid, det)
        except (FileNotFoundError, ValueError) as e:
            log.error(
                "index lookup failed ra=%.5f dec=%.5f det=%d: %s: %s", ra, dec, det, type(e).__name__, e
            )
            yield header_line(grid, det, "lmdiff", 0, dv, "live", None)
            yield error_line("index", f"{type(e).__name__}: {e}")
            n_failed = 1
            yield _done_with_zscale(0, 1, elapsed_ms(), None)
            return
        n_failed = len(loaded.failed)
        yield _status_line(STATUS_MSG_FRAMES)
        with timer.stage("process"):
            resid = await asyncio.to_thread(motion.lambda_matched_residuals, loaded.frames)
        if loaded.frames and not resid:
            log.warning(
                "lmdiff: no frame has a wavelength match in another pass ra=%.5f dec=%.5f det=%d (%d frames)",
                ra, dec, det, len(loaded.frames),
            )  # fmt: skip
        yield header_line(grid, det, "lmdiff", len(resid), dv, "live", None)
        errors = [error_line(obs_id, reason) for obs_id, reason in loaded.failed]
        for ln in errors:
            yield ln
        lines = [frame_line(k, f, include_flags=True) for k, f in enumerate(resid)]
        for ln in lines:
            n_frames += 1
            yield ln
        done = _done_with_zscale(n_frames, n_failed, elapsed_ms(), None)
        if n_frames > 0 and not _too_many_failed(loaded):
            key = cache.stack_key(grid, det, "lmdiff", dv)
            try:
                await _write_l2(
                    key,
                    [header_line(grid, det, "lmdiff", len(resid), dv, "cache", None), *lines, *errors, done],
                )
                cached = True
            except OSError as e:
                log.error("L2 write failed key=%s: %s: %s", key, type(e).__name__, e)
        elif loaded.refs:
            log.warning(
                "not caching lmdiff ra=%.5f dec=%.5f det=%d: %d/%d inputs failed, %d frames",
                ra, dec, det, n_failed, len(loaded.refs), n_frames,
            )  # fmt: skip
        yield done
    finally:
        log.info(
            "sequence ra=%.5f dec=%.5f size=%s det=%d mode=lmdiff source=live n=%d failed=%d cached=%s ms=%d %s",
            ra, dec, size_deg, det, n_frames, n_failed, cached, elapsed_ms(), timer.header(),
        )  # fmt: skip


# ------------------------------------------------------------------ JSON endpoints: /api/motion, /api/lightcurve
@dataclass
class JsonResult:
    body: bytes  # JSON, ready to send
    status: int = 200
    cacheable: bool = False  # a complete result (served from, or written to, the JSON cache)
    source: str = "live"  # "cache" | "live"
    n_frames: int = 0
    n_failed: int = 0
    error: str | None = None  # set when the result is empty/failed; the body also carries it


def _dumps(obj: object) -> bytes:
    # NaN/inf become null (the stdlib json would emit invalid JSON or raise); numpy scalars are accepted.
    return orjson.dumps(obj, option=orjson.OPT_SERIALIZE_NUMPY)


def _empty_motion(grid: Grid, det: int, error: str, passes: list[dict] | None = None) -> dict:
    """C11 shape with no movers + an `error` string (C11 defines no error shape; see report)."""
    return {
        "v": 1,
        "ra": grid.ra,
        "dec": grid.dec,
        "size_deg": grid.size_deg,
        "det": det,
        "npix": grid.npix,
        "t0_yr": None,
        "passes": passes or [],
        "registration_ok": False,
        "grid": None,
        "elapsed_ms": {"stack": 0, "search": 0, "measure": 0},
        "movers": [],
        "error": error,
    }


def _compute_motion(grid: Grid, det: int, frames: list[Frame]) -> dict:
    """The M5-validated chain: pass_stack -> refine_registration -> motion_search -> measure_movers."""
    from backend import motion

    t = time.perf_counter()
    passes = motion.refine_registration(motion.pass_stack(frames))
    t_stack = time.perf_counter()
    mmap = motion.motion_search(passes)
    t_search = time.perf_counter()
    movers = motion.measure_movers(passes, mmap, grid)
    t_measure = time.perf_counter()
    ref_i = max(range(len(passes)), key=lambda i: (passes[i].n, -i))  # same rule as refine_registration
    # refine_registration falls back to dx = dy = 0 (with a warning) when too few stars match; a median
    # offset of exactly 0 is otherwise impossible, so that is the failure signature.
    reg_ok = all(p.dx != 0.0 or p.dy != 0.0 for i, p in enumerate(passes) if i != ref_i)
    vg = mmap.v_grid
    return {
        "v": 1,
        "ra": grid.ra,
        "dec": grid.dec,
        "size_deg": grid.size_deg,
        "det": det,
        "npix": grid.npix,
        "t0_yr": mmap.t0,
        "passes": [{"t_yr": p.t_yr, "n": p.n, "dx": p.dx, "dy": p.dy, "sigma": p.sigma} for p in passes],
        "registration_ok": reg_ok,
        "grid": {
            "vmax": config.VMAX_PX_PER_YR,  # the search limit; the arange grid may overshoot it by < step
            "step": float(vg[1] - vg[0]),  # px/yr
            "n": int(vg.size),  # per axis
        },
        "elapsed_ms": {
            "stack": round((t_stack - t) * 1000),
            "search": round((t_search - t_stack) * 1000),
            "measure": round((t_measure - t_search) * 1000),
        },
        "movers": [
            {
                "x0": m.x0,
                "y0": m.y0,
                "ra0": m.ra0,
                "dec0": m.dec0,
                "pmra_masyr": m.pmra_masyr,
                "pmdec_masyr": m.pmdec_masyr,
                "epmra_masyr": m.epmra_masyr,
                "epmdec_masyr": m.epmdec_masyr,
                "pm_total_arcsec_yr": m.pm_total_arcsec_yr,
                "snr_stack": m.snr_stack,
                "motion_sig": m.motion_sig,
                "n_pass_used": m.n_pass_used,
                "track": [[a, b, c] for a, b, c in m.track],
            }
            for m in movers
        ],
    }


async def _json_result(
    kind: str,
    ra: float,
    dec: float,
    size_deg: float,
    det: int,
    refresh: bool,
    compute,  # async (grid, loaded) -> (payload dict, error str | None)
    unavailable: JsonResult | None = None,
) -> JsonResult:
    """Common flow: L2 JSON hit -> else load frames -> compute -> cache complete results only."""
    grid = make_grid(ra, dec, size_deg)
    key = cache.json_key(kind, grid, det, index.data_version())
    if not refresh:
        hit = await asyncio.to_thread(cache.json_get, key)
        if hit is not None:
            log.info("%s ra=%.5f dec=%.5f size=%s det=%d source=cache", kind, ra, dec, size_deg, det)
            return JsonResult(hit, cacheable=True, source="cache")
    if unavailable is not None:
        return unavailable
    t0 = time.perf_counter()
    try:
        loaded = await load_frames(grid, det)
    except (FileNotFoundError, ValueError) as e:
        log.error(
            "%s: index lookup failed ra=%.5f dec=%.5f det=%d: %s: %s", kind, ra, dec, det, type(e).__name__, e
        )
        msg = f"index lookup failed: {type(e).__name__}: {e}"
        return JsonResult(_dumps({"error": msg}), status=503, error=msg)
    res = JsonResult(b"", n_frames=len(loaded.frames), n_failed=len(loaded.failed))
    try:
        payload, error = await compute(grid, loaded)
    except Exception as e:  # reported as a JSON 500 + log with traceback, never silently
        log.exception("%s failed ra=%.5f dec=%.5f det=%d", kind, ra, dec, det)
        msg = f"{kind} failed: {type(e).__name__}: {e}"
        res.body, res.status, res.error = _dumps({"error": msg}), 500, msg
        return res
    res.body, res.error = _dumps(payload), error
    if error is None and not _too_many_failed(loaded):
        try:
            await asyncio.to_thread(cache.json_put, key, res.body)
            res.cacheable = True
        except OSError as e:
            log.error("JSON cache write failed key=%s: %s: %s", key, type(e).__name__, e)
    elif error is None:
        log.warning(
            "not caching %s ra=%.5f dec=%.5f det=%d: %d/%d inputs failed",
            kind, ra, dec, det, len(loaded.failed), len(loaded.refs),
        )  # fmt: skip
    log.info(
        "%s ra=%.5f dec=%.5f size=%s det=%d source=live frames=%d failed=%d error=%s ms=%d",
        kind, ra, dec, size_deg, det, res.n_frames, res.n_failed, error, (time.perf_counter() - t0) * 1000,
    )  # fmt: skip
    return res


async def motion_result(
    ra: float, dec: float, size_deg: float, det: int, refresh: bool = False
) -> JsonResult:
    """C11 JSON from the same frames as mode=all. < 2 passes / no frames -> 200 with the C11 shape, movers
    [] and an `error` string (not cached). Index failure -> 503, unexpected exception -> 500, both JSON."""

    async def compute(grid: Grid, loaded: Loaded) -> tuple[dict, str | None]:
        n_pass = len({f.pass_idx for f in loaded.frames})
        if n_pass < 2:
            msg = (
                f"need >=2 passes, found {n_pass} ({len(loaded.frames)} frames, {len(loaded.failed)} failed)"
            )
            log.warning("motion: %s ra=%.5f dec=%.5f det=%d", msg, grid.ra, grid.dec, det)
            return _empty_motion(grid, det, msg), msg
        try:
            return await asyncio.to_thread(_compute_motion, grid, det, loaded.frames), None
        except ValueError as e:  # documented failure of pass_stack / motion_search (no coverage, flat image)
            log.warning("motion: %s ra=%.5f dec=%.5f det=%d", e, grid.ra, grid.dec, det)
            return _empty_motion(grid, det, str(e)), str(e)

    return await _json_result("motion", ra, dec, size_deg, det, refresh, compute)


async def lightcurve_result(
    ra: float, dec: float, size_deg: float, det: int, refresh: bool = False
) -> JsonResult:
    """E4: {"points": [...], "summary": {...}} for the request centre. 501 when Track D's functions are missing."""
    from backend import motion

    unavailable = None
    if not (hasattr(motion, "lambda_matched_lightcurve") and hasattr(motion, "lightcurve_summary")):
        log.error(
            "lightcurve requested but backend.motion lacks lambda_matched_lightcurve/lightcurve_summary"
        )
        unavailable = JsonResult(
            _dumps({"error": LIGHTCURVE_UNAVAILABLE}), status=501, error=LIGHTCURVE_UNAVAILABLE
        )

    async def compute(grid: Grid, loaded: Loaded) -> tuple[dict, str | None]:
        empty = {"points": [], "summary": {"per_pass_dmag": [], "chi2_red_constant": None}}
        if not loaded.frames:
            return {**empty, "error": "no frames"}, "no frames"
        # internal pixel coords (C4: row 0 = south), 0-based: the request centre is the grid centre pixel
        x, y = grid.wcs().world_to_pixel_values(grid.ra, grid.dec)
        points = await asyncio.to_thread(motion.lambda_matched_lightcurve, loaded.frames, float(x), float(y))
        if not points:
            msg = "no wavelength-matched points (need >=2 passes with matching lambda)"
            return {**empty, "error": msg}, msg
        return {"points": points, "summary": motion.lightcurve_summary(points)}, None

    return await _json_result("lightcurve", ra, dec, size_deg, det, refresh, compute, unavailable)


# ------------------------------------------------------------------ /api/spectrum (Task 06 F3, contract C6)
def aperture_position(grid: Grid, t_mjd: float, pmra_masyr: float, pmdec_masyr: float) -> tuple[float, float]:
    """Aperture centre (x, y), 0-based internal pixels (row 0 = south, +x = west), at time t_mjd.

    The grid centre is the source position at config.SPHEREX_MID_EPOCH; pmra includes cos(dec) (tasks/08-motion.md
    Conventions): vx = -pmra / pixscale (east-positive pmra moves the source to -x), vy = +pmdec / pixscale.
    """
    from backend import motion

    c = (grid.npix - 1) / 2.0
    dt = motion.mjd_to_yr(t_mjd) - config.SPHEREX_MID_EPOCH
    mas_per_px = grid.pixscale_arcsec * MAS_PER_ARCSEC
    return c - pmra_masyr / mas_per_px * dt, c + pmdec_masyr / mas_per_px * dt


def _spectrum_points(grid: Grid, loaded: Loaded, pmra: float, pmdec: float) -> tuple[list[dict], int]:
    """(points, n_nonfinite). Blocking numpy; call from a thread."""
    from backend import photometry

    pass_of = dict(zip((r.obs_id for r in loaded.refs), group_refs_by_pass(loaded.refs)))
    points: list[dict] = []
    n_bad = 0
    for f in loaded.frames:
        x, y = aperture_position(grid, f.ref.t_mjd, pmra, pmdec)
        flux, err = photometry.aperture_flux(
            f.data,
            f.var,
            x,
            y,
            config.SPECTRUM_APERTURE_R,
            config.SPECTRUM_ANNULUS_R_IN,
            config.SPECTRUM_ANNULUS_R_OUT,
        )
        lam = float(f.lambda_um)
        # err <= 0 means the variance map was NaN/zero over the whole aperture: a flux without an error bar
        if not (math.isfinite(flux) and math.isfinite(err) and err > 0 and math.isfinite(lam)):
            log.warning("spectrum: non-finite photometry obs_id=%s x=%.2f y=%.2f", f.ref.obs_id, x, y)
            n_bad += 1
            continue
        iy = min(max(round(y), 0), f.flags.shape[0] - 1)
        ix = min(max(round(x), 0), f.flags.shape[1] - 1)
        points.append(
            {
                "lambda_um": lam,
                "flux": flux,
                "err": err,
                "t_mjd": float(f.ref.t_mjd),
                "det": f.ref.detector,
                "pass": pass_of.get(f.ref.obs_id, f.pass_idx),
                "flags": int(f.flags[iy, ix]),
            }
        )
    return points, n_bad


async def spectrum_result(
    ra: float,
    dec: float,
    size_deg: float,
    pmra: float = 0.0,
    pmdec: float = 0.0,
    refresh: bool = False,
) -> JsonResult:
    """F3: forced aperture photometry at the grid centre on all 6 detectors, sorted by wavelength.

    A detector whose index file is missing goes into `missing_dets` (not an error). Such a partial result is
    not cached (the index may be built later), nor is one with >= config.MAX_FAIL_FRAC failed frames. All six
    detectors missing -> 503. No point at all -> 200 with `points: []` and `error`.
    """
    grid = make_grid(ra, dec, size_deg)
    pmra, pmdec = cache.round_pm(pmra), cache.round_pm(pmdec)  # computed with what the cache key records
    key = cache.spectrum_key(grid, pmra, pmdec, index.data_version())
    if not refresh:
        hit = await asyncio.to_thread(cache.json_get, key)
        if hit is not None:
            log.info("spectrum ra=%.5f dec=%.5f size=%s source=cache", ra, dec, size_deg)
            return JsonResult(hit, cacheable=True, source="cache")
    t0 = time.perf_counter()

    async def one(det: int) -> tuple[int, Loaded | None]:
        try:
            return det, await load_frames(grid, det)
        except (FileNotFoundError, ValueError) as e:
            log.error(
                "spectrum: D%d index unavailable ra=%.5f dec=%.5f: %s: %s", det, ra, dec, type(e).__name__, e
            )
            return det, None

    # the six loads run concurrently and share fetch's global semaphore
    results = await asyncio.gather(*(one(d) for d in config.DETECTORS))
    missing = [d for d, ld in results if ld is None]
    loaded_by_det = {d: ld for d, ld in results if ld is not None}
    if not loaded_by_det:
        msg = f"no detector index available (missing: {missing})"
        return JsonResult(_dumps({"error": msg}), status=503, error=msg)

    points: list[dict] = []
    n_failed = n_refs = 0
    try:
        for ld in loaded_by_det.values():
            pts, n_bad = await asyncio.to_thread(_spectrum_points, grid, ld, pmra, pmdec)
            points += pts
            n_failed += len(ld.failed) + n_bad
            n_refs += len(ld.refs)
    except Exception as e:  # JSON 500 + traceback in the log, never silent
        log.exception("spectrum failed ra=%.5f dec=%.5f", ra, dec)
        msg = f"spectrum failed: {type(e).__name__}: {e}"
        return JsonResult(_dumps({"error": msg}), status=500, error=msg)
    points.sort(key=lambda p: (p["lambda_um"], p["t_mjd"], p["det"]))
    payload = {
        "points": points,
        "unit": SPECTRUM_UNIT,
        "n_frames": len(points),
        "n_failed": n_failed,
        "dets": sorted({p["det"] for p in points}),
        "missing_dets": missing,
    }
    error = None if points else "no usable frames"
    if error:
        payload["error"] = error
    res = JsonResult(_dumps(payload), n_frames=len(points), n_failed=n_failed, error=error)
    if error is None and not missing and n_failed / (n_failed + len(points)) < config.MAX_FAIL_FRAC:
        try:
            await asyncio.to_thread(cache.json_put, key, res.body)
            res.cacheable = True
        except OSError as e:
            log.error("JSON cache write failed key=%s: %s: %s", key, type(e).__name__, e)
    else:
        log.warning(
            "not caching spectrum ra=%.5f dec=%.5f: missing_dets=%s failed=%d/%d error=%s",
            ra, dec, missing, n_failed, n_failed + len(points), error,
        )  # fmt: skip
    log.info(
        "spectrum ra=%.5f dec=%.5f size=%s pm=(%.1f,%.1f) source=live points=%d failed=%d missing=%s ms=%d",
        ra, dec, size_deg, pmra, pmdec, len(points), n_failed, missing, (time.perf_counter() - t0) * 1000,
    )  # fmt: skip
    return res
