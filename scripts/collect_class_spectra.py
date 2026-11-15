"""Spectra for the labelled objects, computed ON the EC2 box (same region as the data, no tunnel).

    ~/nasa/.venv/bin/python scripts/collect_class_spectra.py labels.csv out.jsonl [--per-class 150] [--frames 16]

Uses exactly the photometry of /api/spectrum (backend.sequence._spectrum_points: 2 px aperture, background annulus,
position at each frame's epoch from the proper motion) but only an evenly spaced subset of `--frames` exposures per
detector: the spectral SHAPE does not need all ~50, and this makes a spectrum about 3x cheaper. One JSON object per
line {id, cls, points: [[lambda_um, flux, err], ...]}; resumes if interrupted. Features are computed locally.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import config, index  # noqa: E402
from backend.models import make_grid  # noqa: E402
from backend.sequence import Loaded, _spectrum_points, fetch_interleaved, group_refs_by_pass, warm_worker  # noqa: E402,F401

PARALLEL_OBJECTS = 3


async def load_capped(grid, det: int, n: int) -> Loaded | None:
    try:
        refs = sorted(await index.find_images(grid.ra, grid.dec, det), key=lambda r: r.t_mjd)
    except (FileNotFoundError, ValueError):
        return None
    if len(refs) > n:
        refs = [refs[i] for i in np.unique(np.linspace(0, len(refs) - 1, n).round().astype(int))]
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


async def spectrum(row: dict, n: int) -> dict | None:
    grid = make_grid(float(row["ra"]) % 360.0, float(row["dec"]), 0.05)
    loads = await asyncio.gather(*(load_capped(grid, d, n) for d in config.DETECTORS))
    pts: list = []
    for ld in loads:
        if ld is not None and ld.frames:
            p, _ = await asyncio.to_thread(_spectrum_points, grid, ld, float(row["pmra"]), float(row["pmdec"]))
            pts += p
    if not pts:
        return None
    return dict(id=row["id"], cls=row["cls"], ra=float(row["ra"]), dec=float(row["dec"]),
                points=[[round(p["lambda_um"], 4), p["flux"], p["err"]] for p in pts])


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("labels")
    ap.add_argument("out")
    ap.add_argument("--per-class", type=int, default=150)
    ap.add_argument("--frames", type=int, default=16)
    a = ap.parse_args()
    rows, taken = [], Counter()
    for r in csv.DictReader(open(a.labels)):
        if taken[r["cls"]] < a.per_class:
            taken[r["cls"]] += 1
            rows.append(r)
    out = Path(a.out)
    done = {json.loads(line)["id"] for line in out.read_text().splitlines()} if out.exists() else set()
    todo = [r for r in rows if r["id"] not in done]
    print(f"{len(rows)} wanted, {len(done)} done, {len(todo)} to do", flush=True)
    await asyncio.gather(*(asyncio.to_thread(index._load, p) for p in dict.fromkeys(config.S3_INDEX_PATHS.values())),
                         return_exceptions=True)
    sem = asyncio.Semaphore(PARALLEL_OBJECTS)
    n_ok = n_bad = 0
    with open(out, "a") as f:
        async def work(r):
            nonlocal n_ok, n_bad
            async with sem:
                try:
                    res = await spectrum(r, a.frames)
                except Exception as e:  # noqa: BLE001 - one bad object must not stop the run
                    print("failed", r["id"], type(e).__name__, e, flush=True)
                    res = None
                if res is None:
                    n_bad += 1
                else:
                    n_ok += 1
                    f.write(json.dumps(res) + "\n")
                    f.flush()
                if (n_ok + n_bad) % 20 == 0:
                    print(f"  {n_ok + n_bad}/{len(todo)} ok={n_ok} bad={n_bad}", flush=True)
        await asyncio.gather(*(work(r) for r in todo))
    print("done", n_ok, "ok", n_bad, "bad")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

