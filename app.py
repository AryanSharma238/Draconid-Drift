"""Draconid Drift: one full-screen sky. Pick a spot, watch SPHEREx frames, see whether something moves."""
import os
import warnings

import streamlit as st
import streamlit.components.v1 as components
from astropy.utils.exceptions import AstropyWarning

from drift import pipeline
from drift.demo import MID_EPOCH
from drift.stage_data import build_stage
from drift.targets import PRESETS, target_position

warnings.simplefilter("ignore", AstropyWarning)
HERE = os.path.dirname(__file__)

st.set_page_config(page_title="Draconid Drift", layout="wide", initial_sidebar_state="collapsed")

# The stage is the whole page: hide Streamlit's own chrome and let the component fill the window.
st.markdown("""<style>
html,body,.stApp,[data-testid="stAppViewContainer"]{background:#13161A!important}
header,[data-testid="stHeader"],[data-testid="stToolbar"],[data-testid="stDecoration"],footer,
[data-testid="stStatusWidget"],#MainMenu{display:none!important}
.block-container,[data-testid="stMainBlockContainer"]{padding:0!important;max-width:none!important}
[data-testid="stCustomComponentV1"],[data-testid="stCustomComponentV1"] iframe,
iframe[title*="drift_stage"]{position:fixed!important;inset:0!important;width:100vw!important;height:100vh!important;
border:0!important;z-index:1000000!important;background:#13161A}
[data-testid="stElementContainer"]{margin:0!important}
</style>""", unsafe_allow_html=True)

stage = components.declare_component("drift_stage", path=os.path.join(HERE, "web", "stage"))

presets = []
for t in PRESETS.values():
    ra, dec = target_position(t, MID_EPOCH)
    presets.append(dict(key=t.key, name=t.name, ra=float(ra), dec=float(dec), pmra=t.pmra, pmdec=t.pmdec,
                        epoch=float(MID_EPOCH), fov=float(t.fov_arcsec), detector=t.detector))

S = st.session_state
S.setdefault("result", None)      # payload for the stage (or None while on the home sky)
S.setdefault("error", None)
S.setdefault("seen", None)        # nonce of the last request we handled

STAGE_DIR = os.path.join(HERE, "data", "stage")


@st.cache_data(show_spinner=False, max_entries=64)
def cached_stage(spec):
    """Stage payload for a spec: precomputed file for the presets, else computed once and kept in memory."""
    import json
    f = os.path.join(STAGE_DIR, f"{spec.key}.json")
    if spec.key in PRESETS and os.path.exists(f):
        with open(f, encoding="utf-8") as fh:
            return json.load(fh)
    res = pipeline.run(spec, mode="target", n_null=60, build=False, max_frames=60)
    ra_now, dec_now = pipeline.propagate(spec.ra, spec.dec, spec.pmra, spec.pmdec, spec.epoch, MID_EPOCH)
    return build_stage(res, spec.name, float(ra_now), float(dec_now), spec.pmra, spec.pmdec, MID_EPOCH,
                       ra0=spec.ra, dec0=spec.dec, epoch0=spec.epoch)


req = stage(presets=presets, mid=float(MID_EPOCH), result=S.result, error=S.error, key="stage", default=None)

# Support deep linking via URL query parameters (?ra=...&dec=...&fov=...)
qp = st.query_params
if "ra" in qp and "dec" in qp and S.result is None and S.seen is None:
    try:
        q_ra, q_dec = float(qp["ra"]), float(qp["dec"])
        q_fov = float(qp.get("fov", 180.0))
        q_spec = pipeline.framed(pipeline.Spec(
            name="Linked location", ra=q_ra, dec=q_dec, pmra=0.0, pmdec=0.0,
            epoch=float(MID_EPOCH), fov_arcsec=q_fov, detector="SPHEREx-D3"
        ))
        S.seen = f"url_{q_ra:.4f}_{q_dec:.4f}"
        S.result = dict(cached_stage(q_spec), nonce=S.seen)
        st.rerun()
    except Exception as e:
        pass

@st.cache_resource
def _band_jobs():
    """Spectrum jobs started as soon as a target's first view is ready, so they run while the page animates."""
    from concurrent.futures import ThreadPoolExecutor
    return {}, ThreadPoolExecutor(2)


def _band_args(r, key):
    tg = [("target", float(r["ra"]), float(r["dec"]), float(r.get("pmra") or 0), float(r.get("pmdec") or 0),
           float(r.get("epoch") or MID_EPOCH))] + [
        (str(i), float(o["ra"]), float(o["dec"]), float(o.get("pmra") or 0), float(o.get("pmdec") or 0),
         float(o.get("epoch") or MID_EPOCH)) for i, o in enumerate(r.get("objects") or [])]
    return (str(key or r["name"]), float(r["ra_c"]), float(r["dec_c"]), float(r["field_deg"]),
            r.get("detector") or "SPHEREx-D3", tuple(tg))


def start_bands(r, key):
    jobs, ex = _band_jobs()
    a = _band_args(r, key)
    if a not in jobs:
        if len(jobs) > 16:
            jobs.clear()
        jobs[a] = ex.submit(compute_bands, *a)
    return jobs[a]


@st.cache_data(show_spinner=False, max_entries=32)
def cached_bands(name, ra_c, dec_c, field_deg, det, targets):
    """All six detector ranges for the stage's field, and a spectrum for each target position."""
    return start_bands_result(name, ra_c, dec_c, field_deg, det, targets)


def start_bands_result(*a):
    jobs, ex = _band_jobs()
    f = jobs.get(a) or ex.submit(compute_bands, *a)
    jobs[a] = f
    return f.result()


def compute_bands(name, ra_c, dec_c, field_deg, det, targets):
    import hashlib
    import json
    from drift.bands import all_bands
    key = "bands_" + hashlib.md5(f"{ra_c:.5f}{dec_c:.5f}{field_deg:.5f}".encode()).hexdigest()[:10]
    pre = os.path.join(STAGE_DIR, f"{name}_bands.json")
    if name in PRESETS and os.path.exists(pre):
        with open(pre, encoding="utf-8") as fh:
            return json.load(fh)
    spec = pipeline.Spec(name=name, ra=ra_c, dec=dec_c, pmra=0.0, pmdec=0.0, epoch=float(MID_EPOCH),
                         fov_arcsec=field_deg * 3600.0, detector=det, key=key)
    return all_bands(spec, targets=targets)


@st.cache_data(show_spinner=False, max_entries=32, ttl=3600)
def cached_depth(ra_c, dec_c, field_deg):
    from drift.depth import depth_map
    return depth_map(ra_c, dec_c, field_deg)


if req and req.get("nonce") != S.seen and req.get("type") == "depth":
    S.seen = req["nonce"]
    r = S.result
    if r and r.get("nonce") == req.get("for") and "depth" not in r:
        try:
            S.result = dict(r, depth=cached_depth(round(float(r["ra_c"]), 5), round(float(r["dec_c"]), 5), round(float(r["field_deg"]), 5)))
        except Exception:
            import traceback
            traceback.print_exc()
            S.result = dict(r, depth=dict(objects=[], error="The distance catalogues did not answer. Try again in a moment."))
    st.rerun()

if req and req.get("nonce") != S.seen and req.get("type") == "bands":
    S.seen = req["nonce"]
    r = S.result
    if r and r.get("nonce") == req.get("for") and "bands" not in r:
        try:
            b = cached_bands(*_band_args(r, req.get("key")))
            S.result = dict(r, bands=b["bands"], spectra=b["spectra"])
        except Exception:
            import traceback
            traceback.print_exc()
            S.result = dict(r, bands=[], spectra={})
    st.rerun()

if req and req.get("nonce") != S.seen:
    S.seen = req["nonce"]
    S.error = None
    if req.get("type") == "home":
        S.result = None
    elif req.get("type") == "go":
        key = req.get("key")
        name_job = None
        if key in PRESETS:
            spec = pipeline.spec_from_target(PRESETS[key])
        else:
            fov = min(max(float(req.get("fov") or 180.0), 60.0), 5400.0)
            name_job = None
            req_name = req.get("name")
            if req_name in (None, "", "Selected area", "Selected patch"):
                # name the patch by what is actually in it (SIMBAD), looked up with a fast 3.0s timeout
                from concurrent.futures import ThreadPoolExecutor
                from drift.naming import name_field
                name_job = ThreadPoolExecutor(1).submit(name_field, round(float(req["ra"]), 4), round(float(req["dec"]), 4),
                                                        round(fov / 3600.0, 4))
                req_name = "Selected area"
            spec = pipeline.Spec(name=str(req_name)[:60], ra=float(req["ra"]), dec=float(req["dec"]),
                                 pmra=float(req.get("pmra") or 0.0), pmdec=float(req.get("pmdec") or 0.0),
                                 epoch=float(req.get("epoch") or 2000.0), fov_arcsec=fov, detector="SPHEREx-D3")
            spec = pipeline.framed(spec)
        try:
            payload = cached_stage(spec)
            payload = dict(payload, nonce=req["nonce"])
            if key not in PRESETS and name_job is not None:
                try:
                    payload["name"] = name_job.result(timeout=0.5)
                except Exception:
                    pass
            S.result = payload
            try:
                start_bands(payload, key)          # begin the five other detectors now, while the page animates
            except Exception:
                pass
        except pipeline.DataFetchError as e:
            S.result, S.error = None, f"No SPHEREx images here yet ({e}). Try another spot."
        except Exception as e:  # keep the page alive, and leave the full traceback in the app log
            import traceback
            traceback.print_exc()
            S.result, S.error = None, f"Something went wrong ({type(e).__name__}: {e})."
    st.rerun()
