"""Name a selected patch of sky by what is actually in it (SIMBAD), not just its constellation.

Rules:
- An extended object (galaxy, nebula, cluster) that is mostly inside the frame (its size between 25% and
  130% of the frame) is the name. Two such objects: both names.
- Zoomed far into an extended object (it is much bigger than the frame): name the brightest star in the
  frame if there is a reasonably bright one, else "Inside <object>".
- Otherwise the brightest star inside the frame (V <= 12); a second star within 1 mag that is also
  brighter than V = 9 is named too.
- Nothing notable, or SIMBAD unreachable: "Patch in <constellation>".
Only objects whose centre is inside the frame count (no neighbours from a few frames away),
except the "inside a big galaxy" case, where the frame centre lies within the object.
"""
from __future__ import annotations

import functools
import io

import numpy as np

MIRRORS = ("https://simbad.cfa.harvard.edu/simbad/sim-tap/sync", "https://simbad.harvard.edu/simbad/sim-tap/sync")
EXTENDED = ("G", "GiG", "GiP", "GiC", "AGN", "Sy1", "Sy2", "SyG", "SBG", "LIN", "QSO", "BLL", "rG", "H2G", "EmG", "IG",
            "PaG", "GrG", "ClG", "PN", "HII", "SNR", "Cl*", "OpC", "GlC", "As*", "ISM", "MoC", "RNe", "DNe", "Neb", "bub", "SFR",
            "cor", "Cld", "GNe", "BNe")
STAR_VMAX = 12.0
PAIR_VMAX = 9.0


def _tap(adql, timeout=2.5):
    import httpx
    for url in MIRRORS:
        try:
            r = httpx.get(url, params=dict(REQUEST="doQuery", LANG="ADQL", FORMAT="csv", QUERY=adql), timeout=timeout)
            if r.status_code == 200 and not r.text.lstrip().startswith("<"):
                import csv
                return list(csv.DictReader(io.StringIO(r.text)))          # every value a string; '' when missing
        except Exception:
            continue
    return None


GREEK = dict(alf="Alpha", bet="Beta", gam="Gamma", del_="Delta", eps="Epsilon", zet="Zeta", eta="Eta", tet="Theta", iot="Iota",
             kap="Kappa", lam="Lambda", mu="Mu", nu="Nu", ksi="Xi", omi="Omicron", pi="Pi", rho="Rho", sig="Sigma", tau="Tau",
             ups="Upsilon", phi="Phi", chi="Chi", psi="Psi", ome="Omega")
GREEK["del"] = GREEK.pop("del_")
GEN = dict(And="Andromedae", Ant="Antliae", Aps="Apodis", Aqr="Aquarii", Aql="Aquilae", Ara="Arae", Ari="Arietis", Aur="Aurigae",
           Boo="Bootis", Cae="Caeli", Cam="Camelopardalis", Cnc="Cancri", CVn="Canum Venaticorum", CMa="Canis Majoris",
           CMi="Canis Minoris", Cap="Capricorni", Car="Carinae", Cas="Cassiopeiae", Cen="Centauri", Cep="Cephei", Cet="Ceti",
           Cha="Chamaeleontis", Cir="Circini", Col="Columbae", Com="Comae Berenices", CrA="Coronae Australis",
           CrB="Coronae Borealis", Crv="Corvi", Crt="Crateris", Cru="Crucis", Cyg="Cygni", Del="Delphini", Dor="Doradus",
           Dra="Draconis", Equ="Equulei", Eri="Eridani", For="Fornacis", Gem="Geminorum", Gru="Gruis", Her="Herculis",
           Hor="Horologii", Hya="Hydrae", Hyi="Hydri", Ind="Indi", Lac="Lacertae", Leo="Leonis", LMi="Leonis Minoris",
           Lep="Leporis", Lib="Librae", Lup="Lupi", Lyn="Lyncis", Lyr="Lyrae", Men="Mensae", Mic="Microscopii", Mon="Monocerotis",
           Mus="Muscae", Nor="Normae", Oct="Octantis", Oph="Ophiuchi", Ori="Orionis", Pav="Pavonis", Peg="Pegasi", Per="Persei",
           Phe="Phoenicis", Pic="Pictoris", Psc="Piscium", PsA="Piscis Austrini", Pup="Puppis", Pyx="Pyxidis", Ret="Reticuli",
           Sge="Sagittae", Sgr="Sagittarii", Sco="Scorpii", Scl="Sculptoris", Sct="Scuti", Ser="Serpentis", Sex="Sextantis",
           Tau="Tauri", Tel="Telescopii", Tri="Trianguli", TrA="Trianguli Australis", Tuc="Tucanae", UMa="Ursae Majoris",
           UMi="Ursae Minoris", Vel="Velorum", Vir="Virginis", Vol="Volantis", Vul="Vulpeculae")


def _readable(s):
    """'alf Cen' -> 'Alpha Centauri', 'eps Ind B' -> 'Epsilon Indi B', 'R Dra' -> 'R Draconis'."""
    w = s.split()
    if len(w) >= 2 and w[1] in GEN:
        g = w[0].rstrip("0123456789")
        num = w[0][len(g):]
        first = (GREEK.get(g, w[0] if not g else g) + (num.lstrip("0") and f"-{num.lstrip('0')}" or "")) if g in GREEK else w[0]
        return " ".join([first, GEN[w[1]]] + w[2:])
    return s


def _clean(main_id):
    s = str(main_id).strip().strip('"')
    for p in ("NAME ", "* ", "V* ", "** "):
        if s.startswith(p):
            s = s[len(p):]
    return " ".join(s.split())


def _nice_names(oids):
    if not oids:
        return {}
    rows = _tap(f"SELECT oidref, id FROM ident WHERE oidref IN ({','.join(str(int(o)) for o in oids)}) AND id LIKE 'NAME %'") or []
    cand = {}
    for r in rows:
        cand.setdefault(int(r["oidref"]), []).append(_clean(r["id"]))
    out = {}
    for o, names in cand.items():               # "Andromeda Galaxy", "Orion Nebula", "Pleiades", "Sirius"
        kind = [n for n in names if any(w in n for w in ("Galaxy", "Nebula", "Cluster")) and len(n) <= 26]
        pool = kind or [n for n in names if len(n) <= 26] or names
        full = [n for n in pool if n.split()[0] not in GEN] or pool          # "Andromeda Galaxy" over "And Nebula"
        out[o] = min(full, key=len)
    return out


def _sep_deg(ra1, dec1, ra2, dec2):
    r = np.radians
    c = np.sin(r(dec1)) * np.sin(r(dec2)) + np.cos(r(dec1)) * np.cos(r(dec2)) * np.cos(r(ra1 - ra2))
    return float(np.degrees(np.arccos(np.clip(c, -1, 1))))


def _in_frame(ra, dec, ra_c, dec_c, half):
    dx = ((ra - ra_c + 540) % 360 - 180) * np.cos(np.radians(dec_c))
    return abs(dx) <= half and abs(dec - dec_c) <= half


def _constellation(ra, dec):
    from astropy.coordinates import SkyCoord, get_constellation
    import astropy.units as u
    return get_constellation(SkyCoord(ra * u.deg, dec * u.deg))


@functools.lru_cache(maxsize=512)
def name_field(ra_c, dec_c, field_deg):
    from concurrent.futures import ThreadPoolExecutor
    half = field_deg / 2.0
    fallback = f"Patch in {_constellation(ra_c, dec_c)}"
    field_am = field_deg * 60.0
    # the best-studied objects in and around the frame (SIMBAD reference count = how notable), and big objects we may be inside
    q_top = (f"SELECT TOP 3000 oid, main_id, ra, dec, pmra, pmdec, otype, galdim_majaxis AS majax, nbref FROM basic "
             f"WHERE 1=CONTAINS(POINT('ICRS',ra,dec), CIRCLE('ICRS',{ra_c:.6f},{dec_c:.6f},{half * 1.415 + 0.08:.6f})) AND nbref >= 5")
    q_big = (f"SELECT TOP 200 oid, main_id, ra, dec, otype, galdim_majaxis AS majax, nbref FROM basic "
             f"WHERE 1=CONTAINS(POINT('ICRS',ra,dec), CIRCLE('ICRS',{ra_c:.6f},{dec_c:.6f},2.0)) "
             f"AND galdim_majaxis > {1.3 * field_am:.3f}")
    q_fit = (f"SELECT TOP 200 oid, main_id, ra, dec, otype, galdim_majaxis AS majax, nbref FROM basic "
             f"WHERE 1=CONTAINS(POINT('ICRS',ra,dec), CIRCLE('ICRS',{ra_c:.6f},{dec_c:.6f},{half * 1.415:.6f})) "
             f"AND galdim_majaxis >= {0.25 * field_am:.3f} AND galdim_majaxis <= {1.3 * field_am:.3f}")
    with ThreadPoolExecutor(3) as ex:
        f1, f2, f3 = ex.submit(_tap, q_top, 12.0), ex.submit(_tap, q_big, 12.0), ex.submit(_tap, q_fit, 12.0)
        top, big, fit = f1.result(), f2.result(), f3.result()
    if top is None and big is None and fit is None:
        return fallback
    top, big, fit = top or [], big or [], fit or []
    top = sorted(top, key=lambda r: -int(r["nbref"]))[:60]           # sorted here: ORDER BY on the server is slow
    vmag = {}
    if top:
        rows = _tap(f"SELECT oidref, V FROM allfluxes WHERE oidref IN ({','.join(str(int(r['oid'])) for r in top)})") or []
        vmag = {int(r["oidref"]): float(r["V"]) for r in rows if r.get("V", "").strip() not in ("", "nan")}
    ot = lambda r: str(r["otype"]).strip('"')
    size = lambda r: float(r["majax"]) if r.get("majax", "").strip() not in ("", "nan") else 0.0
    def inside(r):                         # catalogue positions are for 2000: move fast stars to 2010 (AllWISE) and 2026 (SPHEREx)
        ra, dec = float(r["ra"]), float(r["dec"])
        pa = float(r["pmra"]) if r.get("pmra", "").strip() else 0.0
        pd = float(r["pmdec"]) if r.get("pmdec", "").strip() else 0.0
        for yr in (2010.5, 2026.0):
            d = dec + pd * (yr - 2000) / 3.6e6
            a = ra + pa * (yr - 2000) / 3.6e6 / max(np.cos(np.radians(d)), 1e-6)
            if _in_frame(a, d, ra_c, dec_c, half):
                return True
        return False
    fits = [r for r in fit if ot(r) in EXTENDED and inside(r) and 0.25 * field_am <= size(r) <= 1.3 * field_am and int(r["nbref"]) >= 20]
    big = sorted(big, key=lambda r: -size(r))
    around = [r for r in big if ot(r) in EXTENDED and int(r["nbref"]) >= 300 and _sep_deg(float(r["ra"]), float(r["dec"]), ra_c, dec_c) * 60 <= size(r) / 2]
    around.sort(key=lambda r: -int(r["nbref"]))
    stars = [r for r in top if ot(r) not in EXTENDED and inside(r) and vmag.get(int(r["oid"]), 99) <= STAR_VMAX]
    stars.sort(key=lambda r: vmag[int(r["oid"])])
    cited = [r for r in top if inside(r) and int(r["nbref"]) >= 100]          # e.g. a famous quasar or pulsar
    nice = _nice_names([int(x["oid"]) for x in fits[:2] + around[:1] + stars[:3] + cited[:1]])
    nm = lambda r: nice.get(int(r["oid"]), _readable(_clean(r["main_id"])))
    if fits:
        fits.sort(key=lambda r: -int(r["nbref"]))
        return " and ".join(dict.fromkeys(nm(f) for f in fits[:2]))
    if cited and around and int(cited[0]["oid"]) == int(around[0]["oid"]):
        cited = cited[1:]                                     # the big object itself: handled as "Inside ..."
    if cited and (not stars or int(cited[0]["nbref"]) > 3 * int(stars[0]["nbref"])):
        return nm(cited[0]) + (f" in {nm(around[0])}" if around and int(around[0]["oid"]) != int(cited[0]["oid"]) else "")
    if stars:
        a, va = stars[0], vmag[int(stars[0]["oid"])]
        if around and va > 9.0:                                  # deep inside a big object and only faint stars here
            return f"Inside {nm(around[0])}"
        where = f" in {nm(around[0])}" if around else ""
        others = [r for r in stars[1:] if _sep_deg(float(r["ra"]), float(r["dec"]), float(a["ra"]), float(a["dec"])) * 3600 > 30
                  and ot(r) != "**" and ot(a) != "**" or (ot(a) == "**" and False)]
        if ot(a) == "**":                                   # a multiple-star system entry: name the system once
            return nm(a) + where
        if others:
            b, vb = others[0], vmag[int(others[0]["oid"])]
            if vb <= PAIR_VMAX and vb - va <= 1.0:
                return f"{nm(a)} and {nm(b)}{where}"
        return nm(a) + where
    if around:
        return f"Inside {nm(around[0])}"
    return fallback
