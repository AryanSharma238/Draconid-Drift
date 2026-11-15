"""Build the labelled truth list for the object classifier: SIMBAD objects with a known type, bright enough in
Ks for SPHEREx to measure, spread over the whole sky. Writes data/classify/labels.csv.

Classes (temperature/kind, from the SIMBAD spectral type or object type):
  hot     O, B, A stars
  solar   F, G, K stars
  red     M stars (dwarfs and giants)
  bd      L, T brown dwarfs
  galaxy  galaxies (normal + active)
  quasar  quasars / blazars
Rare objects (black holes, X-ray binaries, pulsars, ...) are deliberately not in the label set.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent.parent / "data" / "classify" / "labels.csv"
TAP = "https://simbad.cfa.harvard.edu/simbad/sim-tap/sync"   # official CDS mirror (the Strasbourg TAP was down 2026-11-14)
PER_CLASS = 300

STAR_OT = "('*','**','Er*','BY*','RS*','Ro*','Pu*','V*','Mi*','Ir*','Or*','TT*','Y*O','LM*','BD*','dS*','RR*','Ce*','SB*','El*','HB*','WD*','C*','S*','PM*','RGB*','AB*','HS*','NL*')"


def spclass(sp: str, ot: str) -> str | None:
    """Coarse class from a SIMBAD spectral type string. None = skip (ambiguous or peculiar)."""
    sp = (sp or "").strip()
    if not sp or re.search(r"[:?]|\+|/|\bwd\b|^D[A-Z]|^C|^S\d|^sd|^k$|^g$|^m$|^f$|^d|rich|pec", sp, re.IGNORECASE):
        return None
    m = re.match(r"^([OBAFGKMLT])([0-9](\.[0-9])?)$|^([OBAFGKMLT])([0-9](\.[0-9])?)?(?:I{1,3}|IV|V)\b", sp)
    if not m:
        return None
    c = m.group(1) or m.group(4)
    return {"O": "hot", "B": "hot", "A": "hot", "F": "solar", "G": "solar", "K": "solar",
            "M": "red", "L": "bd", "T": "bd"}[c]


def query(adql: str) -> list[list]:
    r = httpx.post(TAP, data=dict(request="doQuery", lang="adql", format="json", query=adql), timeout=120.0)
    r.raise_for_status()
    return r.json().get("data", [])


SELECT_COLS = """b.main_id, b.ra, b.dec, b.pmra, b.pmdec, b.sp_type, b.otype,
       b.plx_value, b.rvz_redshift, b.galdim_majaxis,
       f.U, f.B, f.V, f.G, f.R, f.I, f.J, f.H, f.K, f.u_, f.g_, f.r_, f.i_, f.z_"""


def stars(kmin: float, kmax: float, mod: int, rem: int, top: int) -> list[list]:
    return query(f"""SELECT TOP {top} {SELECT_COLS}
FROM basic AS b JOIN allfluxes AS f ON f.oidref = b.oid
WHERE b.sp_type IS NOT NULL AND b.otype IN {STAR_OT} AND f.K BETWEEN {kmin} AND {kmax}
 AND b.ra IS NOT NULL AND ABS(b.dec) < 80 AND MOD(b.oid, {mod}) = {rem}""")


def extragalactic(otypes: str, kmin: float, kmax: float, top: int) -> list[list]:
    return query(f"""SELECT TOP {top} {SELECT_COLS}
FROM basic AS b JOIN allfluxes AS f ON f.oidref = b.oid
WHERE b.otype IN {otypes} AND f.K BETWEEN {kmin} AND {kmax} AND b.ra IS NOT NULL AND ABS(b.dec) < 80
 AND ((b.galdim_majaxis IS NULL) OR b.galdim_majaxis < 0.3)""")


def main() -> int:
    rows: list[dict] = []
    seen: set[str] = set()

    def add(r: list, cls: str) -> None:
        mid = str(r[0])
        if mid in seen:
            return
        seen.add(mid)
        rows.append(dict(
            id=mid, ra=r[1], dec=r[2], pmra=r[3] or 0.0, pmdec=r[4] or 0.0, sp=r[5] or "",
            otype=r[6], plx=r[7] if r[7] is not None else "", z=r[8] if r[8] is not None else "",
            galdim=r[9] if r[9] is not None else "",
            U=r[10] if r[10] is not None else "", B=r[11] if r[11] is not None else "",
            V=r[12] if r[12] is not None else "", G=r[13] if r[13] is not None else "",
            R=r[14] if r[14] is not None else "", I=r[15] if r[15] is not None else "",
            J=r[16] if r[16] is not None else "", H=r[17] if r[17] is not None else "",
            ks=r[18] if r[18] is not None else "",
            u_=r[19] if r[19] is not None else "", g_=r[20] if r[20] is not None else "",
            r_=r[21] if r[21] is not None else "", i_=r[22] if r[22] is not None else "",
            z_=r[23] if r[23] is not None else "",
            cls=cls,
        ))

    # stars: several slices of the catalogue so the sky and the brightness range are well mixed
    for rem in range(0, 6):
        for kmin, kmax in ((5.0, 7.5), (7.5, 9.0), (9.0, 10.0)):
            try:
                for r in stars(kmin, kmax, 6, rem, 400):
                    c = spclass(r[5], r[6])
                    if c:
                        add(r, c)
            except Exception as e:  # noqa: BLE001
                print("slice failed", rem, kmin, kmax, type(e).__name__, e, file=sys.stderr)
    for r in extragalactic("('QSO','Bla','BLL','Sy1','AGN')", 8.0, 14.0, 400):
        add(r, "quasar")
    for r in extragalactic("('G','GiG','LSB','bCG','SBG','H2G','EmG')", 8.0, 12.0, 400):
        add(r, "galaxy")
    # brown dwarfs are faint in Ks (9-15), so they get their own brightness range
    for pat in ("L%", "T%"):
        try:
            for r in query(f"""SELECT TOP 600 {SELECT_COLS}
FROM basic AS b JOIN allfluxes AS f ON f.oidref = b.oid
WHERE b.sp_type LIKE '{pat}' AND f.K BETWEEN 9.0 AND 15.0 AND b.ra IS NOT NULL AND ABS(b.dec) < 80"""):
                if (c := spclass(r[5], r[6])) == "bd":
                    add(r, c)
        except Exception as e:  # noqa: BLE001
            print("bd query failed", pat, type(e).__name__, e, file=sys.stderr)
    # balance: at most PER_CLASS per class (random but reproducible), so no class dominates the accuracy figure
    import random
    random.Random(0).shuffle(rows)
    kept, n = [], {}
    for r in rows:
        if n.get(r["cls"], 0) < PER_CLASS:
            n[r["cls"]] = n.get(r["cls"], 0) + 1
            kept.append(r)
    rows = kept
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    from collections import Counter
    print(len(rows), "objects", dict(Counter(r["cls"] for r in rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

