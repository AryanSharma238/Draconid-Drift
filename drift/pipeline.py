"""End-to-end pipeline: coordinates -> EC2/S3 frames -> clean cube -> verdict -> player payload.

Data fetching uses:
1. Local `.npz` cube cache (instant <5 ms warm hit)
2. EC2 backend tunnel (`http://127.0.0.1:8000/api/cube` in `us-east-1` with S3 VPC Endpoint)
3. Direct local `backend.sequence.load_frames` (local S3 footprint index + parallel HTTP/2 S3 box range reads)
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import time
from dataclasses import dataclass

import httpx
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

from backend import config
from backend.models import make_grid as backend_make_grid
from backend.sequence import load_frames as backend_load_frames

from .clean import Cube, flatten, frames_to_cube, select_same_colour
from .demo import MID_EPOCH, synthetic_raw
from .player_data import build_payload
from .targets import Target, mjd_to_year, propagate
from .verdict import assess

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "cache")
EC2_BACKEND_URL = os.environ.get("SPHEREX_BACKEND_URL", "http://127.0.0.1:8000")

DETECTORS = [f"SPHEREx-D{d}" for d in config.DETECTORS]
DETECTOR_RANGE = {
    "SPHEREx-D1": "0.75–1.11",
    "SPHEREx-D2": "1.11–1.63",
    "SPHEREx-D3": "1.63–2.42",
    "SPHEREx-D4": "2.42–3.82",
    "SPHEREx-D5": "3.82–4.42",
    "SPHEREx-D6": "4.42–5.00",
}


class DataFetchError(RuntimeError):
    """Raised when neither EC2 backend nor S3 footprint index can return frames."""


@dataclass
class Spec:
    name: str
    ra: float
    dec: float
    pmra: float = 0.0
    pmdec: float = 0.0
    epoch: float = 2016.0
    fov_arcsec: float = 240.0
    detector: str = "SPHEREx-D2"
    key: str = "custom"

    def as_target(self):
        return Target(self.key, self.name, self.ra, self.dec, self.pmra, self.pmdec,
                      self.epoch, self.fov_arcsec, self.detector, "")


def spec_from_target(t: Target):
    return Spec(t.name, t.ra, t.dec, t.pmra, t.pmdec, t.epoch, t.fov_arcsec, t.detector, t.key)


def field_center(spec: Spec):
    """Field centre = target position moved to the middle of the SPHEREx mission."""
    return propagate(spec.ra, spec.dec, spec.pmra, spec.pmdec, spec.epoch, MID_EPOCH)


def _det_int(detector: str) -> int:
    if isinstance(detector, int):
        return detector
    s = str(detector).strip()
    return int(s[-1]) if s and s[-1].isdigit() else 2


# ----------------------------------------------------------------- cache ----
def cache_path(spec: Spec):
    ra, dec = field_center(spec)
    k = f"{spec.key}_{ra:.4f}_{dec:.4f}_{int(spec.fov_arcsec)}_{spec.detector}"
    return os.path.join(CACHE_DIR, hashlib.md5(k.encode()).hexdigest()[:12] + ".npz")


def save_cube(path, cube: Cube):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.savez_compressed(path, cube=cube.cube.astype(np.float32), var=cube.var.astype(np.float32),
                        mjd=cube.mjd, wl=cube.wl, header=cube.wcs.to_header_string(),
                        detector=cube.detector, n_found=cube.n_found)


def load_cube(path):
    z = np.load(path, allow_pickle=False)
    wcs = WCS(fits.Header.fromstring(str(z["header"]), sep=""))
    return Cube(z["cube"].astype(float), z["var"].astype(float), z["mjd"], z["wl"], wcs,
                str(z["detector"]), "CACHE", int(z["n_found"]))


# --------------------------------------------------- EC2 + S3 fetchers ------
_EC2_CLIENT: httpx.Client | None = None


def _get_ec2_client() -> httpx.Client:
    global _EC2_CLIENT
    if _EC2_CLIENT is None:
        _EC2_CLIENT = httpx.Client(timeout=httpx.Timeout(12.0, connect=0.4))
    return _EC2_CLIENT


def _fetch_via_ec2_tunnel(ra: float, dec: float, size_deg: float, det: int, detector_str: str) -> Cube | None:
    """If the EC2 SSH tunnel (`127.0.0.1:8000`) is active and exposes `/api/cube`, fetch from EC2."""
    try:
        client = _get_ec2_client()
        r = client.get(
            f"{EC2_BACKEND_URL}/api/cube",
            params={"ra": ra, "dec": dec, "size": size_deg, "det": det},
        )
        if r.status_code != 200:
            return None
        z = np.load(io.BytesIO(r.content), allow_pickle=False)
        grid = backend_make_grid(float(z["ra"]), float(z["dec"]), float(z["size_deg"]), float(z["pixscale_arcsec"]))
        cube_arr = z["cube"].astype(float)
        var_arr = z["var"].astype(float)
        bad = ~np.isfinite(cube_arr) | ~np.isfinite(var_arr) | (var_arr <= 0)
        cube_arr[bad] = np.nan
        var_arr[bad] = np.nan
        return Cube(
            cube_arr,
            var_arr,
            z["mjd"].astype(float),
            z["wl"].astype(float),
            grid.wcs(),
            detector_str,
            "EC2-S3",
            int(z["n_found"]),
        )
    except Exception:
        return None


def _fetch_via_local_s3_backend(ra: float, dec: float, size_deg: float, det: int, detector_str: str) -> Cube:
    """Run `backend.sequence.load_frames` directly using the local S3 footprint index + S3 byte-range reads."""
    grid = backend_make_grid(ra, dec, size_deg)
    try:
        loaded = asyncio.run(backend_load_frames(grid, det))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            loaded = loop.run_until_complete(backend_load_frames(grid, det))
        finally:
            loop.close()
    except Exception as e:
        raise DataFetchError(f"S3 backend fetch failed: {e}") from e
    if not loaded.frames:
        raise DataFetchError(
            f"No usable SPHEREx {detector_str} frames found at RA={ra:.4f}°, Dec={dec:+.4f}° "
            f"(index matched {len(loaded.refs)} exposures)."
        )
    return frames_to_cube(loaded.frames, grid, detector_str, source="S3-DIRECT", n_found=len(loaded.refs))


# ------------------------------------------------------------------ load ----
def load_frames(spec: Spec, demo=False, use_cache=True, progress=None, seed=0):
    """Returns (Cube, info dict). Order: demo -> local cache -> EC2 tunnel -> local S3 range-read backend."""
    from .clean import build_cube

    t0 = time.perf_counter()
    ra, dec = field_center(spec)
    info = dict(center=(ra, dec), planes=None, elapsed_s=0.0)
    if demo:
        raw, fra, fdec = synthetic_raw(spec.as_target(), spec.detector, fov_arcsec=spec.fov_arcsec,
                                       seed=seed)
        cube = build_cube(raw, fra, fdec, spec.fov_arcsec, spec.detector, "SYNTHETIC", len(raw))
        info["elapsed_s"] = time.perf_counter() - t0
        return cube, info

    path = cache_path(spec)
    if use_cache and os.path.exists(path):
        cube = load_cube(path)
        info["elapsed_s"] = time.perf_counter() - t0
        return cube, info

    size_deg = max(min(spec.fov_arcsec / 3600.0, config.MAX_FOV_DEG), 0.01)
    det_num = _det_int(spec.detector)
    if progress:
        progress(1, 4)

    cube = _fetch_via_ec2_tunnel(ra, dec, size_deg, det_num, spec.detector)
    if cube is None:
        cube = _fetch_via_local_s3_backend(ra, dec, size_deg, det_num, spec.detector)

    if progress:
        progress(4, 4)
    try:
        save_cube(path, cube)
    except OSError:
        pass
    info["elapsed_s"] = time.perf_counter() - t0
    return cube, info


# ------------------------------------------------------------------- run ----
def run(spec: Spec, mode="target", same_colour=False, demo=False, use_cache=True, n_null=300,
        progress=None, null_progress=None, seed=0):
    t_start = time.perf_counter()
    cube, info = load_frames(spec, demo=demo, use_cache=use_cache, progress=progress, seed=seed)
    cube = flatten(cube)
    if same_colour:
        cube = select_same_colour(cube)
    wcs = cube.wcs

    def pos(mjd):
        ra, dec = propagate(spec.ra, spec.dec, spec.pmra, spec.pmdec, spec.epoch,
                            mjd_to_year(mjd))
        x, y = wcs.world_to_pixel_values(ra, dec)
        return float(x), float(y)

    m = float(cube.mjd.mean())
    x0, y0 = pos(m)
    xa, ya = pos(m + 0.5 * 365.25)
    xb, yb = pos(m - 0.5 * 365.25)
    v_exp = (xa - xb, ya - yb)
    ny, nx = cube.cube.shape[1:]
    in_field = 0 <= x0 < nx and 0 <= y0 < ny

    verdict = assess(cube, mode=mode, v0=v_exp, center=(x0, y0), n_null=n_null,
                     seed=seed + 1, progress=null_progress)
    v_tr = (verdict["best"]["vx"], verdict["best"]["vy"]) if mode == "blind" else v_exp
    payload = build_payload(cube, pos if in_field else None, v_exp, v_tr,
                            best_track=verdict.get("best"))

    total_s = time.perf_counter() - t_start
    info["total_s"] = total_s
    notes = [f"Processed in {total_s:.2f}s (fetch: {info.get('elapsed_s', 0.0):.2f}s · source: {cube.source})."]
    ra_c, dec_c = info["center"]
    if abs(spec.epoch - MID_EPOCH) > 1.0 and (spec.pmra or spec.pmdec):
        sh = 3600 * np.hypot((ra_c - spec.ra) * np.cos(np.radians(dec_c)), dec_c - spec.dec)
        notes.append(f"Coordinates are for epoch {spec.epoch:.1f}; moved {sh:.0f}\" "
                     f"({sh / 6.15:.0f} px) to the SPHEREx epoch before searching.")
    if cube.source != "SYNTHETIC":
        notes.append(f"{cube.n} of {cube.n_found or cube.n} {cube.detector} images used "
                     f"(wavelength {np.nanmin(cube.wl):.2f}-{np.nanmax(cube.wl):.2f} µm).")
    return dict(cube=cube, verdict=verdict, payload=payload, notes=notes, info=info,
                v_expected=v_exp, center=(x0, y0), in_field=in_field)
