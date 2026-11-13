"""Synthetic end-to-end test: a star moving 3 px/epoch must be detected and linked.
Run: python -m pytest tests -q   (no network needed)"""
import numpy as np
import pandas as pd
from astropy.coordinates import SkyCoord
from astropy.wcs import WCS

from core import detect, imaging
from core.data import PIX_ARCSEC, Target, assign_epochs, clean_frames, select_frames


def fake_frame(epoch, band, mjd, mover_xy, n=80, seed=0):
    rng = np.random.default_rng(seed)
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [133.795, -7.245]
    w.wcs.crpix = [n / 2, n / 2]
    w.wcs.cdelt = [-PIX_ARCSEC / 3600, PIX_ARCSEC / 3600]
    yy, xx = np.indices((n, n))
    img = rng.normal(0, 1, (n, n))
    for sx, sy in [(20, 20), (60, 55)]:  # static stars
        img += 40 * np.exp(-((xx - sx) ** 2 + (yy - sy) ** 2) / 2)
    mx, my = mover_xy
    img += 30 * np.exp(-((xx - mx) ** 2 + (yy - my) ** 2) / 2)
    return dict(img=img.astype("f4"), var=np.ones((n, n), "f4"), header=w.to_header().tostring(),
                band=band, epoch=epoch, mjd=mjd, wave=1.5, flag_frac=0.0, obs_id=f"e{epoch}{band}")


def build():
    frames = []
    for e, mjd in enumerate([60800.0, 60985.0, 61170.0]):
        for b in ("D2", "D3"):
            frames.append(fake_frame(e, b, mjd, (30 + 6 * e, 40), seed=e * 10 + int(b[1])))
    center = SkyCoord(133.795, -7.245, unit="deg")
    w, shape = imaging.target_grid(center, 80 * PIX_ARCSEC / 3600)
    for f in frames:
        f["grid"] = imaging.regrid(f, w, shape)
    return frames, w


def test_cube_and_detection():
    frames, w = build()
    cube, labels, mjds = imaging.build_cube(frames, "deep")
    assert cube.shape[0] == 3
    m_cube, _, _ = imaging.build_cube(frames, "matched", "D2")
    assert m_cube.shape[0] == 3
    raw, rl, _ = imaging.build_cube(frames, "raw")
    assert raw.shape[0] == 6 and "µm" in rl[0]
    diff = imaging.difference_cube(cube)
    det = detect.detect_all(diff, mjds, w, k=5)
    assert (det["sign"] > 0).sum() >= 3
    tracks = detect.link_tracks(det, max_rate_px_per_day=0.1)
    assert len(tracks) >= 1
    best = tracks.sort_values("points", ascending=False).iloc[0]
    assert best["points"] == 3
    # 6 px per ~185 days
    assert abs(best["rate_arcsec_per_day"] - 6 * PIX_ARCSEC / 185) < 0.05
    out = detect.classify(tracks)
    assert "classification" in out


def test_static_stars_cancel():
    frames, w = build()
    cube, _, _ = imaging.build_cube(frames, "deep")
    diff = imaging.difference_cube(cube)
    det = detect.detect_all(diff, [0, 1, 2], w, k=5)
    # nothing should be detected at the static star positions (~20,20) once regridded
    near = det[np.hypot(det["x"] - det["x"].min() * 0 - 21, det["y"] - 21) < 2]
    assert len(near) == 0


def test_photometry_and_gif():
    frames, _ = build()
    w = imaging.frame_wcs(frames[0])
    c = w.pixel_to_world(20, 20)
    flux, err = imaging.aperture_phot(frames[0], c)
    assert flux > 100 and err > 0
    cube, _, _ = imaging.build_cube(frames, "deep")
    gif = imaging.to_gif(cube, -2, 10)
    assert gif[:3] == b"GIF"


def test_target_motion_and_epochs():
    t = Target("Barnard", 269.452, 4.6934, -801.6, 10362.4, 2000.0)
    shift = t.at(2025.9).separation(t.at(2000.0)).arcsec
    assert 255 < shift < 275
    ep = assign_epochs(np.array([60800, 60801, 60990, 60995, 61180]))
    assert list(ep) == [0, 0, 1, 1, 2]


def test_clean_frames_schema():
    df = pd.DataFrame({
        "access_url": ["https://x/level2_2025W19_2B_0073_2D3_spx_l2b.fits",
                       "https://x/gain_matrix.fits"],
        "energy_bandpassname": ["SPHEREx-D3", "SPHEREx-D3"], "t_min": [60800.1, 60800.2],
        "em_min": [1.7e-6, 1.7e-6], "em_max": [1.8e-6, 1.8e-6], "dataproduct_type": ["image", "image"],
        "obs_id": ["a", "b"]})
    out = clean_frames(df)
    assert len(out) == 1 and out["band"].iloc[0] == "D3" and abs(out["lam_lo"].iloc[0] - 1.7) < 1e-6
    out["epoch"] = 0
    assert len(select_frames(out, ["D3"], 2)) == 1
