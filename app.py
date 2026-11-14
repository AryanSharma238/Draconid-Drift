"""Draconid Drift: pick a spot on the sky, watch SPHEREx frames blink smoothly,
and get an honest verdict on whether something moved."""
import json
import os

import numpy as np
import streamlit as st
import streamlit.components.v1 as components

from drift import pipeline
from drift.demo import MID_EPOCH
from drift.targets import PRESETS, target_position
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


st.set_page_config(page_title="Draconid Drift · SPHEREx & Planet X", layout="wide")

st.markdown("""
<style>
  .block-container { padding-top: 1.8rem; padding-bottom: 2.5rem; max-width: 1280px; }
  .hero-banner {
    background: linear-gradient(135deg, #0b132b 0%, #070a14 60%, #0f172a 100%);
    border: 1px solid #1e293b;
    border-radius: 16px;
    padding: 20px 26px;
    margin-bottom: 18px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 14px;
  }
  .hero-title {
    font-size: 28px;
    font-weight: 800;
    letter-spacing: -0.02em;
    color: #f8fafc;
    margin: 0;
  }
  .hero-sub {
    font-size: 14px;
    color: #94a3b8;
    margin: 4px 0 0;
  }
  .badge-pill {
    display: inline-block;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 5px 11px;
    border-radius: 999px;
    background: rgba(56, 189, 248, 0.12);
    border: 1px solid rgba(56, 189, 248, 0.35);
    color: #38bdf8;
  }
</style>
<div class="hero-banner">
  <div>
    <div class="hero-title">Draconid Drift</div>
    <div class="hero-sub">SPHEREx Moving-Object &amp; Planet X Blink Comparator · Reprojected North-Up Multi-Epoch Cutouts &amp; Empirical FAP Verdict</div>
  </div>
  <div>
    <span class="badge-pill">NASA Space Apps · Planet X &amp; SPHEREx</span>
  </div>
</div>
""", unsafe_allow_html=True)

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
    if "preset" in qp and qp["preset"] in PRESETS:
        st.session_state.preset = PRESETS[qp["preset"]].name
    elif "ra" not in qp:  # first visit: start on Barnard's Star preset
        st.session_state.preset = PRESETS["barnard"].name


def apply_preset():
    name = st.session_state.preset
    if name == "Custom coordinates":
        return
    t = next(t for t in PRESETS.values() if t.name == name)
    st.session_state.update(ra=t.ra, dec=t.dec, pmra=t.pmra, pmdec=t.pmdec, epoch=t.epoch,
                            fov=float(t.fov_arcsec), det=t.detector, name=t.name, key=t.key)


if st.session_state.get("preset") in [t.name for t in PRESETS.values()] and st.session_state.get("_first", True):
    apply_preset()
    st.session_state._first = False

# --------------------------------------------------------------- map -------
with st.expander("1. Interactive All-Sky Map (Aladin Lite · DSS2 / 2MASS / AllWISE / SPHEREx)", expanded=False):
    presets_now = []
    for t in PRESETS.values():
        ra, dec = target_position(t, MID_EPOCH)
        presets_now.append(dict(key=t.key, name=t.name, ra=float(ra), dec=float(dec)))
    init = dict(ra=st.session_state.ra, dec=st.session_state.dec, fov=st.session_state.fov, presets=presets_now)
    html = open(os.path.join(HERE, "web", "sky_map.html"), encoding="utf-8").read()
    show_html(html.replace("__INIT__", json.dumps(init)), 500)

# -------------------------------------------------------------- form -------
st.subheader("Target & Search Configuration")
c0, c1, c2, c3 = st.columns([2.2, 1.8, 1.8, 1.8])
c0.selectbox("Preset Target", ["Custom coordinates"] + [t.name for t in PRESETS.values()],
             key="preset", on_change=apply_preset)
c1.number_input("RA (deg)", key="ra", format="%.6f")
c2.number_input("Dec (deg)", key="dec", format="%.6f")
c3.number_input("Field of view (arcsec)", key="fov", min_value=90.0, max_value=600.0, step=30.0)

d0, d1, d2, d3 = st.columns([2.2, 1.8, 1.8, 1.8])
d0.selectbox("SPHEREx Detector Band", pipeline.DETECTORS, key="det",
             format_func=lambda d: f"{d} ({pipeline.DETECTOR_RANGE[d]} µm)")
d1.number_input("Proper motion RA* (mas/yr)", key="pmra", format="%.1f")
d2.number_input("Proper motion Dec (mas/yr)", key="pmdec", format="%.1f")
d3.number_input("Coordinate Epoch (yr)", key="epoch", format="%.2f",
                help="Coordinates are propagated from this epoch to the SPHEREx observation epoch (~2026.3).")

if st.session_state.preset in [t.name for t in PRESETS.values()]:
    t = next(t for t in PRESETS.values() if t.name == st.session_state.preset)
    st.info(f"**{t.name}** — {t.story}" + (f" *{t.epoch_note}*" if t.epoch_note else ""))

e0, e1, e2, e3 = st.columns([2.5, 1.5, 1.5, 2.0])
mode = e0.radio("Search Mode", ["Check this target", "Blind scan of the whole patch"],
                horizontal=True,
                help="Target check searches near the expected position and motion. "
                     "Blind scan searches every position and speed across the patch.")
same = e1.checkbox("Same colour only", value=False,
                   help="Keep only frames inside the densest wavelength window (±2%) so spectral slope doesn't mimic variability.")
demo = e2.checkbox("Use synthetic demo data", value=False,
                   help="Simulated SPHEREx frames with rotated detectors, zodi, and flags.")
run = e3.button("▶ Load Frames & Check Motion", type="primary", use_container_width=True)

# Auto-load default preset on first visit so the viewer is immediately visible
auto_run = False
if "result" not in st.session_state and not st.session_state.get("_auto_loaded", False):
    st.session_state._auto_loaded = True
    auto_run = True

# -------------------------------------------------------------- run --------
if run or auto_run:
    spec = pipeline.Spec(
        name=st.session_state.name if st.session_state.preset != "Custom coordinates" else "Custom position",
        ra=st.session_state.ra, dec=st.session_state.dec,
        pmra=st.session_state.pmra, pmdec=st.session_state.pmdec,
        epoch=st.session_state.epoch, fov_arcsec=st.session_state.fov,
        detector=st.session_state.det,
        key=st.session_state.key if st.session_state.preset != "Custom coordinates" else "custom",
    )
    blind = mode.startswith("Blind")
    bar = st.progress(0.0, text="Loading SPHEREx cutouts & reprojecting onto common North-Up TAN grid…")
    try:
        def dl(done, total):
            bar.progress(min(done / total, 1.0) * 0.6, text=f"Downloading cutouts {done}/{total}…")

        def nl(k, n):
            bar.progress(0.6 + 0.4 * k / n, text=f"Running time-scrambled null trials ({k}/{n})…")

        res = pipeline.run(spec, mode="blind" if blind else "target", same_colour=same, demo=demo,
                           n_null=60 if blind else 300, progress=dl, null_progress=nl)
        bar.empty()
        st.session_state.result = res
    except pipeline.DataFetchError as e:
        bar.empty()
        st.session_state.pop("result", None)
        st.error(f"Could not fetch SPHEREx data: {e}")
        st.write("Try again in a moment, pick another detector, or tick **Use synthetic demo data**.")
    except Exception as e:
        bar.empty()
        st.session_state.pop("result", None)
        st.error(f"Something went wrong: {e}")

res = st.session_state.get("result")
if res:
    cube = res["cube"]
    if cube.source == "SYNTHETIC":
        st.warning("SYNTHETIC DEMO DATA: simulated frames, not real SPHEREx images.")

    st.subheader("Blink Comparator & Co-Add Viewer")
    if res["notes"]:
        st.caption(" · ".join(res["notes"]) +
                   ("" if res["in_field"] else " · Target expected position is outside this field."))

    P = res["payload"]
    ph = P["ny"] * P["zoom"] + 235
    tpl = open(os.path.join(HERE, "web", "player.html"), encoding="utf-8").read()
    capw = P["nx"] * P["zoom"]
    show_html(tpl.replace("__DATA__", json.dumps(P)).replace("__CAPW__", str(capw)), ph)

    st.subheader("Statistical Motion Verdict")
    show_html(verdict_html(res["verdict"]), 265)

    # --- Task 08 M4: Measured Proper Motion vs Gaia/Literature Reference & WiseView Deep Link ---
    ast = P.get("astrometry")
    ra_c, dec_c = res["info"]["center"]
    wv_url = (
        f"http://byw.tools/wiseview#ra={ra_c:.6f}&dec={dec_c:.6f}"
        f"&size={int(st.session_state.fov)}&band=2&speed=500&minbright=-50.0000"
        f"&maxbright=500.0000&window=0.5&diff_window=1&linear=1&zoom=9&invert=1"
    )

    col_pm, col_lc = st.columns([1.15, 1.35])
    with col_pm:
        st.markdown("#### Measured Astrometry vs. Catalog (Task 08 M4)")
        if ast:
            cat_pmra, cat_pmdec = st.session_state.pmra, st.session_state.pmdec
            cat_tot = float(np.hypot(cat_pmra, cat_pmdec) / 1000.0)
            st.markdown(
                f"| Metric | SPHEREx Measured ({ast['n_passes']} passes) | Catalog Reference |\n"
                f"|---|---|---|\n"
                f"| **$\mu_{{\\alpha*}}$ (mas/yr)** | `{ast['pmra_masyr']:+.0f} ± {ast['epmra_masyr']:.0f}` | `{cat_pmra:+.1f}` |\n"
                f"| **$\mu_\\delta$ (mas/yr)** | `{ast['pmdec_masyr']:+.0f} ± {ast['epmdec_masyr']:.0f}` | `{cat_pmdec:+.1f}` |\n"
                f"| **Total PM (″/yr)** | `{ast['pm_total_arcsec_yr']:.2f}″/yr` | `{cat_tot:.2f}″/yr` |"
            )
        else:
            st.caption("Insufficient multi-pass centroids for linear proper-motion fit.")
        st.markdown(
            f"🔗 **[Open current epoch position ({ra_c:.5f}°, {dec_c:+.5f}°) in WiseView ↗]({wv_url})**  \n"
            f"<small style='color:#94a3b8'>Note: WiseView links anchored on 2010 coordinates (e.g. 133.786° for WISE 0855) "
            f"miss the target's 2026 SPHEREx position by >100″. This link uses the PM-propagated SPHEREx center.</small>",
            unsafe_allow_html=True,
        )

    with col_lc:
        st.markdown("#### SPHEREx Photometry: Spectrum (F3) & λ-Matched Light Curve (E4)")
        spec_pts = P.get("spectrum") or []
        lc_pts = P.get("lightcurve") or []
        tab_lc, tab_sp = st.tabs(["λ-Matched Differential Light Curve (Δmag vs Time)", "Aperture Spectrum (Flux vs λ)"])
        with tab_lc:
            if lc_pts:
                import pandas as pd
                df_lc = pd.DataFrame(lc_pts)
                st.scatter_chart(df_lc, x="mjd", y="dmag", color="wl", height=210)
            else:
                st.caption("Not enough cross-pass wavelength matches for differential light curve.")
        with tab_sp:
            if spec_pts:
                import pandas as pd
                df_sp = pd.DataFrame(spec_pts)
                st.scatter_chart(df_sp, x="wl", y="flux", color="pass_idx", height=210)
            else:
                st.caption("No valid aperture photometry points extracted.")

    with st.expander("Technical Details & Kinematics"):
        v = res["verdict"]
        st.write(
            f"**Best-fit trajectory:** starts at pixel `({v['best']['x']}, {v['best']['y']})` at the mean epoch "
            f"and moves at `({v['best']['vx']:+.2f}, {v['best']['vy']:+.2f})` px/yr "
            f"(`{np.hypot(v['best']['vx'], v['best']['vy']) * 6.15:.2f}` arcsec/yr).  \n"
            f"**Catalog expected motion:** `({res['v_expected'][0]:+.2f}, {res['v_expected'][1]:+.2f})` px/yr "
            f"around center pixel `({res['center'][0]:.1f}, {res['center'][1]:.1f})`."
        )
        st.write(
            f"**Dataset:** `{cube.n}` cleaned frames (`{cube.detector}`, source: `{cube.source}`), "
            f"wavelengths `{np.nanmin(cube.wl):.2f}–{np.nanmax(cube.wl):.2f} µm`, "
            f"evaluated against `{v['n_null']}` time-permutation scrambles."
        )
