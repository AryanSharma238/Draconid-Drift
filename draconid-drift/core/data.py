"""SPHEREx data access: target resolution, IRSA queries, server-side cutouts."""
from __future__ import annotations

import io
import math
import re
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import astropy.units as u
import numpy as np
import pandas as pd
import requests
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS, FITSFixedWarning

warnings.simplefilter("ignore", FITSFixedWarning)

PIX_ARCSEC = 6.15  # SPHEREx native pixel scale (~6.2"/px)
BANDS = {
    "D1": (0.75, 1.11), "D2": (1.11, 1.64), "D3": (1.64, 2.42),
    "D4": (2.42, 3.82), "D5": (3.82, 4.42), "D6": (4.42, 5.00),
}
# DR1 = reprocessed first-year release; QR3 = newest weekly quick-release data.
# Both are merged so the tool always shows the full time baseline. Older names kept as fallbacks.
COLLECTIONS = ("spherex_dr1", "spherex_qr3", "spherex_qr2", "spherex_qr")
TIMEOUT = 90

# Official challenge test targets. Positions + proper motions stored so they work offline
# (catalog values: SIMBAD/Gaia; WISE 0855 from Luhman & Esplin 2016, position epoch ~2010.5).
PRESETS = {
    "WISE 0855": dict(coords="08h55m10.831s -07d14m42.53s", pmra=-8118.0, pmdec=680.0, epoch=2010.5),
    "Barnard's Star": dict(coords="17h57m48.4997s +04d41m36.111s", pmra=-801.551, pmdec=10362.394, epoch=2000.0),
    "ε Indi B": dict(coords="22h04m10.52s -56d46m57.7s", pmra=3966.7, pmdec=-2536.2, epoch=2000.0),
    "RW Aurigae": dict(coords="05h07m49.54s +30d24m05.1s", pmra=0.0, pmdec=0.0, epoch=2000.0),
    "R Draconis": dict(coords="16h32m40.18s +66d45m18.2s", pmra=0.0, pmdec=0.0, epoch=2000.0),
}


def preset_target(name: str) -> Target:
    p = PRESETS[name]
    c = SkyCoord(p["coords"], unit=(u.hourangle, u.deg))
    return Target(name, c.ra.deg, c.dec.deg, p["pmra"], p["pmdec"], p["epoch"], "built-in catalog values")


@dataclass
class Target:
    name: str
    ra: float          # deg, at ref_epoch
    dec: float         # deg, at ref_epoch
    pmra: float = 0.0  # mas/yr (mu_alpha*, includes cos dec)
    pmdec: float = 0.0 # mas/yr
    ref_epoch: float = 2000.0
    source: str = "coordinates"

    def at(self, year: float) -> SkyCoord:
        """Position propagated to a Julian year (linear proper motion)."""
        dt = year - self.ref_epoch
        dec = self.dec + self.pmdec * dt / 3.6e6
        ra = self.ra + self.pmra * dt / 3.6e6 / max(math.cos(math.radians(self.dec)), 1e-6)
        return SkyCoord(ra % 360, dec, unit="deg")

    @property
    def total_pm(self) -> float:
        return float(math.hypot(self.pmra, self.pmdec))


def _f(v) -> float:
    try:
        x = float(v)
        return 0.0 if not np.isfinite(x) else x
    except Exception:
        return 0.0


def _parse_coords(text: str) -> SkyCoord | None:
    parts = text.replace(",", " ").split()
    try:
        if len(parts) == 2 and all(re.fullmatch(r"[-+]?\d+(\.\d+)?", p) for p in parts):
            return SkyCoord(float(parts[0]), float(parts[1]), unit="deg")
        return SkyCoord(text, unit=(u.hourangle, u.deg))
    except Exception:
        return None


def resolve_target(text: str, fallback: str | None = None) -> Target:
    """Coordinates, or a name via SIMBAD (with proper motion), or Sesame."""
    text = text.strip()
    c = _parse_coords(text)
    if c is not None:
        return Target(text, c.ra.deg, c.dec.deg)
    try:
        from astroquery.simbad import Simbad

        s = Simbad()
        s.add_votable_fields("pmra", "pmdec")
        r = s.query_object(text)
        if r is not None and len(r):
            cols = {n.lower(): n for n in r.colnames}
            ra, dec = r[cols["ra"]][0], r[cols["dec"]][0]
            if isinstance(ra, (str, bytes)):
                sc = SkyCoord(str(ra), str(dec), unit=(u.hourangle, u.deg))
                ra, dec = sc.ra.deg, sc.dec.deg
            return Target(text, float(ra), float(dec),
                          _f(r[cols["pmra"]][0]) if "pmra" in cols else 0.0,
                          _f(r[cols["pmdec"]][0]) if "pmdec" in cols else 0.0,
                          2000.0, "SIMBAD")
    except Exception:
        pass
    try:
        sc = SkyCoord.from_name(text)
        return Target(text, sc.ra.deg, sc.dec.deg, source="Sesame")
    except Exception:
        pass
    if fallback:
        c = _parse_coords(fallback)
        if c is not None:
            return Target(text, c.ra.deg, c.dec.deg, source="fallback coordinates")
    raise ValueError(f"Could not resolve “{text}”. Try RA/Dec, e.g. 08h55m10.8s -07d14m42s.")


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def _band_of(row) -> str | None:
    for key in ("energy_bandpassname", "bandpass", "band"):
        if key in row and isinstance(row[key], (str, bytes)):
            m = re.search(r"D(\d)", _s(row[key]))
            if m:
                return f"D{m.group(1)}"
    m = re.search(r"D(\d)_spx", _s(row.get("access_url", "")))
    return f"D{m.group(1)}" if m else None


def query_frames(coord: SkyCoord) -> pd.DataFrame:
    """All SPHEREx spectral images covering a position, oldest first."""
    from astroquery.ipac.irsa import Irsa

    parts, last = [], None
    for col in COLLECTIONS:
        try:
            tbl = Irsa.query_sia(pos=(coord, 2 * u.arcsec), collection=col)
        except Exception as e:  # noqa: BLE001
            last = e
            continue
        if tbl is None or len(tbl) == 0:
            continue
        out = clean_frames(tbl.to_pandas())
        if len(out):
            out["collection"] = col
            parts.append(out)
    if not parts:
        if last:
            raise RuntimeError(f"IRSA query failed: {last}")
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    # Same exposure can appear in several releases: keep the first (best-processed) copy.
    df = df.drop_duplicates(subset=["obs_id", "band"], keep="first")
    return df.sort_values("mjd").reset_index(drop=True)


def clean_frames(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["access_url"] = df["access_url"].map(_s)
    if "dataproduct_type" in df:
        df = df[df["dataproduct_type"].map(_s).str.contains("image", na=False)]
    df = df[df["access_url"].str.contains(r"\.fits", na=False)]
    df = df[~df["access_url"].str.contains("gain|calib|psf", case=False, na=False)]
    df["band"] = df.apply(_band_of, axis=1)
    df = df.dropna(subset=["band"])
    df["mjd"] = pd.to_numeric(df.get("t_min"), errors="coerce")
    lo = pd.to_numeric(df.get("em_min", np.nan), errors="coerce") * 1e6
    hi = pd.to_numeric(df.get("em_max", np.nan), errors="coerce") * 1e6
    df["lam_lo"], df["lam_hi"] = lo, hi
    df["obs_id"] = df["obs_id"].map(_s) if "obs_id" in df else df["access_url"].str.extract(r"/([^/]+)\.fits")[0]
    df = df.dropna(subset=["mjd"]).drop_duplicates("access_url")
    keep = ["obs_id", "access_url", "band", "mjd", "lam_lo", "lam_hi"]
    return df[keep].sort_values("mjd").reset_index(drop=True)


def assign_epochs(mjd: np.ndarray, gap_days: float = 30.0) -> np.ndarray:
    """Group observations into visits separated by gaps (SPHEREx revisits ~6 months)."""
    order = np.argsort(mjd)
    ep = np.zeros(len(mjd), int)
    cur = 0
    for i in range(1, len(order)):
        if mjd[order[i]] - mjd[order[i - 1]] > gap_days:
            cur += 1
        ep[order[i]] = cur
    return ep


def select_frames(df: pd.DataFrame, bands: list[str], per_cell: int) -> pd.DataFrame:
    """Evenly sample up to `per_cell` frames per (epoch, band)."""
    sub = df[df["band"].isin(bands)]
    picks = []
    for _, g in sub.groupby(["epoch", "band"]):
        idx = np.unique(np.linspace(0, len(g) - 1, min(per_cell, len(g))).round().astype(int))
        picks.append(g.iloc[idx])
    return pd.concat(picks).sort_values("mjd") if picks else sub.iloc[0:0]


def _wavelength(hdul, hdr, ra, dec, lo, hi) -> float:
    """Central wavelength (µm) at the target pixel, via the SPHEREx spectral WCS."""
    mid = (lo + hi) / 2 if np.isfinite(lo) and np.isfinite(hi) else np.nan
    try:
        from .imaging import fix_sip
        x, y = WCS(fix_sip(hdr.copy())).celestial.world_to_pixel(SkyCoord(ra, dec, unit="deg"))
        ww = WCS(hdr, fobj=hdul, key="W")
        ww.sip = None
        out = ww.pixel_to_world(x, y)
        lam = out[0] if isinstance(out, (list, tuple)) else out
        val = float(lam.to(u.um).value) if hasattr(lam, "to") else float(lam)
        if np.isfinite(val) and 0.6 < val < 5.2:
            if not np.isfinite(mid) or (lo - 0.05) <= val <= (hi + 0.05):
                return val
    except Exception:
        pass
    return mid


def fetch_cutout(row: dict, ra: float, dec: float, size_deg: float) -> dict | None:
    """Download a server-side cutout (IRSA crops the MEF before sending it)."""
    try:
        r = requests.get(row["access_url"],
                         params={"center": f"{ra:.7f},{dec:.7f}", "size": f"{size_deg:.5f}"},
                         timeout=TIMEOUT)
        r.raise_for_status()
        with fits.open(io.BytesIO(r.content)) as h:
            names = [x.name.upper() for x in h]
            ext = "IMAGE" if "IMAGE" in names else 1
            img = np.asarray(h[ext].data, dtype="f4")
            hdr = h[ext].header
            flags = np.asarray(h["FLAGS"].data) if "FLAGS" in names else None
            var = np.asarray(h["VARIANCE"].data, "f4") if "VARIANCE" in names else None
            if "ZODI" in names:
                z = np.asarray(h["ZODI"].data, "f4")
                if z.shape == img.shape:
                    img = img - z  # remove zodiacal foreground
            wave = _wavelength(h, hdr, ra, dec, row.get("lam_lo", np.nan), row.get("lam_hi", np.nan))
        flag_frac = float(np.mean(flags != 0)) if flags is not None else np.nan
        return {**row, "img": img, "var": var, "header": hdr.tostring(),
                "flag_frac": flag_frac, "wave": wave,
                "flags": flags.astype("i4") if flags is not None else None}
    except Exception as e:  # noqa: BLE001
        return {**row, "error": str(e)}


def fetch_many(rows: list[dict], ra: float, dec: float, size_deg: float,
               workers: int = 8, progress=None) -> list[dict]:
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch_cutout, r, ra, dec, size_deg) for r in rows]
        for i, f in enumerate(as_completed(futs), 1):
            res = f.result()
            if res is not None:
                out.append(res)
            if progress:
                progress(i / len(futs))
    return sorted(out, key=lambda d: d["mjd"])


def mjd_to_year(mjd: float) -> float:
    return float(Time(mjd, format="mjd").jyear)


def mjd_to_date(mjd: float) -> str:
    return Time(mjd, format="mjd").to_datetime().strftime("%b %d, %Y")
