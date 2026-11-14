"""find_images + get_frames for a position, printing per-frame timing and L1 bytes.

python scripts/try_fetch.py                       # WISE 0855 D6 at 2025.9, 0.1 deg
python scripts/try_fetch.py --n 5 --det 3
"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import config, fetch, index
from backend.models import make_grid

WISE0855_2025 = (133.759993, -7.242201)  # PM-propagated to 2025.9 (notes/recon.md)


async def main(ra: float, dec: float, det: int, size: float, n: int | None) -> None:
    t0 = time.monotonic()
    refs = await index.find_images(ra, dec, det)
    print(f"find_images: {len(refs)} refs in {time.monotonic() - t0:.2f}s")
    if n:
        refs = refs[:n]
    if not refs:
        print("no coverage in the local index (build more weeks with scripts/build_s3_index.py)")
        return
    grid = make_grid(ra, dec, size)
    t0 = time.monotonic()
    n_ok = n_fail = 0
    async for i, frame, err in fetch.get_frames(refs, grid):
        dt = time.monotonic() - t0
        if frame is None:
            n_fail += 1
            print(f"  [{i}] {refs[i].obs_id}  {dt:6.2f}s  FAILED {err}")
        else:
            n_ok += 1
            print(
                f"  [{i}] {refs[i].obs_id}  {dt:6.2f}s  lambda={frame.lambda_um:.3f}um t={refs[i].t_mjd:.2f}"
            )
    total = time.monotonic() - t0
    nbytes = sum(fetch.l1_path(r, grid).stat().st_size for r in refs if fetch.l1_path(r, grid).exists())
    print(
        f"{n_ok} ok, {n_fail} failed, {total:.1f}s total, {total / len(refs):.2f}s/frame wall, L1 {nbytes / 1e3:.0f} kB"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ra", type=float, default=WISE0855_2025[0])
    ap.add_argument("--dec", type=float, default=WISE0855_2025[1])
    ap.add_argument("--det", type=int, default=6)
    ap.add_argument("--size", type=float, default=config.DEFAULT_FOV_DEG)
    ap.add_argument("--n", type=int, default=None, help="only the first N frames")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("astropy").setLevel(logging.WARNING)  # SIP-without-CTYPE info spam
    asyncio.run(main(a.ra, a.dec, a.det, a.size, a.n))
