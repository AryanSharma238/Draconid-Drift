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


def qopt(name):
    """Query-string number, or None when absent (the box then starts empty)."""
    try:
        return float(qp[name]) if name in qp else None
    except (TypeError, ValueError):
        return None


if "init" not in st.session_state:
    st.session_state.init = True
    st.session_state.preset = "Custom coordinates"
    st.session_state.ra = qopt("ra")                 # empty until a star button or the map fills them
    st.session_state.dec = qopt("dec")
    st.session_state.pmra = None
    st.session_state.pmdec = None
    st.session_state.epoch = None
    st.session_state.fov = min(max(qfloat("fov", 180), 90.0), 600.0)
    st.session_state.det = "SPHEREx-D2"
    st.session_state.name = "Custom position"
    st.session_state.key = "custom"


def set_preset(key):
    t = PRESETS[key]
    st.session_state.update(ra=t.ra, dec=t.dec, pmra=t.pmra, pmdec=t.pmdec, epoch=t.epoch,
                            fov=float(t.fov_arcsec), det=t.detector, name=t.name, key=t.key,
                            preset=t.name, snap_msg="")


def snap_to_star():
    """Re-centre on the brightest SIMBAD object within 90 arcsec, with its proper motion."""
    try:
        import pyvo
        r = 90 / 3600.0
        ra0, dec0 = st.session_state.ra, st.session_state.dec
        if ra0 is None or dec0 is None:
            st.session_state.snap_msg = "Enter RA and Dec (or click the map) first."
            return
        q = f"""SELECT b.main_id, b.ra, b.dec, b.pmra, b.pmdec, f.V AS vmag
                FROM basic AS b LEFT JOIN allfluxes AS f ON b.oid = f.oidref
                WHERE CONTAINS(POINT('ICRS', b.ra, b.dec), CIRCLE('ICRS', {ra0}, {dec0}, {r})) = 1"""
        rows = pyvo.dal.TAPService("https://simbad.cds.unistra.fr/simbad/sim-tap").search(q).to_table()
        best, key = None, None
        for x in rows:
            if np.ma.is_masked(x["ra"]) or np.ma.is_masked(x["dec"]):
                continue
            v = float(x["vmag"]) if not np.ma.is_masked(x["vmag"]) else 99.0
            if key is None or v < key:
                best, key = x, v
        if best is None:
            st.session_state.snap_msg = "No SIMBAD object within 90 arcsec of these coordinates."
            return
        pm = lambda c: 0.0 if np.ma.is_masked(best[c]) else float(best[c])
        off = 3600 * np.hypot((float(best["ra"]) - ra0) * np.cos(np.radians(dec0)), float(best["dec"]) - dec0)
        st.session_state.update(ra=float(best["ra"]), dec=float(best["dec"]), pmra=pm("pmra"), pmdec=pm("pmdec"),
                                epoch=2000.0, preset="Custom coordinates", name=str(best["main_id"]), key="custom")
        vtxt = "" if key >= 99 else f", V = {key:.1f}"
        st.session_state.snap_msg = (f"Centred on {str(best['main_id']).strip()} ({off:.0f} arcsec from the clicked "
                                     f"point{vtxt}). Position and proper motion are from SIMBAD, epoch 2000.")
    except Exception as e:
        st.session_state.snap_msg = f"Could not reach SIMBAD ({type(e).__name__}). Coordinates left unchanged."


# --------------------------------------------------------------- map -------
st.subheader("1. Pick a spot")
presets_now = []
for t in PRESETS.values():
    ra, dec = target_position(t, MID_EPOCH)
    presets_now.append(dict(key=t.key, name=t.name, ra=float(ra), dec=float(dec)))
init = dict(ra=None, dec=None, fov=st.session_state.fov, presets=presets_now)
if st.session_state.ra is not None and st.session_state.dec is not None:
    init.update(ra=float(st.session_state.ra), dec=float(st.session_state.dec))
html = open(os.path.join(HERE, "web", "sky_map.html"), encoding="utf-8").read()
show_html(html.replace("__INIT__", json.dumps(init)), 520)

# -------------------------------------------------------------- form -------
st.subheader("2. Target and options")
st.caption("Pick one of the five test stars, or type RA and Dec yourself (or use the map above).")
bcols = st.columns(len(PRESETS))
for col, t in zip(bcols, PRESETS.values()):
    col.button(t.name, key=f"pb_{t.key}", on_click=set_preset, args=(t.key,), use_container_width=True,
               type="primary" if st.session_state.preset == t.name else "secondary")
c1, c2, c3 = st.columns(3)
c1.number_input("RA (deg)", key="ra", value=None, format="%.6f", placeholder="empty: pick a star or type")
c2.number_input("Dec (deg)", key="dec", value=None, format="%.6f", placeholder="empty: pick a star or type")
c3.number_input("Field of view (arcsec)", key="fov", min_value=90.0, max_value=600.0, step=30.0)
d0, d1, d2, d3 = st.columns([2, 2, 2, 2])
d0.number_input("Proper motion RA* (mas/yr)", key="pmra", value=None, format="%.1f", placeholder="empty = 0")
d1.number_input("Proper motion Dec (mas/yr)", key="pmdec", value=None, format="%.1f", placeholder="empty = 0")
d2.number_input("Epoch of the coordinates", key="epoch", value=None, format="%.2f", placeholder="empty = now",
                help="Coordinates are moved from this epoch to the SPHEREx epoch before searching.")
d3.selectbox("Detector array", irsa.DETECTORS, key="det",
             format_func=lambda d: f"{d[-2:]} ({irsa.DETECTOR_RANGE[d]} µm)")
if st.session_state.preset in [t.name for t in PRESETS.values()]:
    t = next(t for t in PRESETS.values() if t.name == st.session_state.preset)
    st.info(t.story + (" " + t.epoch_note if t.epoch_note else ""))
st.button("Centre on the brightest star within 90 arcsec (SIMBAD)", on_click=snap_to_star,
          help="Fixes a click that missed the star, and fills in its proper motion.")
if st.session_state.get("snap_msg"):
    st.caption(st.session_state.snap_msg)
e0, e1, e2 = st.columns([2, 2, 2])
mode = e0.radio("Verdict mode", ["Check this target", "Blind scan of the whole patch"],
                help="Target check searches near the expected position and motion. "
                     "Blind scan searches every position and speed (slower, stricter).")
same = e1.checkbox("Same colour only", value=False,
                   help="Keep only frames at nearly the same wavelength so brightness changes are real.")
demo = e2.checkbox("Use synthetic demo data", value=False,
                   help="Simulated frames, for trying the tool without IRSA. Clearly labelled.")
view = e2.selectbox("Patch shown (arcsec)", [40, 60, 90, 120, "Full field"], index=1,
                    help="Zoom the frames to a small patch around the target so a pixel or two of motion is "
                         "easy to see against the stars around it. The verdict always uses the full field.")
run = st.button("Show frames and check motion", type="primary")

# -------------------------------------------------------------- run --------
if run and (st.session_state.ra is None or st.session_state.dec is None):
    st.warning("Pick one of the five stars above, or enter RA and Dec, then press the button again.")
    run = False
if run:
    spec = pipeline.Spec(name=st.session_state.name if st.session_state.preset != "Custom coordinates" else "Custom position",
                         ra=st.session_state.ra, dec=st.session_state.dec,
                         pmra=st.session_state.pmra or 0.0, pmdec=st.session_state.pmdec or 0.0,
                         epoch=st.session_state.epoch or float(MID_EPOCH), fov_arcsec=st.session_state.fov,
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
                           n_null=60 if blind else 300, progress=dl, null_progress=nl,
                           view_arcsec=None if view == "Full field" else float(view))
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
