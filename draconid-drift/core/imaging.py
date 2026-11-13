"""Regridding, wavelength-matched stacking, differencing, photometry, GIF export."""
from __future__ import annotations

import io
import warnings

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS

from .data import PIX_ARCSEC

warnings.filterwarnings("ignore", category=RuntimeWarning)


def target_grid(coord: SkyCoord, size_deg: float, pix_arcsec: float = PIX_ARCSEC):
    """Common north-up TAN grid centred on the target."""
    n = max(15, int(round(size_deg * 3600 / pix_arcsec)))
    n += (n + 1) % 2  # odd so the target sits on a pixel centre
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [coord.ra.deg, coord.dec.deg]
    w.wcs.crpix = [(n + 1) / 2, (n + 1) / 2]
    w.wcs.cdelt = [-pix_arcsec / 3600, pix_arcsec / 3600]
    return w, (n, n)


def fix_sip(hdr: fits.Header) -> fits.Header:
    """SPHEREx headers carry SIP distortion terms without the '-SIP' CTYPE suffix.
    Label them so astropy applies the (real) optical distortion explicitly."""
    if "A_ORDER" in hdr:
        for k in ("CTYPE1", "CTYPE2"):
            if k in hdr and not str(hdr[k]).endswith("-SIP"):
                hdr[k] = str(hdr[k]) + "-SIP"
    return hdr


def frame_wcs(frame: dict) -> WCS:
    if "_wcs" not in frame:
        frame["_wcs"] = WCS(fix_sip(fits.Header.fromstring(frame["header"]))).celestial
    return frame["_wcs"]


def regrid(frame: dict, w_out: WCS, shape) -> np.ndarray:
    from reproject import reproject_interp

    arr, fp = reproject_interp((frame["img"], frame_wcs(frame)), w_out,
                               shape_out=shape, order="bilinear")
    arr = arr.astype("f4")
    arr[(fp < 0.5) | ~np.isfinite(arr)] = np.nan
    return arr


def flatten(img: np.ndarray, size: int = 15) -> np.ndarray:
    """Remove smooth large-scale background (zodi residuals, scattered light) so the
    sky is flat and point sources pop. Stars are far smaller than the filter."""
    from scipy import ndimage
    if min(img.shape) < 2 * size:
        return img
    fill = np.where(np.isfinite(img), img, np.nanmedian(img))
    out = img - ndimage.median_filter(fill, size=size, mode="nearest")
    return out


def robust_norm(img: np.ndarray) -> np.ndarray:
    """Flattened, zero-median, unit robust-sigma image (puts all bands on one scale)."""
    img = flatten(img)
    med = np.nanmedian(img)
    mad = np.nanmedian(np.abs(img - med)) * 1.4826
    if not np.isfinite(mad) or mad == 0:
        mad = np.nanstd(img) or 1.0
    return (img - med) / mad


def build_cube(frames: list[dict], mode: str, band: str | None = None):
    """
    mode = "matched": median of same-band frames per epoch  (no wavelength flicker)
    mode = "deep":    every band normalised and averaged per epoch (max S/N)
    mode = "raw":     every exposure in time order (shows the LVF wavelength trap)
    Returns (cube[N,ny,nx], labels, mjds).
    """
    if not frames:
        return None, [], []
    if mode == "raw":
        fs = sorted(frames, key=lambda f: f["mjd"])
        cube = np.stack([robust_norm(f["grid"]) for f in fs])
        return cube, [f"{f['band']} · {f['wave']:.2f} µm" for f in fs], [f["mjd"] for f in fs]
    epochs = sorted({f["epoch"] for f in frames})
    imgs, labels, mjds = [], [], []
    for ep in epochs:
        fe = [f for f in frames if f["epoch"] == ep]
        if mode == "matched":
            fe = [f for f in fe if f["band"] == band]
            if not fe:
                continue
            img = robust_norm(np.nanmedian(np.stack([f["grid"] for f in fe]), axis=0))
        else:
            per_band = []
            for b in sorted({f["band"] for f in fe}):
                st = np.stack([f["grid"] for f in fe if f["band"] == b])
                per_band.append(robust_norm(np.nanmedian(st, axis=0)))
            img = robust_norm(np.nanmean(np.stack(per_band), axis=0))
        imgs.append(img)
        mjds.append(float(np.mean([f["mjd"] for f in fe])))
        labels.append(f"Epoch {len(imgs)} · {len(fe)} frames")
    if not imgs:
        return None, [], []
    return np.stack(imgs), labels, mjds


def difference_cube(cube: np.ndarray) -> np.ndarray | None:
    """Backyard-Worlds-style: each epoch minus the median of all other epochs."""
    if cube is None or len(cube) < 2:
        return None
    out = []
    for i in range(len(cube)):
        ref = np.nanmedian(np.delete(cube, i, axis=0), axis=0)
        out.append(cube[i] - ref)
    return np.stack(out)


def band_matched_difference(frames: list[dict]):
    """
    Difference each band only against itself (same wavelength range), then average the
    per-band significance maps. This cancels static stars of any colour, unlike a
    mixed-wavelength stack. Returns (diff_cube, template, mjds) or (None, None, []).
    """
    epochs = sorted({f["epoch"] for f in frames})
    if len(epochs) < 2:
        return None, None, []
    diffs, temps = [], []
    for b in sorted({f["band"] for f in frames}):
        cube, _, _ = build_cube(frames, "matched", b)
        if cube is None or len(cube) != len(epochs):
            continue  # band missing at some epoch
        d = difference_cube(cube)
        diffs.append(np.stack([robust_norm(x) for x in d]))
        temps.append(np.nanmedian(cube, axis=0))
    if not diffs:
        base, _, _ = build_cube(frames, "deep")
        d = difference_cube(base)
        temp = np.nanmedian(base, axis=0)
    else:
        n = len(diffs)
        d = np.nansum(np.stack(diffs), axis=0) / np.sqrt(n)  # stacked significance
        temp = np.nanmean(np.stack(temps), axis=0)
    mjds = [float(np.mean([f["mjd"] for f in frames if f["epoch"] == e])) for e in epochs]
    return d, temp, mjds


def centroid(img: np.ndarray, x0: float, y0: float, box: int = 3):
    """Background-subtracted flux-weighted centroid within ±box px of (x0, y0)."""
    if not (np.isfinite(x0) and np.isfinite(y0)):
        return np.nan, np.nan, 0.0
    ny, nx = img.shape
    xi, yi = int(round(x0)), int(round(y0))
    x1, x2, y1, y2 = max(xi - box, 0), min(xi + box + 1, nx), max(yi - box, 0), min(yi + box + 1, ny)
    sub = np.nan_to_num(img[y1:y2, x1:x2], nan=0.0)
    if sub.size == 0:
        return np.nan, np.nan, 0.0
    sub = sub - np.median(img[np.isfinite(img)])
    sub[sub < 0] = 0
    tot = sub.sum()
    if tot <= 0:
        return np.nan, np.nan, 0.0
    yy, xx = np.mgrid[y1:y2, x1:x2]
    return float((xx * sub).sum() / tot), float((yy * sub).sum() / tot), float(sub.max())


def measure_motion(cube: np.ndarray, mjds, w, guess_xy, pix_arcsec: float = PIX_ARCSEC):
    """
    Sub-pixel astrometry of one source across epochs -> proper motion (mas/yr).
    guess_xy: list of predicted (x, y) per epoch. Returns dict or None.
    """
    pts = []
    for img, m, (gx, gy) in zip(cube, mjds, guess_xy):
        x, y, peak = centroid(img, gx, gy)
        if np.isfinite(x) and peak > 3:
            pts.append((m, x, y, peak))
    if len(pts) < 2:
        return None
    m, x, y, pk = map(np.array, zip(*pts))
    yrs = (m - m[0]) / 365.25
    if yrs[-1] - yrs[0] < 0.1:
        return None
    sky = w.pixel_to_world(x, y)
    ra0, dec0 = sky.ra.deg[0], sky.dec.deg[0]
    dx = (sky.ra.deg - ra0) * np.cos(np.radians(dec0)) * 3.6e6   # mas (east)
    dy = (sky.dec.deg - dec0) * 3.6e6                              # mas (north)
    pmra = np.polyfit(yrs, dx, 1)[0]
    pmdec = np.polyfit(yrs, dy, 1)[0]
    # Centroid precision ~ FWHM / SNR; FWHM ~ 1.5 px for SPHEREx-like PSFs.
    sig = 1.5 * pix_arcsec * 1000 / np.maximum(pk, 1)
    sig = np.hypot(sig, 0.15 * pix_arcsec * 1000)  # 0.15 px systematic floor (PSF/regridding)
    err = float(np.sqrt(2) * np.median(sig) / max(yrs[-1] - yrs[0], 0.1))
    return dict(pmra=float(pmra), pmdec=float(pmdec), total=float(np.hypot(pmra, pmdec)),
                err=err, n=len(pts), baseline_yr=float(yrs[-1] - yrs[0]),
                xs=x.tolist(), ys=y.tolist())


def limits(cube: np.ndarray, lo=1.0, hi=99.5):
    v = cube[np.isfinite(cube)]
    if v.size == 0:
        return -1.0, 1.0
    return float(np.percentile(v, lo)), float(np.percentile(v, hi))


def sym_limits(cube: np.ndarray, p=99.0):
    v = np.abs(cube[np.isfinite(cube)])
    m = float(np.percentile(v, p)) if v.size else 1.0
    return -m, m


def aperture_phot(frame: dict, coord: SkyCoord, r=2.0, rin=4.0, rout=7.0):
    """Background-subtracted aperture sum at a sky position (native pixels)."""
    img = frame["img"]
    try:
        x, y = frame_wcs(frame).world_to_pixel(coord)
    except Exception:
        return np.nan, np.nan
    yy, xx = np.indices(img.shape)
    d = np.hypot(xx - x, yy - y)
    ap, an = d <= r, (d >= rin) & (d <= rout)
    good = np.isfinite(img)
    if ap.sum() == 0 or not good[ap].any() or not np.isfinite(x) or not np.isfinite(y):
        return np.nan, np.nan
    bg = np.nanmedian(img[an & good]) if (an & good).any() else 0.0
    flux = float(np.nansum(img[ap & good] - bg))
    var = frame.get("var")
    err = float(np.sqrt(np.nansum(var[ap & good]))) if var is not None else np.nan
    return flux, err


def upsample(cube: np.ndarray, factor: int = 4):
    """Cubic-spline upsampling for presentation: stars render as smooth, sharp points
    instead of 6-arcsec blocks. Returns (cube, x_coords, y_coords) where the coords are
    in ORIGINAL pixel units, so every overlay still lines up exactly."""
    from scipy import ndimage
    cube = np.asarray(cube, dtype="f4")
    mask = ~np.isfinite(cube)
    fill = np.where(mask, np.nanmedian(cube), cube)
    up = ndimage.zoom(fill, (1, factor, factor), order=3, mode="nearest", grid_mode=True)
    upm = ndimage.zoom(mask.astype("f4"), (1, factor, factor), order=0, mode="nearest", grid_mode=True)
    up[upm > 0.5] = np.nan
    ny, nx = cube.shape[1:]
    xs = (np.arange(nx * factor) + 0.5) / factor - 0.5
    ys = (np.arange(ny * factor) + 0.5) / factor - 0.5
    return up, xs, ys


def asinh_stretch(cube: np.ndarray, zmin: float, zmax: float, soft: float = 0.08) -> np.ndarray:
    """Astronomer's asinh stretch: faint detail visible, bright cores not blown out. Output 0..1."""
    a = np.clip((cube - zmin) / (zmax - zmin + 1e-9), 0, 1)
    return np.arcsinh(a / soft) / np.arcsinh(1 / soft)


def to_gif(cube: np.ndarray, zmin: float, zmax: float, scale: int = 8, ms: int = 600) -> bytes:
    """Crisp GIF: asinh stretch + nearest-neighbour upscaling (true pixels, no blur)."""
    from PIL import Image

    frames = []
    for img in cube:
        a = asinh_stretch(np.nan_to_num(img, nan=zmin), zmin, zmax)
        a = (np.flipud(a) * 255).astype("uint8")  # FITS row 0 is the bottom
        im = Image.fromarray(a).resize((a.shape[1] * scale, a.shape[0] * scale), Image.NEAREST)
        frames.append(im)
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=ms, loop=0)
    return buf.getvalue()


# ── Colour composites (same recipe as the hero image) ────────────────────────
COLOR_GROUPS = (("D5", "D6"), ("D3", "D4"), ("D1", "D2"))  # R = long, G = mid, B = short λ


def _soft_flatten(img: np.ndarray) -> np.ndarray:
    """Remove only very large-scale background, keeping nebular glow."""
    from scipy import ndimage
    fill = np.where(np.isfinite(img), img, np.nanmedian(img))
    b = max(1, min(img.shape) // 48)                       # bin so the filter stays fast
    ny, nx = (fill.shape[0] // b) * b, (fill.shape[1] // b) * b
    small = np.median(fill[:ny, :nx].reshape(ny // b, b, nx // b, b), axis=(1, 3))
    bg_small = ndimage.median_filter(small, size=max(5, min(small.shape) // 3), mode="nearest")
    bg = ndimage.zoom(bg_small, (fill.shape[0] / bg_small.shape[0], fill.shape[1] / bg_small.shape[1]), order=1)
    return img - bg[: img.shape[0], : img.shape[1]]


def color_cube(frames: list[dict], soft: float = 0.035):
    """
    Per visit: R/G/B = median of long/mid/short-wavelength bands. One stretch per channel is
    shared by all visits, so brightness changes between frames are real, not re-scaling.
    Returns (rgb[N,ny,nx,3] in 0..1, labels, mjds) or (None, [], []).
    """
    epochs = sorted({f["epoch"] for f in frames})
    chans = []  # [channel][epoch] -> image
    for group in COLOR_GROUPS:
        per_ep = []
        for ep in epochs:
            st = [f["grid"] for f in frames if f["epoch"] == ep and f["band"] in group]
            per_ep.append(_soft_flatten(np.nanmedian(np.stack(st), axis=0)) if st else None)
        chans.append(per_ep)
    if all(im is None for ch in chans for im in ch):
        return None, [], []
    shape = next(im for ch in chans for im in ch if im is not None).shape
    out = np.zeros((len(epochs), *shape, 3), "f4")
    for c, per_ep in enumerate(chans):
        vals = np.concatenate([im[np.isfinite(im)].ravel() for im in per_ep if im is not None] or [np.zeros(1)])
        lo, hi = np.percentile(vals, [35, 99.6]) if vals.size > 10 else (0, 1)
        for e, im in enumerate(per_ep):
            if im is None:
                out[e, ..., c] = np.nan
                continue
            a = np.clip((im - lo) / (hi - lo + 1e-9), 0, 1)
            out[e, ..., c] = np.arcsinh(a / soft) / np.arcsinh(1 / soft)
    # A channel missing in a visit borrows the mean of the others (keeps colour neutral, no seams).
    mean = np.nanmean(out, axis=-1, keepdims=True)
    out = np.where(np.isfinite(out), out, mean)
    out = np.nan_to_num(out, nan=0.0)
    mjds = [float(np.mean([f["mjd"] for f in frames if f["epoch"] == e])) for e in epochs]
    labels = [f"Visit {i + 1}" for i in range(len(epochs))]
    return out, labels, mjds


def upsample_rgb(rgb: np.ndarray, factor: int = 4):
    """Spline-upsample an RGB cube [N,ny,nx,3]; coords returned in native pixel units."""
    chans = [upsample(rgb[..., c], factor) for c in range(3)]
    up = np.clip(np.stack([c[0] for c in chans], axis=-1), 0, 1)
    return np.nan_to_num(up), chans[0][1], chans[0][2]


def to_gif_rgb(rgb: np.ndarray, scale: int = 6, ms: int = 650) -> bytes:
    from PIL import Image
    frames = []
    for im in rgb:
        a = (np.flipud(np.clip(im, 0, 1)) * 255).astype("uint8")
        frames.append(Image.fromarray(a).resize((a.shape[1] * scale, a.shape[0] * scale), Image.LANCZOS))
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=ms, loop=0)
    return buf.getvalue()


# ── Smooth playback helpers ──────────────────────────────────────────────────
def colorize(cube01: np.ndarray, stops) -> np.ndarray:
    """Map a [N,ny,nx] cube in 0..1 through colour stops [(pos, '#rrggbb'), ...] -> RGB 0..1."""
    pos = np.array([p for p, _ in stops])
    cols = np.array([[int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)] for _, c in stops])
    x = np.clip(np.nan_to_num(cube01, nan=0.0), 0, 1)
    return np.stack([np.interp(x, pos, cols[:, k]) for k in range(3)], axis=-1)


def png_uri(rgb01: np.ndarray) -> str:
    """Compressed PNG data URI. Row 0 of the array is drawn at the bottom (north up)."""
    import base64
    from PIL import Image
    a = (np.flipud(np.clip(np.nan_to_num(rgb01), 0, 1)) * 255).astype("uint8")
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def tween(frames: list, mjds: list, hold: int = 6, steps: int = 10):
    """Smooth crossfade timeline: hold each visit, then ease into the next.
    Returns list of (rgb, mjd, key_index or None)."""
    out = []
    n = len(frames)
    for i in range(n):
        out += [(frames[i], mjds[i], i)] * hold
        j = (i + 1) % n                       # loop back smoothly to the first visit
        for s in range(1, steps):
            t = s / steps
            e = t * t * (3 - 2 * t)           # smoothstep easing
            mjd = mjds[i] + (mjds[j] - mjds[i]) * e if j > i else mjds[i]
            out.append(((1 - e) * frames[i] + e * frames[j], mjd, None))
    return out
