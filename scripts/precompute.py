"""Fill L2 (data/cache/stacks) for the demo targets so the live path is never needed (SPRINT S3).

python scripts/precompute.py --targets all --dets 3,6 --modes pass,all --size 0.1
python scripts/precompute.py --targets wise0855,barnard --dets 6 --modes pass --extras motion
python scripts/precompute.py --targets all --dets 3,6 --extras ""          # modes only, no extras

--extras (default motion,lightcurve,lmdiff,spectrum): lmdiff = the lmdiff stack (L2 .ndjson.gz) for every target x det;
motion = /api/motion JSON for every target x det; lightcurve = /api/lightcurve JSON, ONLY for config.LIGHTCURVE_TARGETS
(the variables): a fixed aperture on a high-PM target is meaningless, those get one "skipped" row;
spectrum = /api/spectrum JSON (all 6 detectors, one row per target) ONLY for config.SPECTRUM_TARGETS, with the
catalogue PM from targets.CATALOG so the aperture follows the object. Needs all six detector indexes: with a
detector index missing the result is partial and NOT cached (exit code 1), by design.

Resumable and idempotent: a combination whose L2 file already exists is skipped (`--force` redoes it).
Exit code 1 if any combination with coverage ended with no L2 file (too many failed frames, index error).
"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

import orjson

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from targets import CATALOG, TARGETS

from backend import cache, config, index
from backend.models import make_grid
from backend.sequence import (
    MODES,
    JsonResult,
    lightcurve_result,
    motion_result,
    sequence_stream,
    spectrum_result,
)
from backend.timing import Timer

HEADER = ("target", "det", "mode", "frames", "failed", "seconds", "status")
EXTRAS = ("motion", "lightcurve", "lmdiff", "spectrum")
# Not in config.py (Track A owns it): ask to move.
SKIP_SPECTRUM = "skipped (not one of the 5 test targets)"
SKIP_LIGHTCURVE = "skipped (not a variable; fixed aperture is meaningless on a moving target)"


def _csv_arg(value: str, allowed: tuple, cast, what: str) -> list:
    items = [cast(x) for x in value.split(",") if x]
    bad = [x for x in items if x not in allowed]
    if bad:
        raise SystemExit(f"unknown {what}: {bad}; choose from {list(allowed)}")
    return items


async def run_one(
    ra: float, dec: float, size: float, det: int, mode: str, force: bool
) -> tuple[str, str, float, str]:
    """(frames, failed, seconds, status) for one combination; drains sequence_stream so it fills L2."""
    key = cache.stack_key(make_grid(ra, dec, size), det, mode, index.data_version())
    if not force and cache.l2_get(key) is not None:
        return "-", "-", 0.0, "skip (L2 hit)"
    t0 = time.perf_counter()
    done: dict = {}
    n_expected = n_errors = 0
    async for line in sequence_stream(ra, dec, size, det, mode, Timer()):
        msg = orjson.loads(line)
        if msg["type"] == "header":
            n_expected = msg["n_expected"]
        elif msg["type"] == "done":
            done = msg
        elif msg["type"] == "error":
            n_errors += 1
    dt = time.perf_counter() - t0
    if cache.l2_get(key) is not None:
        status = "cached"
    elif n_expected == 0 and not n_errors:
        status = "no coverage"
    else:
        status = "NOT CACHED"
    return str(done.get("n_frames", "?")), str(done.get("n_failed", n_errors)), dt, status


async def run_json(
    kind: str, ra: float, dec: float, size: float, det: int, force: bool
) -> tuple[str, str, float, str]:
    """(frames, failed, seconds, status) for /api/motion or /api/lightcurve; the result functions own the cache."""
    key = cache.json_key(kind, make_grid(ra, dec, size), det, index.data_version())
    if not force and cache.json_get(key) is not None:
        return "-", "-", 0.0, "skip (L2 hit)"
    fn = motion_result if kind == "motion" else lightcurve_result
    t0 = time.perf_counter()
    res: JsonResult = await fn(ra, dec, size, det, refresh=force)
    dt = time.perf_counter() - t0
    if res.status != 200:
        status = f"ERROR {res.status}: {res.error}"
    elif res.error is not None:
        status = f"no result: {res.error}"  # e.g. < 2 passes: reported, like "no coverage", not a failure
    elif res.cacheable:
        status = "cached"
    else:
        status = "NOT CACHED"
    return str(res.n_frames), str(res.n_failed), dt, status


async def run_spectrum(
    name: str, ra: float, dec: float, size: float, force: bool
) -> tuple[str, str, float, str]:
    """(frames, failed, seconds, status) for /api/spectrum with the target's catalogue PM (mas/yr)."""
    _, _, pmra, pmdec, _ = CATALOG[name]
    grid = make_grid(ra, dec, size)
    key = cache.spectrum_key(grid, pmra, pmdec, index.data_version())
    if not force and cache.json_get(key) is not None:
        return "-", "-", 0.0, "skip (L2 hit)"
    t0 = time.perf_counter()
    res: JsonResult = await spectrum_result(ra, dec, size, pmra, pmdec, refresh=force)
    dt = time.perf_counter() - t0
    if res.status != 200:
        status = f"ERROR {res.status}: {res.error}"
    elif res.error is not None:
        status = f"no result: {res.error}"
    elif res.cacheable:
        status = "cached"
    else:
        status = "NOT CACHED"  # partial (a detector index missing) or too many failed frames; see log
    return str(res.n_frames), str(res.n_failed), dt, status


def print_table(rows: list[tuple]) -> None:
    widths = [max(len(str(r[i])) for r in [HEADER, *rows]) for i in range(len(HEADER))]
    for r in [HEADER, *rows]:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))


async def main(a: argparse.Namespace) -> int:
    names = list(TARGETS) if a.targets == "all" else _csv_arg(a.targets, tuple(TARGETS), str, "target")
    dets = _csv_arg(a.dets, config.DETECTORS, int, "det")
    modes = _csv_arg(a.modes, MODES, str, "mode")
    extras = _csv_arg(a.extras, EXTRAS, str, "extra")
    if "lmdiff" in extras and "lmdiff" not in modes:
        modes.append("lmdiff")
    rows: list[tuple] = []
    for name in names:
        ra, dec = TARGETS[name]
        for det in dets:
            for mode in modes:
                frames, failed, dt, status = await run_one(ra, dec, a.size, det, mode, a.force)
                rows.append((name, det, mode, frames, failed, f"{dt:.1f}", status))
                print(*rows[-1], flush=True)  # progress, so an overnight run can be watched
            for kind in ("motion", "lightcurve"):
                if kind not in extras or (kind == "lightcurve" and name not in config.LIGHTCURVE_TARGETS):
                    continue
                frames, failed, dt, status = await run_json(kind, ra, dec, a.size, det, a.force)
                rows.append((name, det, kind, frames, failed, f"{dt:.1f}", status))
                print(*rows[-1], flush=True)
        if "spectrum" in extras:
            if name in config.SPECTRUM_TARGETS:
                frames, failed, dt, status = await run_spectrum(name, ra, dec, a.size, a.force)
                rows.append((name, "1-6", "spectrum", frames, failed, f"{dt:.1f}", status))
            else:
                rows.append((name, "-", "spectrum", "-", "-", "0.0", SKIP_SPECTRUM))
            print(*rows[-1], flush=True)
        if "lightcurve" in extras and name not in config.LIGHTCURVE_TARGETS:
            rows.append((name, "-", "lightcurve", "-", "-", "0.0", SKIP_LIGHTCURVE))
            print(*rows[-1], flush=True)
    print()
    print_table(rows)
    bad = [r for r in rows if r[-1] == "NOT CACHED" or r[-1].startswith("ERROR")]
    nocov = [r for r in rows if r[-1] == "no coverage"]
    nores = [r for r in rows if r[-1].startswith("no result")]
    if nores:
        print(f"\n{len(nores)} extra(s) produced no result: {[(r[0], r[1], r[2]) for r in nores]}")
    if nocov:
        print(f"\n{len(nocov)} combination(s) have no SPHEREx coverage: {[(r[0], r[1]) for r in nocov]}")
    if bad:
        print(
            f"\n{len(bad)} combination(s) NOT cached (>= 20% of frames failed, or index/compute error); see log",
            file=sys.stderr,
        )
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--targets", default="all", help="'all' or comma list of scripts/targets.py keys")
    ap.add_argument("--dets", default="3,6", help="comma list; only D3/D6 are in the footprint index")
    ap.add_argument("--modes", default="pass,all", help="comma list of: " + ",".join(MODES))
    ap.add_argument(
        "--extras", default=",".join(EXTRAS), help="comma list (may be empty) of: " + ",".join(EXTRAS)
    )
    ap.add_argument("--size", type=float, default=config.DEFAULT_FOV_DEG, choices=config.FOV_CHOICES_DEG)
    ap.add_argument("--force", action="store_true", help="recompute even if the L2 file exists")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    sys.exit(asyncio.run(main(ap.parse_args())))
