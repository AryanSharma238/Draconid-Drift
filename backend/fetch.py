"""Async acquisition of cutouts: S3 range reads (FETCH_METHOD="s3") + L1 cache, then process.

Only the s3 branch exists (IRSA cutout service down, SPRINT decision 2026-11-14). Files on
S3 are uncompressed full frames (2040x2040); we read only the grid box of IMAGE/FLAGS/
VARIANCE/ZODI (EPSF skipped) and write a small cropped FITS to L1 (contract C7).
"""

import asyncio
import contextlib
import io
import logging
import os
import random
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import httpx
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

from backend import config, process
from backend.models import Frame, Grid, ImageRef

log = logging.getLogger("fetch")

SECTION_HDUS = ("IMAGE", "FLAGS", "VARIANCE", "ZODI")  # EPSF deliberately skipped
LAMBDA_KEY = "LAMBDA_UM"

import sys

_pool: ProcessPoolExecutor | ThreadPoolExecutor | None = None
_read_pool: ProcessPoolExecutor | ThreadPoolExecutor | None = None
_sem: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _use_threads() -> bool:
    """Processes only on the EC2 backend server. Inside the Streamlit app (Streamlit Cloud, laptops) worker
    processes re-import the whole app on start-up and each holds its own copy of numpy/astropy: dozens of them
    run the 1 GB Streamlit Cloud machine out of memory and the app dies ("Oh no. Error running app")."""
    import os
    return sys.platform == "darwin" or "streamlit" in sys.modules or os.environ.get("DRIFT_THREADS") == "1"


def _get_pool() -> ProcessPoolExecutor | ThreadPoolExecutor:
    global _pool
    if _pool is None:
        if _use_threads():
            _pool = ThreadPoolExecutor(max_workers=config.PROCESS_WORKERS * 2)
        else:
            _pool = ProcessPoolExecutor(max_workers=config.PROCESS_WORKERS)
    return _pool


def _get_read_pool() -> ProcessPoolExecutor | ThreadPoolExecutor:
    """S3 reads in processes on Linux (EC2), threads on macOS (avoids macOS fork/spawn crash in Streamlit)."""
    global _read_pool
    if _read_pool is None:
        if _use_threads():
            _read_pool = ThreadPoolExecutor(max_workers=config.S3_READ_WORKERS * 2)
        else:
            _read_pool = ProcessPoolExecutor(max_workers=config.S3_READ_WORKERS)
    return _read_pool


def _global_sem() -> asyncio.Semaphore:
    """One semaphore shared by all requests (rebuilt only if the event loop changed)."""
    global _sem
    loop = asyncio.get_running_loop()
    if _sem is None or _sem[0] is not loop:
        _sem = (loop, asyncio.Semaphore(config.FETCH_CONCURRENCY))
    return _sem[1]


def l1_path(ref: ImageRef, grid: Grid) -> Path:  # contract C7
    return config.RAW_CACHE_DIR / (f"{ref.obs_id}_{grid.ra:.3f}_{grid.dec:+.3f}_{grid.size_deg}.fits")


FITS_BLOCK = 2880
_http: httpx.Client | None = None
_get_threads: ThreadPoolExecutor | None = None
# Per-process memo of the HDU layout after FLAGS (the only variable-size HDU: tile-compressed):
# [(extname, header_bytes, data_span_bytes), ...] learned from the last file read. Used only to
# PREDICT byte ranges; astropy still parses the real bytes, and a wrong guess costs a fallback GET.
_tail_layout: list[tuple[str, int, int]] = []


def _client() -> httpx.Client:
    global _http, _get_threads
    if _http is None:
        _http = httpx.Client(
            timeout=config.FETCH_TIMEOUT_S, limits=httpx.Limits(max_connections=config.S3_GET_PARALLEL)
        )
        _get_threads = ThreadPoolExecutor(config.S3_GET_PARALLEL)
    return _http


def _get_range(url: str, start: int, end: int) -> tuple[bytes, int]:
    """Bytes [start, end) and the total object size (from Content-Range)."""
    try:
        r = _client().get(url, headers={"Range": f"bytes={start}-{end - 1}"})
    except httpx.TransportError as e:  # -> OSError so _read_s3_box retries it
        raise OSError(f"{type(e).__name__}: {e} for {url}") from e
    if r.status_code == 404:
        raise FileNotFoundError(url)
    if r.status_code != 206:
        raise OSError(f"HTTP {r.status_code} for {url} [{start}, {end})")
    return r.content, int(r.headers["content-range"].rsplit("/", 1)[1])


class _RangeFile(io.RawIOBase):
    """Seekable read-only file over an S3 object: serves prefetched byte ranges, GETs any miss."""

    def __init__(self, url: str, size: int):
        self.url, self.size, self.pos, self.misses = url, size, 0, 0
        self.parts: list[tuple[int, bytes]] = []
        self.name, self.mode = url, "rb"

    def add(self, start: int, data: bytes) -> None:
        self.parts.append((start, data))

    def prefetch(self, ranges: list[tuple[int, int]]) -> None:
        """GET all ranges in parallel (clipped to the object)."""
        rs = [(max(0, a), min(self.size, b)) for a, b in ranges if a < self.size and b > a]
        _client()
        futs = [(a, _get_threads.submit(_get_range, self.url, a, b)) for a, b in rs]
        for a, f in futs:
            self.add(a, f.result()[0])

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, off: int, whence: int = 0) -> int:
        self.pos = {0: off, 1: self.pos + off, 2: self.size + off}[whence]
        return self.pos

    def read(self, n: int = -1) -> bytes:
        n = self.size - self.pos if n is None or n < 0 else min(n, self.size - self.pos)
        if n <= 0:
            return b""
        for start, data in self.parts:
            if start <= self.pos and self.pos + n <= start + len(data):
                out = data[self.pos - start : self.pos - start + n]
                break
        else:
            self.misses += 1
            data, _ = _get_range(
                self.url, self.pos, min(self.size, self.pos + max(n, config.S3_READ_BLOCK_BYTES))
            )
            self.add(self.pos, data)
            out = data[:n]
        self.pos += len(out)
        return out

    def readinto(self, b) -> int:
        out = self.read(len(b))
        b[: len(out)] = out
        return len(out)


def _hdr_at(f: _RangeFile, loc: int) -> tuple[fits.Header, int]:
    """Header starting at byte `loc` and its padded length in bytes (reads via f, prefetched or not)."""
    f.seek(loc)
    buf = b""
    while True:
        buf += f.read(FITS_BLOCK)
        end = next(
            (
                i
                for i in range(len(buf) - FITS_BLOCK, len(buf), 80)
                if buf[i : i + 3] == b"END" and buf[i : i + 80].rstrip() == b"END"
            ),
            None,
        )
        if end is not None:
            return fits.Header.fromstring(buf[: end + 80].decode("ascii")), len(buf)


def _data_span(h: fits.Header) -> int:
    n = (
        abs(h["BITPIX"]) // 8 * int(np.prod([h[f"NAXIS{i}"] for i in range(1, h["NAXIS"] + 1)]))
        if h["NAXIS"]
        else 0
    )
    n += h.get("PCOUNT", 0)
    return -(-n // FITS_BLOCK) * FITS_BLOCK


def _rows_range(dat: int, naxis1: int, bitpix: int, y0: int, y1: int) -> tuple[int, int]:
    row = naxis1 * abs(bitpix) // 8
    return dat + y0 * row, dat + y1 * row


def _flags_heap_range(f: _RangeFile, fh: fits.Header, fdat: int, y0: int, y1: int) -> tuple[int, int] | None:
    """Heap byte span of the tile-compressed FLAGS tiles covering rows y0..y1, or None if the
    table layout is not the simple one (then astropy's own reads fall back to GETs)."""
    if (
        fh.get("TTYPE1") != "COMPRESSED_DATA"
        or fh.get("ZTILE1") != fh.get("ZNAXIS1")
        or fh.get("ZTILE2", 1) != 1
    ):
        return None
    tform = fh["TFORM1"].strip().lstrip("1")
    dt = {"P": ">i4", "Q": ">i8"}.get(tform[:1])
    if dt is None:
        return None
    f.seek(fdat + y0 * fh["NAXIS1"])
    tab = f.read((y1 - y0) * fh["NAXIS1"])
    desc = np.frombuffer(tab, dtype=np.dtype(dt)).reshape(y1 - y0, fh["NAXIS1"] // np.dtype(dt).itemsize)[
        :, :2
    ]
    heap = fdat + fh.get("THEAP", fh["NAXIS1"] * fh["NAXIS2"])
    return heap + int(desc[:, 1].min()), heap + int((desc[:, 1] + desc[:, 0]).max())


@contextlib.contextmanager
def _open_s3(key: str, grid: Grid | None = None) -> Iterator[fits.HDUList]:
    """Lazy HDUList over anonymous S3 HTTPS. With `grid`, prefetches in ~4 parallel rounds every
    byte range crop_hdul will touch (headers, the box rows of each HDU, the FLAGS tiles, WCS-WAVE),
    instead of ~19 sequential readahead GETs (measured 2026-11-14). Misses fall back to a GET."""
    url = config.S3_PUBLIC_BASE + key
    head, size = _get_range(url, 0, config.S3_HEADER_BYTES)
    f = _RangeFile(url, size)
    f.add(0, head)
    if grid is not None:
        _prefetch_box(f, grid)
    f.seek(0)  # astropy takes the current position as the start of the FITS file
    with fits.open(f, lazy_load_hdus=True) as hdul:
        yield hdul
        _learn_tail(hdul)
    log.debug("s3 %s: %d fallback GETs", key, f.misses)


def _prefetch_box(f: _RangeFile, grid: Grid) -> None:
    ih, ilen = _hdr_at(f, FITS_BLOCK)
    idat = FITS_BLOCK + ilen
    _, _, y0, y1 = grid_box(WCS(ih), grid, ih["NAXIS1"], ih["NAXIS2"])  # whole rows are read
    fh_loc = idat + _data_span(ih)
    f.prefetch(
        [(fh_loc, fh_loc + config.S3_HEADER_BYTES), _rows_range(idat, ih["NAXIS1"], ih["BITPIX"], y0, y1)]
    )
    fh, flen = _hdr_at(f, fh_loc)
    fdat = fh_loc + flen
    loc = fdat + _data_span(fh)  # first HDU after FLAGS
    ranges = [(fdat + y0 * fh["NAXIS1"], fdat + y1 * fh["NAXIS1"])]
    for name, hlen, span in _tail_layout:
        ranges.append((loc, loc + hlen))
        if name in SECTION_HDUS and name in _ROW_GEOM:
            ranges.append(_rows_range(loc + hlen, *_ROW_GEOM[name], y0, y1))
        elif name != "EPSF":
            ranges.append((loc + hlen, loc + hlen + span))  # small lookup HDUs (WCS-WAVE) whole
        loc += hlen + span
    f.prefetch(ranges)
    heap = _flags_heap_range(f, fh, fdat, y0, y1)
    if heap is not None:
        f.prefetch([heap])


_ROW_GEOM: dict[
    str, tuple[int, int]
] = {}  # (NAXIS1, BITPIX) of each sectioned tail HDU, learned with the layout


def _learn_tail(hdul: fits.HDUList) -> None:
    """Record the HDU layout after FLAGS from the file just read (only HDUs astropy loaded)."""
    names = [h.name for h in hdul]
    if "FLAGS" not in names:
        return
    out = []
    for i in range(names.index("FLAGS") + 1, len(hdul)):
        fi = hdul.fileinfo(i)
        if fi is None:
            break
        out.append((hdul[i].name, fi["datLoc"] - fi["hdrLoc"], fi["datSpan"]))
        if hdul[i].name in SECTION_HDUS:
            _ROW_GEOM[hdul[i].name] = (hdul[i].header["NAXIS1"], hdul[i].header["BITPIX"])
    _tail_layout[:] = out


def grid_box(full_wcs: WCS, grid: Grid, naxis1: int, naxis2: int) -> tuple[int, int, int, int]:
    """Source pixel box (x0, x1, y0, y1), 0-based half-open, covering the grid plus padding."""
    g = np.linspace(0, grid.npix - 1, config.S3_EDGE_SAMPLES)
    gx, gy = np.meshgrid(g, g)
    ra, dec = grid.wcs().pixel_to_world_values(gx.ravel(), gy.ravel())
    x, y = full_wcs.world_to_pixel_values(ra, dec)
    x0 = max(0, int(np.floor(np.nanmin(x))) - config.S3_BOX_PAD_PX)
    x1 = min(naxis1, int(np.ceil(np.nanmax(x))) + config.S3_BOX_PAD_PX + 1)
    y0 = max(0, int(np.floor(np.nanmin(y))) - config.S3_BOX_PAD_PX)
    y1 = min(naxis2, int(np.ceil(np.nanmax(y))) + config.S3_BOX_PAD_PX + 1)
    if x0 >= x1 or y0 >= y1:
        raise ValueError(f"grid centre ({grid.ra}, {grid.dec}) is outside this image")
    return x0, x1, y0, y1


def crop_hdul(hdul: fits.HDUList, grid: Grid) -> bytes:
    """Cropped FITS bytes for the grid box. Runs blocking I/O; call from a thread.

    CRPIX is shifted by the box offset (SIP is relative to CRPIX so it stays valid).
    The wavelength is computed from the FULL frame (the 'W' WCS is a lookup into WCS-WAVE
    indexed by full-frame pixels) and stored as LAMBDA_UM.
    """
    full_hdr = hdul["IMAGE"].header
    x0, x1, y0, y1 = grid_box(WCS(full_hdr), grid, full_hdr["NAXIS1"], full_hdr["NAXIS2"])
    lam = process.wavelength_at(hdul, grid.ra, grid.dec)
    out = [fits.PrimaryHDU(header=hdul[0].header.copy())]
    for name in SECTION_HDUS:
        if name not in hdul:
            continue
        hdr = hdul[name].header.copy()
        data = np.asarray(hdul[name].section[y0:y1, x0:x1])
        for k in ("BZERO", "BSCALE", "BLANK"):
            hdr.remove(k, ignore_missing=True)
        hdr["CRPIX1"] = hdr["CRPIX1"] - x0
        hdr["CRPIX2"] = hdr["CRPIX2"] - y0
        if name == "IMAGE":
            hdr["HIERARCH " + LAMBDA_KEY] = (float(lam), "um at grid centre, from full-frame WCS-WAVE")
        out.append(fits.ImageHDU(data, hdr, name=name))
    buf = io.BytesIO()
    fits.HDUList(out).writeto(buf)
    return buf.getvalue()


def _read_s3_box(ref: ImageRef, grid: Grid) -> bytes:
    """Blocking, with retries on transient I/O errors. A missing key is not retried."""
    last: Exception | None = None
    for attempt in range(config.FETCH_RETRIES):
        try:
            with _open_s3(ref.uri, grid) as hdul:
                return crop_hdul(hdul, grid)
        except FileNotFoundError:
            raise
        except (OSError, TimeoutError) as e:
            last = e
            log.warning("s3 read %s attempt %d failed: %r", ref.uri, attempt + 1, e)
            time.sleep(0.5 * 2**attempt + random.uniform(0, 0.3))
    raise RuntimeError(f"s3 read failed after {config.FETCH_RETRIES} tries: {last!r}")


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


async def _acquire(ref: ImageRef, grid: Grid) -> bytes:
    """L1 hit -> disk bytes; miss -> S3 box read (under the global semaphore) -> atomic L1 write."""
    path = l1_path(ref, grid)
    if path.exists():
        return await asyncio.to_thread(path.read_bytes)
    if config.FETCH_METHOD != "s3":
        raise NotImplementedError(f"FETCH_METHOD={config.FETCH_METHOD!r}: only 's3' is implemented")
    async with _global_sem():
        data = await asyncio.get_running_loop().run_in_executor(_get_read_pool(), _read_s3_box, ref, grid)
    await asyncio.to_thread(_write_atomic, path, data)
    return data


async def _one(i: int, ref: ImageRef, grid: Grid) -> tuple[int, Frame | None, str | None]:
    try:
        data = await _acquire(ref, grid)
        loop = asyncio.get_running_loop()
        frame = await loop.run_in_executor(_get_pool(), process.process_cutout, data, ref, grid)
        return i, frame, None
    except Exception as e:  # noqa: BLE001 - one bad file must not kill the sequence; logged + counted
        log.error("fetch failed obs_id=%s key=%s: %s: %s", ref.obs_id, ref.uri, type(e).__name__, e)
        return i, None, f"{type(e).__name__}: {e}"


async def get_frames(refs: list[ImageRef], grid: Grid) -> AsyncIterator[tuple[int, Frame | None, str | None]]:
    """Yields (i, frame, error) as each finishes; i indexes `refs`. Never raises."""
    tasks = [asyncio.ensure_future(_one(i, r, grid)) for i, r in enumerate(refs)]
    try:
        for fut in asyncio.as_completed(tasks):
            yield await fut
    finally:
        for t in tasks:
            t.cancel()
