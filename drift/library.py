"""Everything the pipeline has looked at, kept between visits: the Discoveries log and the Compare page read it.

An entry is written each time a target's page is built, and its spectrum is added once the six detectors have
been measured. A *discovery* is an entry where the pipeline found something: a confirmed move against the
archive image, or brightness that changes coherently in time. Stored in data/library.json (one small dict per
object, keyed by name), and seeded from the precomputed presets so the log is never empty.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(HERE, "data", "library.json")
STAGE_DIR = os.path.join(HERE, "data", "stage")
SEED = os.path.join(HERE, "data", "discoveries_seed.json")      # real pipeline runs on well-known stars (scripts/build_discoveries.py)
_LOCK = threading.Lock()
MAX_ENTRIES = 400


def _load():
    try:
        with open(PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(lib):
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(lib, fh)
        os.replace(tmp, PATH)
    except Exception:
        pass                                            # read-only disk: keep going, the log just won't persist


def _moved(verdict):
    m = re.search(r"Moved\s+([\d,]+)", (verdict or {}).get("word", ""))
    return float(m.group(1).replace(",", "")) if m else None


def _small_spectrum(sp):
    """The binned spectrum only (wavelength, flux): enough to draw, small enough to keep."""
    if not sp:
        return None
    pts = sp.get("smooth") or []
    return dict(pts=[[round(float(p[0]), 4), float(p[1])] for p in pts if p and p[1] and p[1] > 0],
                unit=sp.get("unit", "µJy"), features=[f.get("label") for f in sp.get("features") or []])


def entry_from_stage(st, key=None, fov=None):
    f = st.get("facts") or {}
    lc = st.get("lc") or {}
    moved = _moved(st.get("verdict"))
    varies = bool(lc.get("word", "").startswith("varies"))
    return dict(
        name=st.get("name"), key=key or "custom", ra=st.get("ra"), dec=st.get("dec"), pmra=st.get("pmra") or 0.0,
        pmdec=st.get("pmdec") or 0.0, epoch=st.get("epoch"), fov=fov or round(float(st.get("field_deg") or 0.08) * 3600 / 1.3),
        detector=st.get("detector") or "SPHEREx-D3", kind=f.get("kind") or "Patch of sky", distance_ly=f.get("distance_ly"),
        temp_k=f.get("temp_k"), motion=f.get("motion_arcsec_yr"), constellation=f.get("constellation"),
        moved_arcsec=moved, verdict=(st.get("verdict") or {}).get("word"), conf=(st.get("verdict") or {}).get("conf"),
        lc=lc.get("word"), lc_src=lc.get("src"), varies=varies, n_objects=len(st.get("objects") or []),
        visits=st.get("visits"), discovery=bool(moved or varies), when=time.time())


def record(st, key=None, fov=None):
    """Add or refresh the entry for a stage payload."""
    if not st or not st.get("name"):
        return
    e = entry_from_stage(st, key, fov)
    with _LOCK:
        lib = _load() or _seed()
        old = lib.get(e["name"]) or {}
        if old.get("spectrum"):
            e["spectrum"] = old["spectrum"]
        e["first"] = old.get("first", e["when"])
        lib[e["name"]] = e
        if len(lib) > MAX_ENTRIES:                       # forget the oldest plain look-ups first
            for k in sorted(lib, key=lambda k: (lib[k].get("discovery"), lib[k].get("when", 0)))[:len(lib) - MAX_ENTRIES]:
                lib.pop(k, None)
        _save(lib)


def record_bands(name, bands_payload, lc=None):
    """Keep the target's spectrum (and a NEOWISE light curve, if one was found) with its entry."""
    sp = _small_spectrum(((bands_payload or {}).get("spectra") or {}).get("target"))
    with _LOCK:
        lib = _load() or _seed()
        e = lib.get(name)
        if not e:
            return
        if sp:
            e["spectrum"] = sp
        if lc and lc.get("word"):
            e["lc"], e["lc_src"] = lc["word"], lc.get("src")
            e["varies"] = lc["word"].startswith("varies")
            e["discovery"] = bool(e.get("moved_arcsec") or e["varies"])
        _save(lib)


def _seed():
    lib = {}
    try:
        from .targets import PRESETS
        keys = list(PRESETS)
    except Exception:
        keys = []
    for k in keys:
        try:
            with open(os.path.join(STAGE_DIR, f"{k}.json"), encoding="utf-8") as fh:
                st = json.load(fh)
            e = entry_from_stage(st, k)
            e["when"] = e["first"] = 0.0                  # presets sit at the bottom of the log
            try:
                with open(os.path.join(STAGE_DIR, f"{k}_bands.json"), encoding="utf-8") as fh:
                    e["spectrum"] = _small_spectrum(json.load(fh)["spectra"]["target"])
            except Exception:
                pass
            lib[e["name"]] = e
        except Exception:
            continue
    try:
        with open(SEED, encoding="utf-8") as fh:
            for name, e in json.load(fh).items():
                lib.setdefault(name, e)
    except Exception:
        pass
    return lib


def listing():
    """dict(discoveries=[...newest first], library=[...all, for Compare])."""
    with _LOCK:
        lib = _load()
        seed = _seed()
        if not lib:
            lib = seed
            _save(lib)
        else:
            for k, v in seed.items():
                lib.setdefault(k, v)
    rows = sorted(lib.values(), key=lambda e: -(e.get("when") or 0))
    return dict(discoveries=[e for e in rows if e.get("discovery")], library=rows)
