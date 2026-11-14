"""Recon R2: query IRSA TAP for a target and download its cutouts.

python scripts/download_target.py wise0855 --size 0.1 --det 1,2,3,4,5,6
"""

import argparse
import asyncio
import gzip
import random
import sys
import time
from pathlib import Path

import httpx
import pyvo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spx_util import RECON_DIR, adql_images, config, cutout_url
from targets import TARGETS


async def fetch_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str, dest: Path) -> int | str:
    """Returns bytes written, or an error string."""
    if dest.exists():
        return 0
    async with sem:
        err = ""
        for k in range(config.FETCH_RETRIES):
            try:
                r = await client.get(url)
                r.raise_for_status()
                content = r.content
                if content[:2] == b"\x1f\x8b":
                    content = gzip.decompress(content)
                tmp = dest.with_suffix(".tmp")
                tmp.write_bytes(content)
                tmp.replace(dest)
                return len(content)
            except (httpx.HTTPError, OSError) as e:
                err = f"{type(e).__name__}: {e}"
                await asyncio.sleep(0.5 * 2**k + random.random() * 0.3)
        return f"{url} -> {err}"


async def download(rows: list[dict], ra: float, dec: float, size: float, outdir: Path) -> list[int | str]:
    sem = asyncio.Semaphore(config.FETCH_CONCURRENCY)
    timeout = httpx.Timeout(config.FETCH_TIMEOUT_S)
    async with httpx.AsyncClient(http2=True, timeout=timeout, follow_redirects=True) as client:
        tasks = [
            fetch_one(client, sem, cutout_url(r["uri"], ra, dec, size), outdir / Path(r["uri"]).name)
            for r in rows
        ]
        return await asyncio.gather(*tasks)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", choices=sorted(TARGETS))
    ap.add_argument("--size", type=float, default=config.DEFAULT_FOV_DEG)
    ap.add_argument("--det", default="1,2,3,4,5,6")
    ap.add_argument("--limit", type=int, default=0, help="download at most N cutouts (0 = all)")
    a = ap.parse_args()
    dets = tuple(int(d) for d in a.det.split(","))
    ra, dec = TARGETS[a.target]

    wall0 = time.time()
    t0 = time.time()
    tab = pyvo.dal.TAPService(config.IRSA_TAP_URL).search(adql_images(ra, dec, dets)).to_table()
    tap_s = time.time() - t0

    outdir = RECON_DIR / a.target / f"size{a.size}"
    outdir.mkdir(parents=True, exist_ok=True)
    tab.write(RECON_DIR / a.target / "manifest.csv", format="csv", overwrite=True)
    rows = [dict(zip(tab.colnames, row)) for row in tab]
    if a.limit:
        rows = rows[: a.limit]

    results = asyncio.run(download(rows, ra, dec, a.size, outdir))
    failed = [r for r in results if isinstance(r, str)]
    nbytes = sum(r for r in results if isinstance(r, int))
    print(f"target={a.target} size={a.size} dets={dets}")
    print(
        f"TAP {tap_s:.1f}s, {len(tab)} rows; downloaded {len(results) - len(failed)} ({nbytes / 1e6:.1f} MB new)"
    )
    print(f"failed {len(failed)}")
    for f in failed:
        print("  FAIL", f)
    print(f"wall {time.time() - wall0:.1f}s")


if __name__ == "__main__":
    main()
