"""Image discovery from the S3 footprint index (SPRINT 2026-11-14: replaces the TAP query).

The index (config.S3_INDEX_PATH, built by scripts/build_s3_index.py) holds one row per D6
exposure with its four sky corners. D3 keys are derived from the D6 key (same pointing to
0.02 deg within an exposure); existence of the derived file is NOT checked here, a missing
file shows up as a counted fetch failure.
"""

import asyncio
import csv
import gzip
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from backend import config
from backend.models import ImageRef

log = logging.getLogger("index")

INDEXED_DETS = tuple(sorted(config.S3_INDEX_PAIRS))  # an index file not built yet fails loud on load
INDEX_COLUMNS = (
    "key",
    "release",
    "exposure_id",
    "version",
    "det",
    "mjd_beg",
    "mjd_avg",
    *(f"ra{i}" for i in range(4)),
    *(f"dec{i}" for i in range(4)),
)
# contract C2 filename regex (confirmed on 56 real names)
FILENAME_RE = re.compile(
    r"^level2_(?P<exposure_id>\d{4}W\d{2}_\d[A-Z]_\d{4}_\d)D(?P<det>[1-6])_spx_"
    r"(?P<version>l2b(?:_retry)?-v\d+-\d{4}-\d{3})\.fits$"
)
VERSION_RE = re.compile(r"^l2b(?P<retry>_retry)?-v(?P<v>\d+)-(?P<yyyy>\d{4})-(?P<ddd>\d{3})$")
PREFILTER_RADIUS_DEG = 3.0  # half-diagonal of a 2040 px detector is ~2.5 deg


def parse_key(key: str) -> dict[str, str]:
    """{release, exposure_id, version, det} from an S3 key; ValueError if it doesn't match C2."""
    parts = key.split("/")
    m = FILENAME_RE.match(parts[-1])
    if m is None or len(parts) != 6:
        raise ValueError(f"unrecognised SPHEREx key: {key!r}")
    return {"release": parts[0], **m.groupdict()}


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """(vNN, yyyy, ddd, is_retry) for C2 ordering. Never compare version strings directly."""
    m = VERSION_RE.match(version)
    if m is None:
        raise ValueError(f"unrecognised version: {version!r}")
    return int(m["v"]), int(m["yyyy"]), int(m["ddd"]), int(m["retry"] is not None)


def _dedupe(refs: list[ImageRef]) -> list[ImageRef]:
    """Contract C2 dedupe (private; Track E's ids.dedupe is not merged yet; Track A to swap)."""
    prio = {r: i for i, r in enumerate(config.RELEASE_PRIORITY)}
    best: dict[str, ImageRef] = {}
    for r in refs:
        k = (-prio.get(r.release, len(prio)), version_tuple(r.version))
        cur = best.get(r.obs_id)
        if cur is None or k > (-prio.get(cur.release, len(prio)), version_tuple(cur.version)):
            best[r.obs_id] = r
    return list(best.values())


@dataclass
class _Index:
    rows: list[str]  # S3 keys (release / exposure / version parsed from them)
    ra: np.ndarray  # (N, 4)
    dec: np.ndarray  # (N, 4)
    mjd_beg: np.ndarray  # (N,)
    cen: np.ndarray  # (N, 3) unit vector of the mean corner direction


_cache: dict[Path, tuple[float, _Index]] = {}


def read_rows(path: Path) -> list[dict[str, str]]:
    """Read a (possibly truncated, multi-member) gzip CSV. A truncated tail is logged, not fatal."""
    rows: list[dict[str, str]] = []
    try:
        with gzip.open(path, "rt", newline="") as f:
            rows.extend(csv.DictReader(f))
    except EOFError:
        log.warning("index %s has a truncated tail; kept %d complete rows", path, len(rows))
    return rows


def _load(path: Path) -> _Index:
    mtime = path.stat().st_mtime  # FileNotFoundError is loud on purpose
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    npz_path = path.with_suffix('.npz')
    if npz_path.is_file() and npz_path.stat().st_mtime >= mtime:
        try:
            z = np.load(npz_path)
            idx = _Index(list(z['key']), z['ra'], z['dec'], z['mjd_beg'], z['cen'])
            _cache[path] = (mtime, idx)
            log.info('loaded footprint index from binary npz: %d rows', len(idx.rows))
            return idx
        except Exception as e:
            log.warning('npz index read failed %s, falling back to csv: %s', npz_path, e)
    # stream the CSV straight into compact arrays: a dict per row cost ~1.7 KB (460 MB for one index)
    from array import array
    slim, corners, mjds = [], array("d"), array("d")
    try:
        with gzip.open(path, "rt", newline="") as f:
            rd = csv.reader(f)
            h = {n: i for i, n in enumerate(next(rd))}
            ik, ir, ie, iv, im = h["key"], h["release"], h["exposure_id"], h["version"], h["mjd_beg"]
            ic = [h[f"ra{i}"] for i in range(4)] + [h[f"dec{i}"] for i in range(4)]
            for r in rd:
                if len(r) < len(h):
                    continue
                slim.append(r[ik])                          # release, exposure_id, version are all in the key (checked: 0 mismatches)
                corners.extend(float(r[i]) for i in ic)
                mjds.append(float(r[im]))
    except EOFError:
        log.warning("index %s has a truncated tail; kept %d complete rows", path, len(slim))
    cc = np.frombuffer(corners, dtype=float).reshape(-1, 8)
    del corners
    ra, dec, mjd = cc[:, :4].copy(), cc[:, 4:].copy(), np.frombuffer(mjds, dtype=float).copy()
    rr, dd = np.deg2rad(ra), np.deg2rad(dec)
    v = np.stack([np.cos(dd) * np.cos(rr), np.cos(dd) * np.sin(rr), np.sin(dd)], axis=-1)
    cen = v.mean(axis=1)
    cen /= np.linalg.norm(cen, axis=-1, keepdims=True) if len(cen) else 1
    idx = _Index(slim, ra, dec, mjd, cen)
    _cache[path] = (mtime, idx)
    log.info("loaded footprint index: %d rows", len(slim))
    return idx


def contains(ra: float, dec: float, corner_ra: np.ndarray, corner_dec: np.ndarray) -> np.ndarray:
    """Exact spherical point-in-quadrilateral test for N quads (corner arrays shape (N, 4)).

    Gnomonic projection about (ra, dec) maps great circles to straight lines, so a spherical
    convex polygon (sides along great circles) stays a straight-sided polygon; the point is the
    origin. Inside iff all edge cross products share a sign. Requires corners within 90 deg of
    the point (guaranteed by the caller's prefilter); others are reported False.
    """
    r0, d0 = np.deg2rad(ra), np.deg2rad(dec)
    r, d = np.deg2rad(corner_ra), np.deg2rad(corner_dec)
    cosc = np.sin(d0) * np.sin(d) + np.cos(d0) * np.cos(d) * np.cos(r - r0)
    with np.errstate(divide="ignore", invalid="ignore"):
        xi = np.cos(d) * np.sin(r - r0) / cosc
        eta = (np.cos(d0) * np.sin(d) - np.sin(d0) * np.cos(d) * np.cos(r - r0)) / cosc
    xn, yn = np.roll(xi, -1, axis=1), np.roll(eta, -1, axis=1)
    cross = xi * yn - xn * eta  # (corner_i x corner_{i+1}) z-component, origin = point
    ok = (cosc > 0).all(axis=1)
    return ok & ((cross > 0).all(axis=1) | (cross < 0).all(axis=1))


def derive_key(key: str, det: int) -> str:
    """Indexed key (det config.S3_INDEX_PAIRS[det]) -> key for `det`: '/N/' dir and 'DN_spx' token swapped."""
    if det not in INDEXED_DETS:
        raise ValueError(f"D{det} is not indexed (indexed: {INDEXED_DETS})")
    src = config.S3_INDEX_PAIRS[det]
    if det == src:
        return key
    head, fname = key.rsplit("/", 1)
    parent, _ = head.rsplit("/", 1)
    return f"{parent}/{det}/{fname.replace(f'D{src}_spx', f'D{det}_spx')}"


def _find(idx: _Index, ra: float, dec: float, det: int) -> list[ImageRef]:
    r, d = np.deg2rad(ra), np.deg2rad(dec)
    p = np.array([np.cos(d) * np.cos(r), np.cos(d) * np.sin(r), np.sin(d)])
    cand = np.nonzero(idx.cen @ p > np.cos(np.deg2rad(PREFILTER_RADIUS_DEG)))[0]
    if cand.size == 0:
        return []
    hit = cand[contains(ra, dec, idx.ra[cand], idx.dec[cand])]
    refs = []
    for i in hit:
        k = idx.rows[i]
        p = parse_key(k)
        release, exposure_id, version, mjd_beg = p["release"], p["exposure_id"], p["version"], idx.mjd_beg[i]
        key = derive_key(k, det)
        refs.append(
            ImageRef(
                obs_id=f"{exposure_id}_D{det}",
                exposure_id=exposure_id,
                detector=det,
                t_mjd=float(mjd_beg),
                release=release,
                version=version,
                uri=key,
            )
        )
    return sorted(_dedupe(refs), key=lambda x: x.t_mjd)


async def find_images(ra: float, dec: float, det: int) -> list[ImageRef]:
    """All exposures whose footprint contains (ra, dec), deduped (C2), sorted by t_mjd."""
    if det not in INDEXED_DETS:
        raise ValueError(f"D{det} is not indexed (indexed: {INDEXED_DETS})")
    idx = await asyncio.to_thread(_load, config.S3_INDEX_PATHS[config.S3_INDEX_PAIRS[det]])
    return _find(idx, ra, dec, det)


def data_version() -> str:
    return "sprint-2026-11-15-b"  # constant; bump manually. -b: TRANSIENT/SUR_ERROR no longer masked (2026-11-14)
