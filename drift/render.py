"""Frames -> small PNGs with ONE fixed stretch per view (the key to smooth blinking).

Includes saturated-core reconstruction so bright stars don't show hollow flagged holes
at their peaks, plus a normalized intensity array [0..255] per frame so the client canvas
can apply false-color scientific colormaps, contrast/bias sliders, and sub-pixel rendering
in real time without re-running Python.
"""
from __future__ import annotations

import base64
import io

import numpy as np
from PIL import Image


def robust_sigma(a):
    a = a[np.isfinite(a)]
    if a.size == 0:
        return 1.0
    return float(1.4826 * np.median(np.abs(a - np.median(a)))) or 1.0


def stretch_params(images):
    """lo=-0.35*sigma (preserves faint sky texture), soft=sigma, hi=max(p99.6, 10 sigma)."""
    stack = np.concatenate([np.asarray(i).ravel() for i in images])
    stack = stack[np.isfinite(stack)]
    sig = robust_sigma(stack)
    hi = max(float(np.percentile(stack, 99.6)) if stack.size else 10 * sig, 10 * sig)
    return dict(lo=-0.35 * sig, soft=sig, hi=hi, sig=sig)


def reconstruct_saturated_cores(img, sig):
    """Reconstruct NaN pixels that sit inside the saturated core of a bright star
    (surrounded by >= 3 finite 3x3 neighbors brighter than 5*sigma), while leaving
    genuine bad detector pixels/columns as NaN."""
    a = np.array(img, float).copy()
    orig_bad = ~np.isfinite(a)
    if not orig_bad.any():
        return a, np.zeros_like(orig_bad), orig_bad
    for _ in range(3):
        cur_bad = ~np.isfinite(a)
        if not cur_bad.any():
            break
        pad = np.pad(a, 1, constant_values=np.nan)
        neigh = np.stack([pad[dy:dy + a.shape[0], dx:dx + a.shape[1]]
                          for dy in range(3) for dx in range(3) if not (dy == 1 and dx == 1)], axis=0)
        n_bright = (np.isfinite(neigh) & (neigh > 5.0 * sig)).sum(axis=0)
        with np.errstate(all="ignore"):
            n_max = np.nanmax(np.where(np.isfinite(neigh), neigh, -np.inf), axis=0)
        sat = cur_bad & (n_bright >= 3) & np.isfinite(n_max)
        if not sat.any():
            break
        a[sat] = n_max[sat] * 1.15
    rem_bad = ~np.isfinite(a)
    sat_core = orig_bad & ~rem_bad
    return a, sat_core, rem_bad


def normalize_frame(img, p):
    """Return (norm_u8, bad_mask, sat_mask) in display order (row 0 = north up).
    norm_u8 is 0..255 where 0 = sky background (lo) and 255 = bright peak (hi)."""
    raw_flipped = np.flipud(np.asarray(img, float))
    sig = p.get("sig", p["soft"])
    a, sat_core, bad = reconstruct_saturated_cores(raw_flipped, sig)
    lo, soft, hi = p["lo"], p["soft"], p["hi"]
    denom = float(np.arcsinh(max((hi - lo) / soft, 1e-6)))
    f = np.arcsinh(np.clip(a - lo, 0, None) / soft) / denom
    f = np.clip(np.nan_to_num(f, nan=0.0), 0.0, 1.0)
    f[sat_core] = 1.0
    u8 = np.rint(f * 255.0).astype(np.uint8)
    return u8, bad, sat_core


def to_png_b64(img, p):
    """img: 2-D array in array order (row 0 = FITS y=0 = south); flipped so north is up.
    Encodes normalized intensity in R, bad-pixel mask in G (255=valid, 0=flagged),
    and saturated-core reconstruction mask in B (255=reconstructed core, 0=normal),
    while remaining a valid PNG that the JS canvas lut engine decodes losslessly."""
    u8, bad, _ = normalize_frame(img, p)
    g = 255 - u8
    rgb = np.repeat(g[..., None], 3, axis=2)
    rgb[bad] = (206, 214, 235)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def to_raw_b64(img, p):
    """Compact base64 of (u8_intensity, mask_byte) where mask_byte: 0=valid, 1=bad_pixel, 2=sat_core."""
    u8, bad, sat = normalize_frame(img, p)
    mask = np.zeros_like(u8)
    mask[bad] = 1
    mask[sat] = 2
    interleaved = np.stack([u8, mask], axis=-1).tobytes()
    return base64.b64encode(interleaved).decode("ascii")


def to_diff_raw_b64(img, sig):
    """Contract C9 diverging stretch for difference/residual images:
    symmetric limits ±5*sig mapped to 0..255 where 128 = 0.0 (black),
    0 = -5*sig (negative ghost = blue), 255 = +5*sig (positive source = orange)."""
    a = np.flipud(np.asarray(img, float))
    bad = ~np.isfinite(a)
    lim = max(5.0 * float(sig), 1e-6)
    v = np.clip(np.nan_to_num(a, nan=0.0) / lim, -1.0, 1.0)
    u8 = np.rint((v + 1.0) * 127.5).astype(np.uint8)
    mask = np.zeros_like(u8)
    mask[bad] = 1
    interleaved = np.stack([u8, mask], axis=-1).tobytes()
    return base64.b64encode(interleaved).decode("ascii")


def flip_y(y, ny):
    """array row -> display row (north up)."""
    return ny - 1 - y
