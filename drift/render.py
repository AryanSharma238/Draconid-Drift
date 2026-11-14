"""Frames -> small PNGs with ONE fixed stretch per view (the key to smooth blinking).

Inverted grayscale (stars dark on white, like WiseView), asinh stretch, the same
lo/hi on every frame of a view, masked pixels shown light blue-grey, north up, east left.
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
    """lo=0 (sky level after offset removal), soft=sigma, hi=max(p99.8, 12 sigma)."""
    stack = np.concatenate([np.asarray(i).ravel() for i in images])
    stack = stack[np.isfinite(stack)]
    sig = robust_sigma(stack)
    hi = max(float(np.percentile(stack, 99.8)) if stack.size else 12 * sig, 12 * sig)
    return dict(lo=0.0, soft=sig, hi=hi)


def to_png_b64(img, p):
    """img: 2-D array in array order (row 0 = FITS y=0 = south); flipped so north is up."""
    a = np.flipud(np.asarray(img, float))
    lo, soft, hi = p["lo"], p["soft"], p["hi"]
    f = np.arcsinh(np.clip(a - lo, 0, None) / soft) / np.arcsinh((hi - lo) / soft)
    g = np.clip(1.0 - np.nan_to_num(f, nan=0.0), 0, 1)
    rgb = np.repeat((g * 255).astype(np.uint8)[..., None], 3, axis=2)
    bad = ~np.isfinite(a)
    rgb[bad] = (206, 214, 235)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def flip_y(y, ny):
    """array row -> display row (north up)."""
    return ny - 1 - y


def to_png_diff_b64(img, lim):
    """Signed difference on white: red = brighter than the reference, blue = dimmer, white = no change.
    Masked pixels grey. Same limit for every image of a view."""
    a = np.flipud(np.asarray(img, float))
    v = np.clip(np.nan_to_num(a, nan=0.0) / lim, -1, 1)
    v = np.sign(v) * np.sqrt(np.abs(v))               # lift faint differences a little
    rgb = np.full(a.shape + (3,), 255, np.uint8)
    pos, neg = v > 0, v < 0
    rgb[pos, 1] = rgb[pos, 2] = (255 * (1 - v[pos])).astype(np.uint8)
    rgb[neg, 0] = rgb[neg, 1] = (255 * (1 + v[neg])).astype(np.uint8)
    rgb[~np.isfinite(a)] = (190, 190, 190)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")
