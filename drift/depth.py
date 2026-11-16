"""A 3D map of everything with a measured distance in and around the field: stars from Gaia DR3 parallaxes,
galaxies and quasars from SIMBAD redshifts (and measured distances for nearby galaxies).

Distances
    star:    d = 1 / parallax                                   (parsec, parallax in arcsec)
    galaxy:  d = c * t_lb(z),  t_lb(z) = int_0^z dz' / ((1+z') H(z'))   light-travel distance, Planck 2018
             (= how far the light we receive now has travelled, so "distance" and "light left ... ago" agree)
Motion (stars, for the future slider)
    v_tan = 4.74047 * mu[arcsec/yr] * d[pc]  km/s,  v = v_tan + v_rad * n,   r(t) = r0 + v t
"""
from __future__ import annotations

import math

import numpy as np

LY_PER_PC = 3.26156
KMS_TO_LY_PER_YR = 1.0 / 299792.458        # 1 km/s = c/299792 -> light years per year
GAIA_TAP = "https://gea.esac.esa.int/tap-server/tap/sync"


def _num(v):
    try:
        x = float(str(v).strip().strip('"'))
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _gaia(ra, dec, r_deg, timeout=20.0):
    """Gaia DR3 stars with a parallax better than 5 sigma (distance good to 20%), brightest first."""
    import csv
    import io

    import httpx
    q = ("SELECT TOP 1500 source_id, ra, dec, parallax, pmra, pmdec, radial_velocity, phot_g_mean_mag FROM gaiadr3.gaia_source "
         f"WHERE 1=CONTAINS(POINT('ICRS',ra,dec),CIRCLE('ICRS',{ra:.6f},{dec:.6f},{r_deg:.5f})) "
         "AND parallax_over_error > 5 AND parallax > 0 ORDER BY phot_g_mean_mag")
    try:
        t = httpx.get(GAIA_TAP, params=dict(REQUEST="doQuery", LANG="ADQL", FORMAT="csv", QUERY=q), timeout=timeout).text
        return list(csv.DictReader(io.StringIO(t)))
    except Exception:
        return None


def _simbad(ra, dec, r_deg):
    from .naming import _tap
    cols = ("oid, main_id, otype, sp_type, ra, dec, plx_value, rvz_redshift, rvz_radvel, pmra, pmdec, nbref, "
            "galdim_majaxis AS majax, galdim_minaxis AS minax, galdim_angle AS pa")
    return _tap(f"SELECT TOP 3000 {cols} FROM basic WHERE 1=CONTAINS(POINT('ICRS',ra,dec),CIRCLE('ICRS',{ra:.6f},"
                f"{dec:.6f},{r_deg:.6f})) AND (plx_value > 0.05 OR rvz_redshift > 0.0003)", 8.0) or []


def _offset(ra, dec, ra_c, dec_c):
    """Tangent-plane offsets (degrees, east and north) of a position from the field centre."""
    a, d, a0, d0 = map(math.radians, (ra, dec, ra_c, dec_c))
    cosc = math.sin(d0) * math.sin(d) + math.cos(d0) * math.cos(d) * math.cos(a - a0)
    x = math.cos(d) * math.sin(a - a0) / cosc
    y = (math.cos(d0) * math.sin(d) - math.sin(d0) * math.cos(d) * math.cos(a - a0)) / cosc
    return math.degrees(x), math.degrees(y)


def depth_map(ra_c, dec_c, field_deg, target_name=None):
    """Everything with a distance within the field (at least 0.15 deg around its centre).

    Returns dict(radius_deg, objects=[dict(name, kind, cls, x, y (deg offsets), ly, z, v=[ve, vn, vr] km/s, mag)],
    sources=[...]). Objects without a trustworthy distance are left out."""
    from concurrent.futures import ThreadPoolExecutor
    from .facts import catalogue_distance_ly, lookback_years
    from .inframe import kind_of
    from .naming import _clean, _readable
    r_deg = max(field_deg * 0.71, 0.02)          # just the field (its corners); the map shows only the field
    with ThreadPoolExecutor(2) as ex:
        fg, fs = ex.submit(_gaia, ra_c, dec_c, r_deg), ex.submit(_simbad, ra_c, dec_c, r_deg)
        gaia, sim = fg.result(), fs.result()
    objs, seen = [], []

    def add(o):
        objs.append(o)
        seen.append((o["ra"], o["dec"]))

    def near_seen(ra, dec, arcsec=2.0):
        c = math.cos(math.radians(dec))
        return any(abs(dec - d) * 3600 < arcsec and abs(ra - r) * c * 3600 < arcsec for r, d in seen)

    # SIMBAD first: it carries names and types
    for r in sorted(sim, key=lambda r: -int(r.get("nbref") or 0)):
        ra, dec = _num(r.get("ra")), _num(r.get("dec"))
        if ra is None or dec is None or near_seen(ra, dec) or str(r.get("otype", "")).strip('"') in ("Pl", "Pl?"):
            continue                                   # one entry per star (no planets, no duplicate names)
        ot = str(r.get("otype", "")).strip('"')
        kind = kind_of(ot)
        plx, z = _num(r.get("plx_value")), _num(r.get("rvz_redshift"))
        galaxy = "alax" in kind or kind in ("Quasar", "Blazar", "Blazar (quasar)", "Active galaxy", "Radio source") or ot in ("QSO", "Bla", "BLL", "AGN")
        if galaxy:
            if z is not None and z > 0.003:
                ly = -1.0                          # filled in below, all redshifts at once
            else:
                ly = catalogue_distance_ly(r.get("oid"))
            if not ly:
                continue
            cls = "quasar" if ot in ("QSO", "Bla", "BLL") or "uasar" in kind else "galaxy"
            v = [0.0, 0.0, 0.0]
        else:
            if not plx or plx < 0.05:
                continue
            pc = 1000.0 / plx
            ly = pc * LY_PER_PC
            if ly > 3e5:                    # parallax noise beyond the Milky Way: not a real distance
                continue
            pmra, pmdec, rv = _num(r.get("pmra")) or 0.0, _num(r.get("pmdec")) or 0.0, _num(r.get("rvz_radvel")) or 0.0
            v = [4.74047 * pmra / 1000 * pc, 4.74047 * pmdec / 1000 * pc, rv]
            cls = "star"
        x, y = _offset(ra, dec, ra_c, dec_c)
        maj, mnr, pa = _num(r.get("majax")), _num(r.get("minax")), _num(r.get("pa"))
        add(dict(size=round(maj, 3) if maj else None, ratio=round(mnr / maj, 3) if maj and mnr else None,
                 pa=pa, name=_readable(_clean(str(r.get("main_id", "")))), kind=kind, cls=cls, ra=ra, dec=dec, x=round(x, 5),
                 y=round(y, 5), ly=float(ly), z=z if galaxy else None, v=[round(c, 3) for c in v],
                 nb=int(r.get("nbref") or 0)))
    # Gaia fills in every other star with a good parallax
    for g in gaia or []:
        ra, dec, plx = _num(g.get("ra")), _num(g.get("dec")), _num(g.get("parallax"))
        if ra is None or dec is None or not plx or near_seen(ra, dec):
            continue
        pc = 1000.0 / plx
        pmra, pmdec, rv = _num(g.get("pmra")) or 0.0, _num(g.get("pmdec")) or 0.0, _num(g.get("radial_velocity")) or 0.0
        x, y = _offset(ra, dec, ra_c, dec_c)
        add(dict(name=f"Gaia DR3 {g.get('source_id')}", kind="Star", cls="star", ra=ra, dec=dec, x=round(x, 5),
                 y=round(y, 5), ly=pc * LY_PER_PC, z=None,
                 v=[round(4.74047 * pmra / 1000 * pc, 3), round(4.74047 * pmdec / 1000 * pc, 3), round(rv, 3)],
                 mag=_num(g.get("phot_g_mean_mag")), nb=0))
    zs = [o for o in objs if o["ly"] == -1.0]
    if zs:
        from astropy.cosmology import Planck18
        lb = Planck18.lookback_time(np.array([o["z"] for o in zs])).to("yr").value
        for o, t in zip(zs, lb):
            o["ly"] = float(t)
    for o in objs:
        o["ly"] = round(o["ly"], 4 if o["ly"] < 100 else 1)
        o.pop("ra"); o.pop("dec")
    half = max(field_deg / 2.0, 0.01)
    big = lambda o: (o.get("size") or 0) / 120.0                 # half its size in degrees: a galaxy may hang over the edge
    objs = [o for o in objs if abs(o["x"]) <= half + big(o) and abs(o["y"]) <= half + big(o)]   # only what is in the patch
    objs.sort(key=lambda o: o["ly"])
    return dict(radius_deg=half, objects=objs,
                sources=["Gaia DR3 parallaxes" if gaia is not None else None, "SIMBAD distances and redshifts"],
                gaia_ok=gaia is not None)
