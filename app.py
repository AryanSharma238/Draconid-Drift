"""Draconid Drift: one full-screen sky. Pick a spot, watch SPHEREx frames, see whether something moves."""
import os
import warnings

import streamlit as st
import streamlit.components.v1 as components
from astropy.utils.exceptions import AstropyWarning

from drift import irsa, pipeline
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
                        epoch=t.epoch, fov=float(t.fov_arcsec)))

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
            spec = pipeline.Spec(name=str(req.get("name") or "Selected patch")[:60], ra=float(req["ra"]), dec=float(req["dec"]),
                                 pmra=float(req.get("pmra") or 0.0), pmdec=float(req.get("pmdec") or 0.0),
                                 epoch=float(req.get("epoch") or 2000.0), fov_arcsec=180.0, detector="SPHEREx-D2")
        try:
            res = pipeline.run(spec, mode="target", n_null=300, build=False)
            payload = build_stage(res, spec.name, spec.ra, spec.dec)
            payload["nonce"] = req["nonce"]
            S.result = payload
        except irsa.IrsaError:
            S.result, S.error = None, "SPHEREx archive did not answer. Try again in a moment."
        except Exception as e:  # keep the page alive
            S.result, S.error = None, f"Something went wrong ({type(e).__name__})."
    st.rerun()
