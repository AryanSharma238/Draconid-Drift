"""Draconid Drift: pick a spot on the sky, watch SPHEREx frames blink smoothly,
and get an honest verdict on whether something moved."""
import json
import os

import numpy as np
import streamlit as st
import streamlit.components.v1 as components

from drift import irsa, pipeline
from drift.targets import PRESETS, target_position
from drift.demo import MID_EPOCH
from drift.verdict_html import render as verdict_html

HERE = os.path.dirname(__file__)


def show_html(html, height):
    """st.iframe on current Streamlit; components.html on older versions."""
    if hasattr(st, "iframe"):
        st.iframe(html, height=int(height))
    else:
        components.html(html, height=int(height))


def qfloat(name, default):
    try:
        return float(st.query_params.get(name, default))
    except (TypeError, ValueError):
        return float(default)

st.set_page_config(page_title="Draconid Drift", layout="wide")
st.title("Draconid Drift")
st.caption("SPHEREx moving-object viewer: pick a spot, watch the frames, check the motion.")

# ------------------------------------------------------------- state -------
qp = st.query_params
if "init" not in st.session_state:
    st.session_state.init = True
    st.session_state.preset = "Custom coordinates"
    st.session_state.ra = qfloat("ra", 269.448503)
    st.session_state.dec = qfloat("dec", 4.739420)
    st.session_state.pmra = 0.0
    st.session_state.pmdec = 0.0
    st.session_state.epoch = float(MID_EPOCH)
    st.session_state.fov = min(max(qfloat("fov", 240), 90.0), 600.0)
    st.session_state.det = "SPHEREx-D2"
    st.session_state.name = "Custom position"
    st.session_state.key = "custom"
    if "ra" not in qp:                       # first visit: start on Barnard's Star preset
        st.session_state.preset = PRESETS["barnard"].name


def apply_preset():
    name = st.session_state.preset
    if name == "Custom coordinates":
        return
    t = next(t for t in PRESETS.values() if t.name == name)
    st.session_state.update(ra=t.ra, dec=t.dec, pmra=t.pmra, pmdec=t.pmdec, epoch=t.epoch,
                            fov=float(t.fov_arcsec), det=t.detector, name=t.name, key=t.key)


if st.session_state.get("preset") in [t.name for t in PRESETS.values()] and st.session_state.get("_first", True):
    apply_preset(); st.session_state._first = False

# --------------------------------------------------------------- map -------
st.subheader("1. Pick a spot")
presets_now = []
for t in PRESETS.values():
    ra, dec = target_position(t, MID_EPOCH)
    presets_now.append(dict(key=t.key, name=t.name, ra=float(ra), dec=float(dec)))
init = dict(ra=None, dec=None, fov=st.session_state.fov, presets=presets_now)
if "ra" in qp:
    init.update(ra=qfloat("ra", 0.0), dec=qfloat("dec", 0.0))
html = open(os.path.join(HERE, "web", "sky_map.html"), encoding="utf-8").read()
show_html(html.replace("__INIT__", json.dumps(init)), 520)

# -------------------------------------------------------------- form -------
st.subheader("2. Target and options")
c0, c1, c2, c3 = st.columns([2, 2, 2, 2])
c0.selectbox("Preset", ["Custom coordinates"] + [t.name for t in PRESETS.values()],
             key="preset", on_change=apply_preset)
c1.number_input("RA (deg)", key="ra", format="%.6f")
c2.number_input("Dec (deg)", key="dec", format="%.6f")
c3.number_input("Field of view (arcsec)", key="fov", min_value=90.0, max_value=600.0, step=30.0)
d0, d1, d2, d3 = st.columns([2, 2, 2, 2])
d0.number_input("Proper motion RA* (mas/yr)", key="pmra", format="%.1f")
d1.number_input("Proper motion Dec (mas/yr)", key="pmdec", format="%.1f")
d2.number_input("Epoch of the coordinates", key="epoch", format="%.2f",
                help="Coordinates are moved from this epoch to the SPHEREx epoch before searching.")
d3.selectbox("Detector array", irsa.DETECTORS, key="det",
             format_func=lambda d: f"{d[-2:]} ({irsa.DETECTOR_RANGE[d]} µm)")
if st.session_state.preset in [t.name for t in PRESETS.values()]:
    t = next(t for t in PRESETS.values() if t.name == st.session_state.preset)
    st.info(t.story + (" " + t.epoch_note if t.epoch_note else ""))
e0, e1, e2 = st.columns([2, 2, 2])
mode = e0.radio("Verdict mode", ["Check this target", "Blind scan of the whole patch"],
                help="Target check searches near the expected position and motion. "
                     "Blind scan searches every position and speed (slower, stricter).")
same = e1.checkbox("Same colour only", value=False,
                   help="Keep only frames at nearly the same wavelength so brightness changes are real.")
demo = e2.checkbox("Use synthetic demo data", value=False,
                   help="Simulated frames, for trying the tool without IRSA. Clearly labelled.")
run = st.button("Show frames and check motion", type="primary")

# -------------------------------------------------------------- run --------
if run:
    spec = pipeline.Spec(name=st.session_state.name if st.session_state.preset != "Custom coordinates" else "Custom position",
                         ra=st.session_state.ra, dec=st.session_state.dec,
                         pmra=st.session_state.pmra, pmdec=st.session_state.pmdec,
                         epoch=st.session_state.epoch, fov_arcsec=st.session_state.fov,
                         detector=st.session_state.det,
                         key=st.session_state.key if st.session_state.preset != "Custom coordinates" else "custom")
    blind = mode.startswith("Blind")
    bar = st.progress(0.0, text="Finding SPHEREx images…")
    try:
        def dl(done, total):
            bar.progress(min(done / total, 1.0) * 0.6, text=f"Downloading cutouts {done}/{total}…")

        def nl(k, n):
            bar.progress(0.6 + 0.4 * k / n, text="Checking the motion against scrambled data…")

        res = pipeline.run(spec, mode="blind" if blind else "target", same_colour=same, demo=demo,
                           n_null=60 if blind else 300, progress=dl, null_progress=nl)
        bar.empty()
        st.session_state.result = res
    except irsa.IrsaError as e:
        bar.empty()
        st.session_state.pop("result", None)
        st.error(f"Could not get SPHEREx data from IRSA: {e}")
        st.write("Try again in a moment, pick another detector, or tick **Use synthetic demo data**.")
    except Exception as e:  # keep the app alive, show what happened
        bar.empty()
        st.session_state.pop("result", None)
        st.error(f"Something went wrong: {e}")

res = st.session_state.get("result")
if res:
    st.subheader("3. Frames")
    cube = res["cube"]
    if cube.source == "SYNTHETIC":
        st.warning("SYNTHETIC DEMO DATA: simulated frames, not real SPHEREx images.")
    for n in res["notes"]:
        st.caption(n)
    if not res["in_field"]:
        st.caption("The target's expected position is outside this field, so no marker is drawn.")
    P = res["payload"]
    ph = P["ny"] * P["zoom"] + 190
    tpl = open(os.path.join(HERE, "web", "player.html"), encoding="utf-8").read()
    capw = P["nx"] * P["zoom"]
    show_html(tpl.replace("__DATA__", json.dumps(P)).replace("__CAPW__", str(capw)), ph)
    st.subheader("4. Verdict")
    show_html(verdict_html(res["verdict"]), 250)
    with st.expander("Details"):
        v = res["verdict"]
        st.write(f"Best track starts at pixel ({v['best']['x']}, {v['best']['y']}) and moves "
                 f"({v['best']['vx']:.2f}, {v['best']['vy']:.2f}) px/yr. "
                 f"Expected motion for this target: ({res['v_expected'][0]:.2f}, {res['v_expected'][1]:.2f}) px/yr.")
        st.write(f"{cube.n} frames, wavelengths {np.nanmin(cube.wl):.2f}-{np.nanmax(cube.wl):.2f} µm, "
                 f"{v['n_null']} scrambles. Pass thresholds are heuristics; see the README.")
