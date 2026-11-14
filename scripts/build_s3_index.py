"""Build the S3 footprint index (SPRINT decision 2026-11-14).

Lists D6 keys for config.S3_INDEX_RELEASES, ranged-GETs each file's header, and appends one
row (key, release, exposure_id, version, det, mjd_beg, mjd_avg, 4 sky corners) to
config.S3_INDEX_PATH (gzip CSV). Resumable: keys already in the file are skipped; rows are
flushed periodically, so a killed run loses at most the last few seconds.

python scripts/build_s3_index.py                      # full build
python scripts/build_s3_index.py --weeks 1 --limit 200  # smoke test
"""

import argparse
import asyncio
import csv
import gzip
import logging
import os
import random
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import httpx
from astropy.io import fits
from astropy.wcs import WCS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import config
from backend.index import INDEX_COLUMNS, parse_key, read_rows

log = logging.getLogger("build_s3_index")
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
FITS_BLOCK = 2880
FLUSH_EVERY_ROWS = 500
FLUSH_EVERY_S = 10.0
PROGRESS_EVERY_S = 5.0


def footprint_row(key: str, data: bytes) -> dict:
    """Index row from the first S3_HEADER_BYTES of a file (PRIMARY + IMAGE header)."""
    meta = parse_key(key)
    # PRIMARY has no data, so the IMAGE header starts at the second FITS block run.
    end = next(
        (i for i in range(FITS_BLOCK, len(data) - 79, 80) if data[i : i + 80].rstrip() == b"END"),
        None,
    )
    if end is None:
        raise ValueError("IMAGE header END card not within the fetched bytes")
    hdr = fits.Header.fromstring(data[FITS_BLOCK : end + 80].decode("ascii"))
    corners = WCS(hdr).calc_footprint()  # (4, 2) ra, dec
    row = {
        "key": key,
        "release": meta["release"],
        "exposure_id": meta["exposure_id"],
        "version": meta["version"],
        "det": int(meta["det"]),
        "mjd_beg": float(hdr["MJD-BEG"]),
        "mjd_avg": float(hdr["MJD-AVG"]),
    }
    for i, (ra, dec) in enumerate(corners):
        row[f"ra{i}"] = round(float(ra), 6)
        row[f"dec{i}"] = round(float(dec), 6)
    return row


async def _get(client: httpx.AsyncClient, url: str, **kw) -> httpx.Response:
    """GET with FETCH_RETRIES retries on transport errors / 5xx / 429 (recon backoff)."""
    last: Exception | None = None
    for attempt in range(config.FETCH_RETRIES):
        try:
            r = await client.get(url, **kw)
            if r.status_code in (200, 206):
                return r
            if r.status_code < 500 and r.status_code != 429:
                r.raise_for_status()
            last = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
        except httpx.TransportError as e:
            last = e
        await asyncio.sleep(0.5 * 2**attempt + random.uniform(0, 0.3))
    raise RuntimeError(f"{url} failed after {config.FETCH_RETRIES} tries: {last!r}")


async def list_prefix(
    client: httpx.AsyncClient, prefix: str, delimiter: str | None
) -> tuple[list[str], list[str]]:
    """(keys, common prefixes) under `prefix`, following continuation tokens."""
    keys: list[str] = []
    prefixes: list[str] = []
    token: str | None = None
    while True:
        params = {"list-type": "2", "prefix": prefix}
        if delimiter:
            params["delimiter"] = delimiter
        if token:
            params["continuation-token"] = token  # httpx URL-encodes it
        root = ET.fromstring((await _get(client, config.S3_PUBLIC_BASE, params=params)).content)
        keys += [e.findtext(f"{S3_NS}Key") for e in root.iter(f"{S3_NS}Contents")]
        prefixes += [e.findtext(f"{S3_NS}Prefix") for e in root.iter(f"{S3_NS}CommonPrefixes")]
        if root.findtext(f"{S3_NS}IsTruncated") != "true":
            return keys, prefixes
        token = root.findtext(f"{S3_NS}NextContinuationToken")


async def list_det_keys(client: httpx.AsyncClient, release: str, weeks: int | None, det: int) -> list[str]:
    _, week_dirs = await list_prefix(client, f"{release}/level2/", "/")
    week_dirs = sorted(week_dirs)[:weeks]
    out: list[str] = []
    for wd in week_dirs:
        _, vers = await list_prefix(client, wd, "/")
        res = await asyncio.gather(*(list_prefix(client, f"{v}{det}/", None) for v in vers))
        n = 0
        for keys, _ in res:
            out += [k for k in keys if k.endswith(".fits")]
            n += len(keys)
        log.info("%s: %d keys in %d version dirs", wd, n, len(vers))
    return out


class _Writer:
    """Append-only gzip CSV writer; each flush is a sync-flush so a killed run stays readable."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists() or path.stat().st_size == 0
        self.f = gzip.open(path, "at", newline="")  # noqa: SIM115 (lives as long as the writer)
        self.w = csv.DictWriter(self.f, fieldnames=INDEX_COLUMNS)
        if new:
            self.w.writeheader()
        self.pending = 0
        self.last_flush = time.monotonic()

    def write(self, row: dict) -> None:
        self.w.writerow(row)
        self.pending += 1
        if self.pending >= FLUSH_EVERY_ROWS or time.monotonic() - self.last_flush > FLUSH_EVERY_S:
            self.flush()

    def flush(self) -> None:
        self.f.flush()
        self.pending = 0
        self.last_flush = time.monotonic()

    def close(self) -> None:
        self.f.close()


async def build(
    releases: list[str], weeks: int | None, limit: int | None, det: int = config.S3_INDEX_DET
) -> int:
    path = config.S3_INDEX_PATHS[det]
    done = {r["key"] for r in read_rows(path)} if path.exists() else set()
    log.info("resuming with %d rows already indexed", len(done))
    timeout = httpx.Timeout(config.FETCH_TIMEOUT_S)
    limits = httpx.Limits(max_connections=config.S3_INDEX_CONCURRENCY)
    async with httpx.AsyncClient(http2=True, timeout=timeout, limits=limits) as client:
        keys: list[str] = []
        for rel in releases:
            keys += await list_det_keys(client, rel, weeks, det)
        todo = [k for k in dict.fromkeys(keys) if k not in done]
        log.info("%d keys listed, %d to fetch", len(keys), len(todo))
        if limit is not None:
            todo = todo[:limit]
        total, n_ok, n_fail = len(todo), 0, 0
        writer = _Writer(path)
        sem = asyncio.Semaphore(config.S3_INDEX_CONCURRENCY)
        loop = asyncio.get_running_loop()
        n_proc = os.cpu_count() or 1
        pool = ProcessPoolExecutor(max_workers=n_proc)
        log.info("parsing headers on %d processes", n_proc)
        t0 = last = time.monotonic()

        async def one(key: str) -> dict | None:
            nonlocal n_fail
            async with sem:
                try:
                    r = await _get(
                        client,
                        config.S3_PUBLIC_BASE + key,
                        headers={"Range": f"bytes=0-{config.S3_HEADER_BYTES - 1}"},
                    )
                    # Header parse + WCS is the bottleneck (~52/s on one core): run it on all cores.
                    return await loop.run_in_executor(pool, footprint_row, key, r.content)
                except (RuntimeError, ValueError, KeyError, httpx.HTTPError) as e:
                    n_fail += 1
                    log.error("FAILED %s: %s: %s", key, type(e).__name__, e)
                    return None

        try:
            for fut in asyncio.as_completed([one(k) for k in todo]):
                row = await fut
                if row is not None:
                    writer.write(row)
                    n_ok += 1
                now = time.monotonic()
                if now - last > PROGRESS_EVERY_S:
                    last = now
                    n = n_ok + n_fail
                    rate = n / (now - t0)
                    eta = (total - n) / rate if rate else float("inf")
                    print(f"{n}/{total}  {rate:.1f}/s  ETA {eta:.0f}s  failed={n_fail}", flush=True)
        finally:
            writer.close()
            pool.shutdown(cancel_futures=True)
        dt = time.monotonic() - t0
        print(f"done: {n_ok} ok, {n_fail} failed, {total} requested, {dt:.1f}s, {n_ok / dt:.1f}/s")
    return n_fail


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--releases", default=",".join(config.S3_INDEX_RELEASES))
    ap.add_argument("--weeks", type=int, default=None, help="first N week dirs per release only")
    ap.add_argument("--det", type=int, default=config.S3_INDEX_DET, choices=sorted(config.S3_INDEX_PATHS))
    ap.add_argument("--limit", type=int, default=None, help="max new keys to fetch")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(build([r for r in a.releases.split(",") if r], a.weeks, a.limit, a.det))


if __name__ == "__main__":
    main()
