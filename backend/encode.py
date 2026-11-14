"""Frame -> NDJSON lines (contract C5)."""

import base64
import logging

import numpy as np
import orjson
from astropy.time import Time

from backend.models import Frame, Grid

log = logging.getLogger("encode")

F16_CLIP = 60000.0  # float16 max is 65504; beyond that values become inf


def b64_f16(a: np.ndarray) -> str:
    """flipud (row0 = north, contract C4) -> float16 little-endian -> base64.

    Values beyond +-F16_CLIP are clipped (with a warning); NaN is preserved.
    """
    a = np.asarray(a, dtype=np.float64)
    over = np.isfinite(a) & (np.abs(a) > F16_CLIP)
    if over.any():
        log.warning("clipping %d pixels beyond +-%g before float16 encode", int(over.sum()), F16_CLIP)
        a = np.where(np.isfinite(a), np.clip(a, -F16_CLIP, F16_CLIP), a)
    return base64.b64encode(np.flipud(a).astype("<f2").tobytes()).decode("ascii")


def b64_u32(a: np.ndarray) -> str:
    return base64.b64encode(np.flipud(a).astype("<u4").tobytes()).decode("ascii")


def _line(obj: dict) -> bytes:
    return orjson.dumps(obj) + b"\n"


def header_line(
    grid: Grid,
    det: int,
    mode: str,
    n_expected: int,
    data_version: str,
    source: str,
    zscale: tuple[float, float] | list[float] | None,
) -> bytes:
    return _line(
        {
            "type": "header",
            "v": 1,
            "ra": grid.ra,
            "dec": grid.dec,
            "size_deg": grid.size_deg,
            "npix": grid.npix,
            "pixscale_arcsec": grid.pixscale_arcsec,
            "det": det,
            "mode": mode,
            "n_expected": n_expected,
            "data_version": data_version,
            "source": source,
            "zscale": None if zscale is None else [float(zscale[0]), float(zscale[1])],
        }
    )


def frame_line(i: int, f: Frame, include_flags: bool, n_coadd: int = 1) -> bytes:
    """`kind` is "pass" for co-added frames (obs_id "pass{p}_D{det}"), else "single".

    `n_coadd` is not stored on Frame, so the caller supplies it for pass frames.
    """
    is_pass = f.ref.obs_id.startswith("pass")
    obj = {
        "type": "frame",
        "i": i,
        "obs_id": f.ref.obs_id,
        "t_mjd": float(f.ref.t_mjd),
        "date": Time(f.ref.t_mjd, format="mjd").isot[:19] + "Z",
        "det": f.ref.detector,
        "lambda_um": float(f.lambda_um),
        "pass": int(f.pass_idx),
        "release": f.ref.release,
        "n_coadd": int(n_coadd),
        "kind": "pass" if is_pass else "single",
        "data": b64_f16(f.data),
    }
    if include_flags:
        obj["flags"] = b64_u32(f.flags)
    return _line(obj)


def zscale_line(lo: float, hi: float) -> bytes:
    return _line({"type": "zscale", "lo": float(lo), "hi": float(hi)})


def error_line(obs_id: str, reason: str) -> bytes:
    return _line({"type": "error", "obs_id": obs_id, "reason": reason})


def done_line(n_frames: int, n_failed: int, elapsed_ms: int) -> bytes:
    return _line({"type": "done", "n_frames": n_frames, "n_failed": n_failed, "elapsed_ms": elapsed_ms})
