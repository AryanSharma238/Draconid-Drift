"""External catalogs: Gaia DR3 (proper motions) and IMCCE SkyBoT (known asteroids)."""
from __future__ import annotations

import math

import astropy.units as u
import numpy as np
import pandas as pd
from astropy.coordinates import SkyCoord
from astropy.time import Time

GAIA_EPOCH = 2016.0


def gaia_sources(coord: SkyCoord, radius_deg: float, gmax: float = 19.0) -> pd.DataFrame:
    from astroquery.vizier import Vizier

    v = Vizier(columns=["Source", "RA_ICRS", "DE_ICRS", "pmRA", "pmDE", "Gmag"],
               column_filters={"Gmag": f"<{gmax}"}, row_limit=3000)
    res = v.query_region(coord, radius=radius_deg * u.deg, catalog="I/355/gaiadr3")
    if not res:
        return pd.DataFrame()
    df = res[0].to_pandas()
    df = df.rename(columns={"RA_ICRS": "ra", "DE_ICRS": "dec", "pmRA": "pmra", "pmDE": "pmdec"})
    df[["pmra", "pmdec"]] = df[["pmra", "pmdec"]].fillna(0.0)
    df["pm_total"] = np.hypot(df["pmra"], df["pmdec"])
    return df


def propagate(df: pd.DataFrame, year: float) -> pd.DataFrame:
    dt = year - GAIA_EPOCH
    out = df.copy()
    out["dec_t"] = df["dec"] + df["pmdec"] * dt / 3.6e6
    out["ra_t"] = df["ra"] + df["pmra"] * dt / 3.6e6 / np.cos(np.radians(df["dec"]))
    return out


def skybot(coord: SkyCoord, radius_deg: float, mjd: float) -> pd.DataFrame:
    """Known solar-system bodies in the field at a given time (geocentric approx.)."""
    from astroquery.imcce import Skybot

    try:
        t = Skybot.cone_search(coord, radius_deg * u.deg, Time(mjd, format="mjd"),
                               location="500", get_raw_response=False)
        df = t.to_pandas()
        df["mjd"] = mjd
        return df
    except Exception:
        return pd.DataFrame()


def arcsec_sep(ra1, dec1, ra2, dec2) -> float:
    d = math.radians
    c = (math.sin(d(dec1)) * math.sin(d(dec2)) +
         math.cos(d(dec1)) * math.cos(d(dec2)) * math.cos(d(ra1 - ra2)))
    return math.degrees(math.acos(max(-1.0, min(1.0, c)))) * 3600
