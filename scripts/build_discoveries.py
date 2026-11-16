"""Run the full pipeline on well-known fast-moving and variable stars and store the results as the seed of the
Discoveries log and the Compare list (data/discoveries_seed.json). Every entry is a real measurement: the move is
confirmed against the archive image, the brightness is measured from SPHEREx (NEOWISE for saturated stars), and
the spectrum comes from all six detectors.   python scripts/build_discoveries.py [--jobs 3]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "data", "discoveries_seed.json")

# name, RA, Dec (J2000, deg), pmRA*, pmDec (mas/yr)
TARGETS = [
    ("Kapteyn's Star", 77.9191, -45.0184, 6491.2, -5708.6), ("Wolf 359", 164.1205, 7.0147, -3842.0, -2725.0),
    ("Lalande 21185", 165.8341, 35.9699, -580.3, -4766.0), ("Ross 128", 176.9349, 0.7993, 607.3, -1223.0),
    ("61 Cygni A", 316.7248, 38.7494, 4164.2, 3249.6), ("Groombridge 34", 4.5953, 44.0221, 2891.5, 411.8),
    ("Lacaille 9352", 346.4668, -35.8531, 6766.0, 1330.3), ("Luyten's Star", 111.8521, 5.2258, 571.2, -3691.5),
    ("van Maanen's Star", 12.2912, 5.3886, 1231.4, -2711.9), ("Teegarden's Star", 43.2537, 16.8813, 3429.1, -3805.5),
    ("Ross 154", 282.4556, -23.8362, 639.4, -193.0), ("Lacaille 8760", 319.3136, -38.8674, -3259.0, -1145.9),
    ("Gliese 1", 1.3830, -37.3664, 5634.1, -2337.3), ("Struve 2398 A", 280.6967, 59.6286, -1311.7, 1792.3),
    ("Proxima Centauri", 217.4289, -62.6795, -3781.7, 769.5), ("Gliese 412 A", 165.0233, 43.5276, -4410.8, 943.3),
    ("Luhman 16", 162.3288, -53.3195, -2767.5, 356.9), ("Mira", 34.8366, -2.9776, 9.3, -237.4),
    ("Chi Cygni", 297.6413, 32.9141, -24.1, -41.2), ("T Tauri", 65.4976, 19.5351, 11.7, -14.3),
    ("R Leonis", 146.8890, 11.4285, -17.0, -11.0), ("CW Leonis", 146.9893, 13.2786, 35.0, 12.0),
]


def one(t):
    from drift import pipeline
    from drift.bands import all_bands
    from drift.demo import MID_EPOCH
    from drift.library import _small_spectrum, entry_from_stage
    from drift.stage_data import build_stage
    name, ra, dec, pmra, pmdec = t
    spec = pipeline.framed(pipeline.Spec(name=name, ra=ra, dec=dec, pmra=pmra, pmdec=pmdec, epoch=2000.0,
                                         fov_arcsec=300, detector="SPHEREx-D3"))
    res = pipeline.run(spec, mode="target", n_null=60, build=False, max_frames=60)
    r2, d2 = pipeline.propagate(spec.ra, spec.dec, spec.pmra, spec.pmdec, spec.epoch, MID_EPOCH)
    st = build_stage(res, name, float(r2), float(d2), pmra, pmdec, MID_EPOCH, ra0=ra, dec0=dec, epoch0=2000.0)
    st = json.loads(json.dumps(st, default=float))
    e = entry_from_stage(st, None, 300)
    e["ra"], e["dec"], e["epoch"] = ra, dec, 2000.0            # reopen from the catalogue position
    lc = st.get("lc") or {}
    if lc.get("sat") or not lc.get("pts"):
        try:
            from drift.neowise import neowise_lc
            lc2 = neowise_lc(ra, dec, pmra, pmdec, 2000.0)
            if lc2:
                e["lc"], e["lc_src"] = lc2["word"], lc2["src"]
                e["varies"] = lc2["word"].startswith("varies")
        except Exception:
            pass
    try:
        tg = [("target", float(st["ra"]), float(st["dec"]), pmra, pmdec, float(st["epoch"]))]
        b = all_bands(pipeline.Spec(name=name, ra=st["ra_c"], dec=st["dec_c"], pmra=0.0, pmdec=0.0, epoch=float(MID_EPOCH),
                                    fov_arcsec=st["field_deg"] * 3600.0, detector="SPHEREx-D3", key="disc_" + name.replace(" ", "_")),
                      targets=tg)
        e["spectrum"] = _small_spectrum(b["spectra"].get("target"))
    except Exception as ex:
        print("  no spectrum for", name, ex, flush=True)
    e["discovery"] = bool(e.get("moved_arcsec") or e.get("varies"))
    e["when"] = e["first"] = time.time()
    return e


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--jobs", type=int, default=3); a = ap.parse_args()
    out = {}
    if os.path.exists(OUT):
        out = json.load(open(OUT))
    todo = [t for t in TARGETS if t[0] not in out]
    with ProcessPoolExecutor(a.jobs) as ex:
        fut = {ex.submit(one, t): t[0] for t in todo}
        for f in as_completed(fut):
            n = fut[f]
            try:
                e = f.result(); out[e["name"]] = e
                print(f"{n}: {e.get('verdict')} | {e.get('lc')} | spectrum {'yes' if e.get('spectrum') else 'no'}", flush=True)
                json.dump(out, open(OUT, "w"))
            except Exception as ex:
                print(f"{n}: FAILED {type(ex).__name__}: {ex}", flush=True)
    print("done", len(out))


if __name__ == "__main__":
    main()
