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

req = stage(presets=presets, mid=float(MID_EPOCH), result=S.result, error=S.error, key="stage", default=None)

if req and req.get("nonce") != S.seen:
    S.seen = req["nonce"]
    S.error = None
    if req.get("type") == "home":
        S.result = None
    elif req.get("type") == "go":
        key = req.get("key")
        if key in PRESETS:
            spec = pipeline.spec_from_target(PRESETS[key])
        else:
            fov = min(max(float(req.get("fov") or 180.0), 60.0), 5400.0)
            if req.get("name") in (None, "", "Selected area", "Selected patch"):
                from astropy.coordinates import SkyCoord, get_constellation
                import astropy.units as u
                c = SkyCoord(float(req["ra"]) * u.deg, float(req["dec"]) * u.deg)
                near = [t.name for t in PRESETS.values()
                        if c.separation(SkyCoord(*target_position(t, MID_EPOCH), unit="deg")).arcmin < 30]
                req["name"] = f"Near {near[0]}" if near else f"Patch in {get_constellation(c)}"
            spec = pipeline.Spec(name=str(req.get("name") or "Selected area")[:60], ra=float(req["ra"]), dec=float(req["dec"]),
                                 pmra=float(req.get("pmra") or 0.0), pmdec=float(req.get("pmdec") or 0.0),
                                 epoch=float(req.get("epoch") or 2000.0), fov_arcsec=fov, detector="SPHEREx-D4")
        try:
            big = spec.fov_arcsec > 400
            res = pipeline.run(spec, mode="target", n_null=120 if big else 300, build=False, max_frames=120)
            ra_now, dec_now = pipeline.propagate(spec.ra, spec.dec, spec.pmra, spec.pmdec, spec.epoch, MID_EPOCH)
            payload = build_stage(res, spec.name, float(ra_now), float(dec_now), spec.pmra, spec.pmdec, spec.epoch)
            payload["nonce"] = req["nonce"]
            S.result = payload
        except pipeline.DataFetchError as e:
            S.result, S.error = None, f"No SPHEREx images here yet ({e}). Try another spot."
        except Exception as e:  # keep the page alive
            S.result, S.error = None, f"Something went wrong ({type(e).__name__}: {e})."
    st.rerun()
