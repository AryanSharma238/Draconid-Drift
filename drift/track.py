"""Matched-filter track search and shift-and-add for the empirical FAP verdict.

Per frame i (variance V_i, PSF p):  A_i = corr(I_i / V_i, p),  B_i = corr(1 / V_i, p^2).
A candidate track starting at pixel (x, y) at the mean epoch and moving at v (px/yr)
visits (x + v_x dt_i, y + v_y dt_i). Its signal-to-noise is
        nu = sum_i A_i(track) / sqrt( sum_i B_i(track) ).
Lambda-matched residuals, multi-pass astrometry, and aperture photometry live in
`backend.motion` and `backend.photometry`.
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import maximum_filter
from scipy.ndimage import shift as ndshift
from scipy.signal import fftconvolve

PSF_SIGMA_PIX = 0.8   # effective Gaussian sigma (PSF ~3-6" FWHM, undersampled, + reprojection)
CLIP_NU = 6.0         # per-frame S/N is clipped at +/-6 so one bright star in one frame
                      # cannot fake a track (a real mover must be there in many frames)


def psf_kernel(sigma=PSF_SIGMA_PIX):
    r = max(int(np.ceil(3 * sigma)), 1)
    y, x = np.mgrid[-r:r + 1, -r:r + 1]
    return np.exp(-(x * x + y * y) / (2 * sigma ** 2))


def frame_maps(cube, var, sigma=PSF_SIGMA_PIX):
    good = np.isfinite(cube) & np.isfinite(var) & (var > 0)
    w = np.where(good, 1.0 / np.where(good, var, 1.0), 0.0)
    z = np.where(good, cube, 0.0)
    p = psf_kernel(sigma)
    A = np.array([fftconvolve(z[i] * w[i], p, mode="same") for i in range(len(cube))])
    B = np.array([fftconvolve(w[i], p * p, mode="same") for i in range(len(cube))])
    return A, B, good


class TrackSearch:
    def __init__(self, A, B, pad):
        self.M = int(pad)
        M = self.M
        self.A = np.pad(A, ((0, 0), (M, M), (M, M)))
        self.B = np.pad(B, ((0, 0), (M, M), (M, M)))
        self.n, self.ny, self.nx = A.shape
        sb = np.sqrt(np.clip(self.B, 0, None))
        r = np.where(sb > 0, self.A / np.where(sb > 0, sb, 1.0), 0.0)
        self.Ac = np.clip(r, -CLIP_NU, CLIP_NU) * sb
        self.f_idx = np.arange(self.n)[:, None, None]

    def nu_region(self, v, dt, region, frames=None):
        """S/N map over region=(y0,y1,x0,x1) for velocity v=(vx,vy) px/yr."""
        y0, y1, x0, x1 = region
        M = self.M
        if frames is not None:
            frames = np.asarray(frames, int)
            dt = dt[frames]
            Ac = self.Ac[frames]
            B = self.B[frames]
            f_idx = np.arange(len(frames))[:, None, None]
        else:
            Ac, B, f_idx = self.Ac, self.B, self.f_idx
        sx = np.rint(v[0] * dt).astype(int)[:, None, None]
        sy = np.rint(v[1] * dt).astype(int)[:, None, None]
        yy = np.arange(y0 + M, y1 + M)[None, :, None] + sy
        xx = np.arange(x0 + M, x1 + M)[None, None, :] + sx
        As = Ac[f_idx, yy, xx].sum(axis=0)
        Bs = B[f_idx, yy, xx].sum(axis=0)
        return np.where(Bs > 0, As / np.sqrt(np.where(Bs > 0, Bs, 1.0)), 0.0)

    def static_map(self, dt):
        """Stationary S/N of every pixel over all frames, grown by 1 px (3x3 maximum),
        stored padded. A track that passes through any such location is 'explained by a star'."""
        nu0 = self.nu_region((0.0, 0.0), dt, (0, self.ny, 0, self.nx))
        F = maximum_filter(nu0, size=3)
        self.F = np.pad(F, self.M)
        self.F_inner = F
        return F

    def static_on_track(self, v, dt, region):
        """Highest stationary S/N met anywhere along the track starting at each pixel."""
        y0, y1, x0, x1 = region
        M = self.M
        out = np.zeros((y1 - y0, x1 - x0))
        sxs = np.rint(v[0] * dt).astype(int)
        sys = np.rint(v[1] * dt).astype(int)
        for sx, sy in set(zip(sxs, sys)):
            ys, xs = y0 + M + sy, x0 + M + sx
            np.maximum(out, self.F[ys:ys + (y1 - y0), xs:xs + (x1 - x0)], out=out)
        return out

    def best_over_grid(self, vgrid, dt, region):
        """max over positions and velocities of T = nu^2 - (best stationary S/N met along
        the track)^2: how much better 'moving' explains the data than 'a star it passes'."""
        best = dict(T=-np.inf)
        y0, y1, x0, x1 = region
        for v in vgrid:
            sx = np.rint(v[0] * dt).astype(int)
            sy = np.rint(v[1] * dt).astype(int)
            ry0, ry1 = max(y0, -int(sy.min())), min(y1, self.ny - int(sy.max()))
            rx0, rx1 = max(x0, -int(sx.min())), min(x1, self.nx - int(sx.max()))
            if ry1 <= ry0 or rx1 <= rx0:
                continue
            reg = (ry0, ry1, rx0, rx1)
            nu = self.nu_region(v, dt, reg)
            so = self.static_on_track(v, dt, reg)
            T = np.maximum(nu, 0.0) ** 2 - so ** 2
            k = np.unravel_index(np.argmax(T), T.shape)
            if T[k] > best["T"]:
                best = dict(T=float(T[k]), nu=float(nu[k]), v=tuple(v),
                            y=ry0 + int(k[0]), x=rx0 + int(k[1]), nu_static=float(so[k]))
        if best["T"] == -np.inf:
            best = dict(T=0.0, nu=0.0, v=(0.0, 0.0), y=y0, x=x0, nu_static=0.0)
        return best

    def nu_at(self, x, y, v, dt, frames=None):
        return float(self.nu_region(v, dt, (y, y + 1, x, x + 1), frames)[0, 0])


def velocity_grid(center, half_width, step):
    k = int(round(half_width / step))
    offs = np.arange(-k, k + 1) * step
    return [(center[0] + a, center[1] + b) for a in offs for b in offs]


def stack_image(cube, var, mjd, v, tracked=True, clip_sigma=4.0):
    """Inverse-variance mean with iterative sigma clipping (Task 09 E3).
    tracked=True shifts each frame back along velocity v (px/yr) so a mover is sharp
    and stars smear; tracked=False is the ordinary stack."""
    dt = (mjd - mjd.mean()) / 365.25
    good = np.isfinite(cube) & np.isfinite(var) & (var > 0)
    w = np.where(good, 1.0 / np.where(good, var, 1.0), 0.0)
    z = np.where(good, np.nan_to_num(cube, nan=0.0), 0.0)
    if tracked:
        z_sh = np.zeros_like(z)
        w_sh = np.zeros_like(w)
        for i in range(len(cube)):
            s = (-v[1] * dt[i], -v[0] * dt[i])     # array order (row=y, col=x)
            a = ndshift(z[i] * w[i], s, order=1, mode="nearest")
            b = ndshift(w[i], s, order=1, mode="nearest")
            w_sh[i] = np.clip(b, 0.0, None)
            z_sh[i] = np.where(w_sh[i] > 1e-12, a / np.where(w_sh[i] > 1e-12, w_sh[i], 1.0), 0.0)
        z, w = z_sh, w_sh
    den = w.sum(axis=0)
    num = (z * w).sum(axis=0)
    mean = np.where(den > 1e-12, num / np.where(den > 1e-12, den, 1.0), np.nan)
    n_valid = (w > 0).sum(axis=0)
    if clip_sigma and len(cube) >= 3:
        for _ in range(2):
            resid = np.abs(z - mean[None, :, :]) * np.sqrt(np.clip(w, 0.0, None))
            keep = (w > 0) & ((resid <= clip_sigma) | (n_valid[None, :, :] < 3))
            any_keep = keep.any(axis=0)
            w_eff = np.where(keep | ~any_keep[None, :, :], w, 0.0)
            den = w_eff.sum(axis=0)
            num = (z * w_eff).sum(axis=0)
            mean = np.where(den > 1e-12, num / np.where(den > 1e-12, den, 1.0), np.nan)
    return mean, den
