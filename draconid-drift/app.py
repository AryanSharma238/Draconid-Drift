"""Draconid Drift — a public sky-change explorer for NASA SPHEREx.
NASA Space Apps Challenge 2026 · "Planet X and SPHEREx"

Multipage app: Home · Flipbook · Motion · Spectrum · Discoveries · Sky & Data.
The loaded target lives in session state, so switching pages never re-downloads data.
"""
from __future__ import annotations

import json
import urllib.parse

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from core import catalogs, detect, imaging, player, style
from core.data import (BANDS, PRESETS, assign_epochs, fetch_many, mjd_to_date, mjd_to_year,
                       preset_target, query_frames, resolve_target, select_frames)

APP_NAME = "Draconid Drift"
NAME_A, NAME_B = "DRACONID", "DRIFT"
st.set_page_config(page_title=f"{APP_NAME} · SPHEREx Sky-Change Explorer", page_icon="☄",
                   layout="wide", initial_sidebar_state="collapsed")
style.inject()

GRAY = [[0, "#000000"], [0.5, "#5f5f5f"], [1, "#FFFFFF"]]
DIFF = [[0, "#4AA3FF"], [0.5, "#000000"], [1, "#E8A33D"]]
BAND_COLORS = {"D1": "#2F6FDE", "D2": "#2AA0B8", "D3": "#4E9F5B",
               "D4": "#8E5BC9", "D5": "#E8A33D", "D6": "#D9503F"}
FOOT = ("Data: NASA/JPL-Caltech SPHEREx via NASA/IPAC IRSA (DR1 + QR3) · Gaia DR3 (ESA) · "
        "Aladin Lite & SIMBAD (CDS) · SkyBoT (IMCCE). Built for the NASA Space Apps Challenge 2026.")

# ── Session state ──
qp = st.query_params
ss = st.session_state
ss.setdefault("target", qp.get("target", "WISE 0855"))
ss.setdefault("result", None)
ss.setdefault("autorun", "target" in qp)
ss.setdefault("derived", {})
ss.setdefault("cfg", dict(fov=float(qp.get("fov", 12.0)), per_cell=2, bands=list(BANDS),
                          k_sigma=6.0, skybot=False, native=False, flow=True))
CFG = ss.cfg


# ── Data pipeline (cached) ───────────────────────────────────────────────────
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def cached_resolve(text: str):
    return preset_target(text) if text in PRESETS else resolve_target(text)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def cached_query(ra: float, dec: float) -> pd.DataFrame:
    from astropy.coordinates import SkyCoord
    return query_frames(SkyCoord(ra, dec, unit="deg"))


@st.cache_data(ttl=6 * 3600, show_spinner=False, max_entries=8)
def cached_fetch(rows_json: str, ra: float, dec: float, size: float):
    return fetch_many(json.loads(rows_json), ra, dec, size)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def cached_gaia(ra, dec, radius):
    from astropy.coordinates import SkyCoord
    try:
        return catalogs.gaia_sources(SkyCoord(ra, dec, unit="deg"), radius)
    except Exception:
        return pd.DataFrame()


def pipeline(text: str):
    with st.status("Searching the SPHEREx archive…", expanded=True) as status:
        tgt = cached_resolve(text)
        st.write(f"Resolved **{tgt.name}** via {tgt.source}"
                 + (f" · proper motion {tgt.total_pm:,.0f} mas/yr" if tgt.total_pm else ""))
        c_now = tgt.at(2025.9)
        frames = cached_query(round(c_now.ra.deg, 6), round(c_now.dec.deg, 6))
        if frames.empty:
            status.update(label="No SPHEREx images cover this position yet.", state="error")
            return None
        frames["epoch"] = assign_epochs(frames["mjd"].values)
        st.write(f"Found **{len(frames)}** spectral images across **{frames['epoch'].nunique()}** "
                 f"visits ({mjd_to_date(frames['mjd'].min())} → {mjd_to_date(frames['mjd'].max())}).")
        center = tgt.at(mjd_to_year(float(frames["mjd"].median())))  # motion-aware centre
        pick = select_frames(frames, CFG["bands"] or list(BANDS), CFG["per_cell"])
        size = CFG["fov"] / 60.0
        st.write(f"Downloading **{len(pick)}** server-side cutouts ({CFG['fov']:.1f}′ field)…")
        got = cached_fetch(json.dumps(pick.to_dict("records"), default=float),
                           center.ra.deg, center.dec.deg, size)
        good = [f for f in got if "error" not in f and f.get("img") is not None and f["img"].size]
        if not good:
            err = next((f.get("error") for f in got if "error" in f), "unknown error")
            status.update(label=f"Cutout download failed: {err}", state="error")
            return None
        st.write("Re-projecting onto a common north-up grid…")
        w, shape = imaging.target_grid(center, size)
        for f in good:
            f["grid"] = imaging.regrid(f, w, shape)
            f["pos"] = tgt.at(mjd_to_year(f["mjd"]))
        st.write("Fetching Gaia DR3 for proper-motion overlays…")
        gaia = cached_gaia(center.ra.deg, center.dec.deg, size * 0.75)
        status.update(label=f"Ready · {len(good)} frames of {tgt.name}", state="complete", expanded=False)
    return dict(target=tgt, frames=good, all_frames=frames, w=w, shape=shape, center=center,
                gaia=gaia, size=size, fov=CFG["fov"])


def derived(name, fn):
    """Memoise heavy per-target products across page switches."""
    key = (id(ss.result), name)
    if key not in ss.derived:
        ss.derived[key] = fn()
    return ss.derived[key]


# ── Navigation ───────────────────────────────────────────────────────────────
def page_home(): home()
def page_flip(): flip()
def page_motion(): motion()
def page_spectrum(): spectrum()
def page_disc(): discoveries()
def page_data(): skydata()


PAGES = {
    "home": st.Page(page_home, title="Home", url_path="home", default=True),
    "flip": st.Page(page_flip, title="Flipbook", url_path="flipbook"),
    "motion": st.Page(page_motion, title="Motion", url_path="motion"),
    "spectrum": st.Page(page_spectrum, title="Spectrum", url_path="spectrum"),
    "disc": st.Page(page_disc, title="Discoveries", url_path="discoveries"),
    "data": st.Page(page_data, title="Sky & Data", url_path="sky-data"),
}


def navbar(on_hero: bool = False):
    with st.container(key="navbar_hero" if on_hero else "navbar"):
        l1, l2, l3, mid, r1, r2, r3 = st.columns([1, 1, 1.05, 2.3, 1.15, 1.1, 1.65], vertical_alignment="center")
        l1.page_link(PAGES["flip"], label="Flipbook")
        l2.page_link(PAGES["motion"], label="Motion")
        l3.page_link(PAGES["spectrum"], label="Spectrum")
        with mid:
            st.markdown(f'<div class="wordmark"><b>{NAME_A}</b><span>{NAME_B}</span>{style.MARK}</div>',
                        unsafe_allow_html=True)
            st.page_link(PAGES["home"], label=" ")  # whole wordmark area returns home
        r1.page_link(PAGES["disc"], label="Discoveries")
        r2.page_link(PAGES["data"], label="Sky & Data")
        with r3:
            with st.container(key="navcta"):
                st.page_link(PAGES["flip"], label="Start exploring")


def choose(name: str):
    ss.target = name
    ss.autorun = True


def target_bar():
    """Compact search + presets + settings shown on every analysis page."""
    c1, c2 = st.columns([5, 1])
    with c1:
        st.text_input("Target", key="target", label_visibility="collapsed",
                      placeholder="Object name or coordinates, e.g. 08h55m10.8s −07d14m42s")
    with c2:
        run = st.button("Explore", type="primary", width="stretch")
    pc = st.columns(len(PRESETS))
    for col, name in zip(pc, PRESETS):
        col.button(name, width="stretch", on_click=choose, args=(name,), key=f"p_{name}")
    with st.expander("Settings", expanded=False):
        s1, s2, s3 = st.columns(3)
        CFG["fov"] = s1.slider("Field of view (arcmin)", 2.0, 30.0, CFG["fov"], 0.5,
                               help="Larger fields show more sky and catch faster movers, but load slower.")
        CFG["per_cell"] = s2.slider("Frames per visit & band", 1, 6, CFG["per_cell"],
                                    help="More frames = deeper, cleaner images and spectra; slower load.")
        CFG["bands"] = s3.multiselect("Detector bands", list(BANDS), default=CFG["bands"],
                                      format_func=lambda b: f"{b} · {BANDS[b][0]:.2f}–{BANDS[b][1]:.2f} µm")
        s4, s5 = st.columns(2)
        CFG["k_sigma"] = s4.slider("Detection threshold (σ)", 3.0, 12.0, CFG["k_sigma"], 0.5)
        CFG["skybot"] = s5.toggle("Cross-match known asteroids (IMCCE SkyBoT)", value=CFG["skybot"])
        CFG["flow"] = s4.toggle("Motion-compensated in-betweens (optical flow)", value=CFG["flow"],
                                help="Off: plain crossfade between visits. On: classic bidirectional optical "
                                     "flow (OpenCV Farneback) warps pixels along their motion. No machine learning.")
        CFG["native"] = s5.toggle("Native pixels (no upsampling)", value=CFG["native"],
                                  help="Off: 4× spline display for smooth, crisp stars. On: raw 6.2″ pixels.")
    if run or ss.autorun:
        ss.autorun = False
        st.query_params.update({"target": ss.target, "fov": f"{CFG['fov']:g}"})
        try:
            ss.result = pipeline(ss.target)
            ss.derived = {}
        except Exception as e:  # noqa: BLE001
            st.error(f"{e}")
            ss.result = None
    return ss.result


def summary(R):
    tgt, frames = R["target"], R["frames"]
    m = [f["mjd"] for f in frames]
    style.stats([
        (f"{len(frames)}", "frames analysed"),
        (f"{len({f['epoch'] for f in frames})}", "visits"),
        (f"{len({f['band'] for f in frames})}/6", "detector bands"),
        (f"{(max(m) - min(m)):.0f} d", "time baseline"),
        (f"{tgt.total_pm / 1000:.2f}″/yr" if tgt.total_pm else "—", "catalog proper motion"),
    ])
    st.write("")


def analysis_page(kicker: str, title: str, lead: str):
    """Shared header for analysis pages. Returns the loaded result or None."""
    navbar()
    style.section(kicker, title, lead)
    R = target_bar()
    if R is None:
        st.markdown('<p class="note">Pick a target above to load SPHEREx data. '
                    'The five buttons are the official challenge targets.</p>', unsafe_allow_html=True)
        return None
    summary(R)
    return R


# ── Figure helpers ───────────────────────────────────────────────────────────
def target_xy(R, year):
    x, y = R["w"].world_to_pixel(R["target"].at(year))
    return float(x), float(y)


def gaia_xy(R, year, min_pm=50):
    g = R["gaia"]
    if g is None or g.empty:
        return [], [], []
    g = catalogs.propagate(g[g["pm_total"] >= min_pm], year)
    if g.empty:
        return [], [], []
    from astropy.coordinates import SkyCoord
    x, y = R["w"].world_to_pixel(SkyCoord(g["ra_t"].values, g["dec_t"].values, unit="deg"))
    return list(x), list(y), [f"Gaia {int(s)} · {p:.0f} mas/yr" for s, p in zip(g["Source"], g["pm_total"])]


def sky(img_or_cube, cscale, zmin, zmax):
    cube = img_or_cube if img_or_cube.ndim == 3 else img_or_cube[None]
    if cscale is GRAY:
        cube = imaging.asinh_stretch(cube, zmin, zmax)
        zmin, zmax = 0.0, 1.0
    if CFG["native"]:
        ny, nx = cube.shape[1:]
        return cube, np.arange(nx), np.arange(ny), zmin, zmax
    up, xs, ys = imaging.upsample(cube, 4)
    return up, xs, ys, zmin, zmax


def sky_trace(z, xs, ys, cscale, zmin, zmax):
    return go.Heatmap(z=z, x=xs, y=ys, colorscale=cscale, zmin=zmin, zmax=zmax, showscale=False,
                      zsmooth=False, hoverinfo="skip")


def rgb_trace(rgb, xs, ys):
    return go.Image(z=(np.clip(rgb, 0, 1) * 255).astype("uint8"), x0=float(xs[0]), dx=float(xs[1] - xs[0]),
                    y0=float(ys[0]), dy=float(ys[1] - ys[0]), hoverinfo="skip")


def animated(R, rgbs, xs, ys, labels, mjds, overlay=True, height=660):
    """Smooth flipbook: every visit holds, then crossfades (eased) into the next.
    Frames are compressed PNGs so dozens of in-between frames stay light."""
    tgt = R["target"]
    x0, dx = float(xs[0]), float(xs[1] - xs[0])
    y0, dy = float(ys[0]), float(ys[1] - ys[0])
    ny, nx = rgbs[0].shape[:2]
    timeline = imaging.tween(list(rgbs), list(mjds)) if len(rgbs) > 1 else [(rgbs[0], mjds[0], 0)]

    def traces(rgb, mjd):
        tr = [go.Image(source=imaging.png_uri(rgb), x0=x0, dx=dx, y0=y0 + dy * (ny - 1), dy=-dy,
                       hoverinfo="skip")]
        if overlay:
            yr = mjd_to_year(mjd)
            tx, ty = target_xy(R, yr)
            gx, gy, gt = gaia_xy(R, yr)
            tr.append(go.Scatter(x=gx, y=gy, mode="markers", text=gt, hoverinfo="text",
                                 marker=dict(symbol="circle-open", size=16, color="#7ED6A0", line=dict(width=1.5)),
                                 name="Gaia high-PM (predicted)"))
            tr.append(go.Scatter(x=[tx], y=[ty], mode="markers", hoverinfo="text",
                                 text=[f"{tgt.name} predicted position"],
                                 marker=dict(symbol="circle-open", size=26, color=style.ACCENT, line=dict(width=2)),
                                 name="Target (predicted)"))
        return tr

    frames, first_of = [], {}
    for n, (rgb, mjd, key) in enumerate(timeline):
        frames.append(go.Frame(data=traces(rgb, mjd), name=f"f{n}"))
        if key is not None and key not in first_of:
            first_of[key] = n
    fig = go.Figure(data=frames[0].data, frames=frames)
    steps = [dict(method="animate", label=labels[k],
                  args=[[f"f{first_of[k]}"], dict(mode="immediate", frame=dict(duration=0, redraw=True),
                                                  transition=dict(duration=0))]) for k in sorted(first_of)]
    fig.update_layout(
        updatemenus=[dict(type="buttons", direction="left", x=0, y=-0.02, xanchor="left", yanchor="top",
                          bgcolor="#F4F3F1", bordercolor="#F4F3F1", font=dict(color="#0D1215", size=13),
                          showactive=False, pad=dict(r=8, t=4, b=4, l=8),
                          buttons=[dict(label="▶ Play", method="animate",
                                        args=[None, dict(frame=dict(duration=70, redraw=True), fromcurrent=True,
                                                         mode="immediate", transition=dict(duration=0))]),
                                   dict(label="Pause", method="animate",
                                        args=[[None], dict(frame=dict(duration=0), mode="immediate")])])],
        sliders=[dict(active=0, steps=steps, x=0.2, len=0.8, y=-0.02, pad=dict(t=0),
                      currentvalue=dict(prefix="", font=dict(color="#F4F3F1", size=15)),
                      bgcolor="#3A3F42", activebgcolor="#F4F3F1", bordercolor="#3A3F42",
                      ticklen=0, minorticklen=0, font=dict(color="rgba(0,0,0,0)", size=1))],
    )
    fig = style.layout(fig, height=height, square=True)
    fig.update_xaxes(range=[x0 - dx / 2, x0 + dx * (nx - 0.5)])
    fig.update_yaxes(range=[y0 - dy / 2, y0 + dy * (ny - 0.5)], autorange=False)
    return fig


def play(R, rgbs, labels, mjds, overlay, key):
    """Smooth canvas flipbook with flow-warped in-betweens and motion-interpolated markers."""
    ny, nx = R["shape"]
    real, tween = derived(("player", key, CFG["flow"], CFG["native"]),
                           lambda: player.build_frames(list(rgbs), n_between=10 if len(rgbs) <= 8 else 3,
                                                       flow=CFG["flow"]))
    frac = lambda x, y: [(float(x) + 0.5) / nx, (float(y) + 0.5) / ny]
    markers = {}
    if overlay:
        markers["target"] = [frac(*target_xy(R, mjd_to_year(m))) for m in mjds]
        per = [gaia_xy(R, mjd_to_year(m)) for m in mjds]
        if per and per[0][0]:
            markers["others"] = [[frac(per[v][0][k], per[v][1][k]) for v in range(len(mjds))]
                                 for k in range(len(per[0][0]))]
    note = ("In-betweens on: frames between the real visits (dots on the timeline) are generated, "
            + ("motion-compensated with classic optical flow, no machine learning." if CFG["flow"]
               else "as plain crossfades.") + " They are for viewing only; all measurements use real frames.")
    components.html(player.html(real, tween, labels, markers, height=700, note=note), height=720)


GRAY_STOPS = [(0, "#000000"), (1, "#ffffff")]
DIFF_STOPS = [(0, "#4aa3ff"), (0.5, "#000000"), (1, "#e8a33d")]


# ── Pages ────────────────────────────────────────────────────────────────────
def home():
    navbar(on_hero=True)
    style.hero("Watching the sky,<br>catching what drifts",
               "A public explorer for NASA’s SPHEREx infrared survey. Every visit to any patch of sky, "
               "aligned, wavelength-matched and played back to reveal moving and changing worlds.")
    with st.container(key="herocta"):
        st.page_link(PAGES["flip"], label="Start exploring  ↗")

    style.section("Targets", "Start with the official five",
                  "Each one loads live SPHEREx data and opens the flipbook.")
    blurbs = {"WISE 0855": "The coldest known brown dwarf. Moves 8″ a year.",
              "Barnard's Star": "The fastest-moving star in our sky.",
              "ε Indi B": "A pair of brown dwarfs orbiting a nearby star.",
              "RW Aurigae": "A young star that dims and brightens.",
              "R Draconis": "A pulsating red giant, a classic variable."}
    cols = st.columns(5)
    for col, (name, txt) in zip(cols, blurbs.items()):
        with col:
            with st.container(key=f"tcard_{name.replace(' ', '_').replace(chr(39), '')}"):
                st.markdown(f'<div class="tname">{name}</div><div class="tdesc">{txt}</div>', unsafe_allow_html=True)
                if st.button("Open ↗", key=f"open_{name}", width="stretch"):
                    choose(name)
                    st.switch_page(PAGES["flip"])

    st.write("")
    style.section("Method", "Built for the way SPHEREx sees",
                  "SPHEREx has no spectrograph: the colour it records depends on where a star lands on the "
                  "detector. A plain video flickers. Draconid Drift compares like with like.")
    a, b, c = st.columns(3)
    with a:
        style.card("01", "Flipbook", "Every visit to a patch of sky, in true infrared colour, "
                   "wavelength-matched so only real changes move.")
    with b:
        style.card("02", "Motion", "Sub-pixel astrometry measures proper motion from SPHEREx alone, "
                   "within a few percent of the catalog for Barnard’s Star and ε Indi B.")
    with c:
        style.card("03", "Spectra", "Forced photometry across 102 colours turns any point into a "
                   "0.75–5 µm spectrum, with ice and methane features marked.")
    style.footer(NAME_A, NAME_B, FOOT + " Hero image: SPHEREx view of the Orion Nebula, rendered by this tool.")


def flip():
    R = analysis_page("Flipbook", "Every visit, played back",
                      "SPHEREx revisits each patch of sky about every six months. Watch what moves.")
    if R is None:
        return style.footer(NAME_A, NAME_B, FOOT)
    frames = R["frames"]
    m1, m2, m3 = st.columns([2.2, 1, 0.8])
    mode = m1.radio("View", ["Colour", "Single band", "Difference", "Raw exposures"], horizontal=True,
                    help="Colour: short/mid/long wavelengths as blue/green/red. Difference: each visit minus the "
                         "others. Raw: every exposure, which flickers because each lands at a different wavelength.")
    present = sorted({f["band"] for f in frames})
    band = m2.selectbox("Band", present, index=min(2, len(present) - 1),
                        format_func=lambda b: f"{b} · {BANDS[b][0]:.2f}–{BANDS[b][1]:.2f} µm",
                        disabled=mode != "Single band")
    overlay = m3.toggle("Overlays", value=True)
    name = R["target"].name.replace(" ", "_")

    if mode == "Colour":
        rgb, labels, mjds = derived("rgb", lambda: imaging.color_cube(frames))
        if rgb is None:
            st.info("Not enough bands for a colour view here. Try Single band.")
        else:
            if CFG["native"]:
                up, xs, ys = rgb, np.arange(rgb.shape[2]), np.arange(rgb.shape[1])
            else:
                up, xs, ys = derived("rgb_up", lambda: imaging.upsample_rgb(rgb, 4))
            labels = [f"{l} · {mjd_to_date(m)}" for l, m in zip(labels, mjds)]
            play(R, up, labels, mjds, overlay, "rgb")
            st.download_button("Download GIF", imaging.to_gif_rgb(rgb), file_name=f"{name}_colour.gif", mime="image/gif")
    elif mode == "Difference":
        diff, _, bm = derived("diff", lambda: imaging.band_matched_difference(frames))
        if diff is None:
            st.info("Difference imaging needs at least two visits; this position has one so far.")
        else:
            lo, hi = imaging.sym_limits(diff, 99.5)
            cube, xs, ys, a, b = sky(diff, DIFF, lo, hi)
            rgbs = imaging.colorize((cube - a) / (b - a), DIFF_STOPS)
            labels = [f"Visit {i + 1} − others · {mjd_to_date(m)}" for i, m in enumerate(bm)]
            play(R, rgbs, labels, bm, overlay, "diff")
            st.markdown('<p class="note"><b>Each visit minus all the others,</b> band by band. Static stars vanish; '
                        'anything that moved or changed lights up <b style="color:#b8741a">amber</b> (new) and '
                        '<b style="color:#2F6FDE">blue</b> (gone).</p>', unsafe_allow_html=True)
    else:
        key = "matched" if mode == "Single band" else "raw"
        cube, labels, mjds = imaging.build_cube(frames, key, band)
        if cube is None:
            st.info("No frames for this band. Try another band.")
        else:
            if key != "raw":
                labels = [f"{l.replace('Epoch', 'Visit')} · {mjd_to_date(m)}" for l, m in zip(labels, mjds)]
            zmin, zmax = -2.0, max(8.0, imaging.limits(cube, 1, 99.0)[1] * 0.6)
            sc, xs, ys, a, b = sky(cube, GRAY, zmin, zmax)
            play(R, imaging.colorize(sc, GRAY_STOPS), labels, mjds, overlay, f"{key}-{band}")
            if key == "raw":
                st.markdown('<p class="note"><b>Why it flickers.</b> SPHEREx uses linear variable filters: '
                            'wavelength depends on where a star lands on the detector, so every raw exposure is a '
                            'different colour. Colour and Single band views fix this.</p>', unsafe_allow_html=True)
            st.download_button("Download GIF", imaging.to_gif(cube, zmin, zmax), file_name=f"{name}_{key}.gif",
                               mime="image/gif")
    style.footer(NAME_A, NAME_B, FOOT)


def motion():
    R = analysis_page("Motion", "Proper motion, measured by SPHEREx",
                      "Sub-pixel centroids of the target in every visit, fitted with a straight line "
                      "and checked against the catalog.")
    if R is None:
        return style.footer(NAME_A, NAME_B, FOOT)
    tgt, w = R["target"], R["w"]
    deep, _, dmj = derived("deep", lambda: imaging.build_cube(R["frames"], "deep"))
    if deep is None or len(deep) < 2:
        st.info("Motion needs at least two visits.")
        return style.footer(NAME_A, NAME_B, FOOT)
    guesses = [target_xy(R, mjd_to_year(m)) for m in dmj]
    res = imaging.measure_motion(deep, dmj, w, guesses)
    if res is None:
        st.info("The target is too faint here for a centroid in enough visits.")
        return style.footer(NAME_A, NAME_B, FOOT)
    cat = tgt.total_pm
    agree = f"{100 * (1 - abs(res['total'] - cat) / cat):.0f}%" if cat > 0 else "—"
    style.stats([(f"{res['total'] / 1000:.2f}″/yr", "measured by SPHEREx"),
                 (f"{cat / 1000:.2f}″/yr" if cat else "—", "catalog value"),
                 (agree, "agreement with catalog"),
                 (f"±{res['err'] / 1000:.2f}″/yr", f"1σ · {res['n']} visits · {res['baseline_yr']:.1f} yr")],
                highlight=0)
    st.write("")
    rgb, _, _ = derived("rgb", lambda: imaging.color_cube(R["frames"]))
    if rgb is not None:
        up, xs, ys = derived("rgb_up", lambda: imaging.upsample_rgb(rgb, 4))
        fig = go.Figure(rgb_trace(np.mean(up, axis=0), xs, ys))
    else:
        med = np.nanmedian(deep, axis=0)
        mz, xs, ys, a0, a1 = sky(med, GRAY, -2, max(8, imaging.limits(med, 1, 99.0)[1] * .6))
        fig = go.Figure(sky_trace(mz[0], xs, ys, GRAY, a0, a1))
    fig.add_trace(go.Scatter(x=res["xs"], y=res["ys"], mode="lines+markers", name="Measured",
                             line=dict(color=style.ACCENT, width=3), marker=dict(size=10)))
    gx, gy = zip(*guesses)
    fig.add_trace(go.Scatter(x=gx, y=gy, mode="lines+markers", name="Catalog prediction",
                             line=dict(color="#9CC7FF", width=2, dash="dot"), marker=dict(symbol="circle-open", size=12)))
    c0x, c0y = res["xs"][0], res["ys"][0]
    fig = style.layout(fig, 560, square=True)
    fig.update_xaxes(range=[c0x - 12, c0x + 12])
    fig.update_yaxes(range=[c0y - 12, c0y + 12], autorange=False)
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    st.markdown(f'<p class="note">Measured μα* = <b>{res["pmra"]:,.0f}</b>, μδ = <b>{res["pmdec"]:,.0f}</b> mas/yr. '
                'Nearby stars also show parallax (a yearly wobble), which a ~1-year, few-visit baseline cannot fully '
                'separate. Precision improves with every new SPHEREx survey.</p>', unsafe_allow_html=True)
    style.footer(NAME_A, NAME_B, FOOT)


def spectrum():
    R = analysis_page("Spectrum", "102 colours, one point of light",
                      "Motion-corrected forced photometry across every exposure, plus brightness over time.")
    if R is None:
        return style.footer(NAME_A, NAME_B, FOOT)

    def build():
        rows = []
        for f in R["frames"]:
            flux, err = imaging.aperture_phot(f, f["pos"])
            rows.append(dict(band=f["band"], wave=f["wave"], flux=flux, err=err, mjd=f["mjd"],
                             epoch=f["epoch"], flagged=f["flag_frac"]))
        return pd.DataFrame(rows).dropna(subset=["wave", "flux"])

    phot = derived("phot", build)
    if phot.empty:
        st.info("No photometry available at the target position.")
        return style.footer(NAME_A, NAME_B, FOOT)
    fig = go.Figure()
    for b, g in phot.groupby("band"):
        fig.add_trace(go.Scatter(x=g["wave"], y=g["flux"], mode="markers", name=b,
                                 error_y=dict(type="data", array=g["err"], visible=True, thickness=1),
                                 marker=dict(size=10, color=BAND_COLORS.get(b), line=dict(width=0)),
                                 hovertemplate="%{x:.3f} µm<br>%{y:.3g}<extra>" + b + "</extra>"))
    for nm, lam in [("H₂O ice", 3.0), ("CH₄", 3.3), ("CO₂", 4.27), ("CO", 4.67)]:
        fig.add_vline(x=lam, line=dict(color="#D9D6D0", dash="dot"))
        fig.add_annotation(x=lam, y=1.03, yref="paper", text=nm, showarrow=False, font=dict(size=11, color="#5F6368"))
    fig.update_xaxes(title="Wavelength (µm)")
    fig.update_yaxes(title="Aperture flux (MJy/sr · pix)")
    st.plotly_chart(style.layout(fig, 480), width="stretch", config={"displayModeBar": False})
    st.markdown('<p class="note">The aperture follows the target’s catalog proper motion to each exposure’s date. '
                'Brown dwarfs show deep <b>methane absorption near 3.3 µm</b> and a peak near 4 µm.</p>',
                unsafe_allow_html=True)

    st.write("")
    style.section("Light curve", "Brightness over time")
    norm = phot.copy()
    norm["rel"] = norm.groupby("band")["flux"].transform(lambda s: s / np.nanmedian(s))
    norm["date"] = pd.to_datetime(norm["mjd"] + 2400000.5 - 2440587.5, unit="D")
    fig = go.Figure()
    for b, g in norm.groupby("band"):
        fig.add_trace(go.Scatter(x=g["date"], y=g["rel"], mode="markers", name=b, marker=dict(size=10, color=BAND_COLORS.get(b))))
    fig.add_hline(y=1, line=dict(color="#D9D6D0", dash="dot"))
    fig.update_yaxes(title="Flux / band median")
    st.plotly_chart(style.layout(fig, 420), width="stretch", config={"displayModeBar": False})
    spread = norm.groupby("band")["rel"].std().mean()
    verdict = "Stable" if spread < 0.1 else "Variable"
    st.markdown(f'<p class="note"><b>{verdict}.</b> Mean scatter within bands: {spread * 100:.1f}%. Normalising per band '
                'removes the source’s colour so only changes in time remain (try RW Aurigae and R Draconis).</p>',
                unsafe_allow_html=True)
    style.footer(NAME_A, NAME_B, FOOT)


def discoveries():
    R = analysis_page("Discoveries", "What moved that nobody expected?",
                      "Difference imaging, track linking and catalog cross-matching flag candidates to inspect.")
    if R is None:
        return style.footer(NAME_A, NAME_B, FOOT)
    w = R["w"]
    diff, template, dm = derived("diff", lambda: imaging.band_matched_difference(R["frames"]))
    if diff is None:
        st.info("Discovery needs at least two visits.")
        return style.footer(NAME_A, NAME_B, FOOT)
    k = CFG["k_sigma"]
    det = detect.detect_all(diff, dm, w, k, template)
    tracks = detect.link_tracks(det)
    g = R["gaia"]
    g = catalogs.propagate(g, mjd_to_year(float(np.mean(dm)))) if g is not None and len(g) else g
    ast = None
    if CFG["skybot"]:
        with st.spinner("Querying IMCCE SkyBoT for known solar-system objects…"):
            ast = pd.concat([catalogs.skybot(R["center"], R["size"], m) for m in dm], ignore_index=True)
    tracks = detect.classify(tracks, g, ast)
    cube, _, _ = derived("deep", lambda: imaging.build_cube(R["frames"], "deep"))
    var = detect.variables(cube, w)
    n_new = int((tracks["classification"] == "New candidate").sum()) if len(tracks) else 0
    style.stats([(f"{len(det)}", f">{k:g}σ detections"), (f"{len(tracks)}", "linked movers"),
                 (f"{n_new}", "candidates to inspect"), (f"{len(var)}", "variability candidates")], highlight=2)
    st.write("")
    dz, dxs, dys, d0, d1 = sky(np.nanmax(np.abs(diff), axis=0), DIFF, 0, imaging.sym_limits(diff, 99.5)[1])
    fig = go.Figure(sky_trace(dz[0], dxs, dys, [[0, "#000"], [1, "#FFFFFF"]], d0, d1))
    for _, t in tracks.iterrows():
        color = "#E8A33D" if t["classification"] == "New candidate" else "#7ED6A0"
        fig.add_trace(go.Scatter(x=t["xs"], y=t["ys"], mode="lines+markers", name=f"Track {t['track']}",
                                 line=dict(color=color, width=2), marker=dict(size=7),
                                 hovertext=t["classification"], hoverinfo="text"))
    if len(var):
        fig.add_trace(go.Scatter(x=var["x"], y=var["y"], mode="markers", name="Variable",
                                 marker=dict(symbol="diamond-open", size=14, color="#9CC7FF")))
    st.plotly_chart(style.layout(fig, 600, square=True), width="stretch", config={"displayModeBar": False})
    if len(tracks):
        show = tracks.drop(columns=["xs", "ys"]).round(3)
        st.dataframe(show, width="stretch", hide_index=True)
        st.download_button("Export candidates (CSV)", show.to_csv(index=False),
                           file_name=f"{R['target'].name.replace(' ', '_')}_candidates.csv")
    st.markdown('<p class="note"><b>Green</b> = explained by Gaia proper motion or a known asteroid. '
                '<b>Amber</b> = in neither catalog; inspect it in the Flipbook, then report promising ones to '
                'Backyard Worlds or the Minor Planet Center.</p>', unsafe_allow_html=True)
    style.footer(NAME_A, NAME_B, FOOT)


def skydata():
    R = analysis_page("Sky & Data", "Context, metadata and sharing",
                      "Compare against other surveys, inspect every frame, and share a link to this view.")
    if R is None:
        return style.footer(NAME_A, NAME_B, FOOT)
    survey = st.selectbox("Background survey", ["CDS/P/allWISE/color", "CDS/P/2MASS/color",
                                                "CDS/P/DSS2/color", "CDS/P/PanSTARRS/DR1/color-z-zg-g"])
    c = R["center"]
    components.html(f"""
    <div id="al" style="width:100%;height:560px;border-radius:22px;overflow:hidden"></div>
    <script>
    function start() {{
    if (typeof A === "undefined") {{ document.getElementById('al').innerHTML =
      '<p style="font-family:sans-serif;color:#5f6368;padding:24px">Sky viewer unavailable right now (CDS Aladin did not load).</p>'; return; }}
    A.init.then(() => {{
      const a = A.aladin('#al', {{survey: '{survey}', fov: {R['fov'] / 60 * 1.6:.4f},
        target: '{c.ra.deg:.6f} {c.dec.deg:.6f}', showReticle: true, showFullscreenControl: true,
        showCooGridControl: true, cooFrame: 'ICRS'}});
      a.addCatalog(A.catalogFromVizieR('I/355/gaiadr3', '{c.ra.deg:.6f} {c.dec.deg:.6f}',
        {R['fov'] / 60 * 0.8:.4f}, {{onClick: 'showTable', color: '#7ED6A0', sourceSize: 10}}));
      a.addCatalog(A.catalogFromSimbad('{c.ra.deg:.6f} {c.dec.deg:.6f}', {R['fov'] / 60 * 0.8:.4f},
        {{onClick: 'showPopup', color: '#E8A33D'}}));
    }});
    }}
    </script>
    <script src="https://aladin.cds.unistra.fr/AladinLite/api/v3/latest/aladin.js" onload="start()" onerror="start()"></script>""", height=575)
    st.markdown('<p class="note"><b>Green:</b> Gaia DR3 · <b>Amber:</b> SIMBAD objects.</p>', unsafe_allow_html=True)

    st.write("")
    style.section("Frames", "Every exposure used")
    meta = pd.DataFrame([{k: f[k] for k in ("obs_id", "band", "wave", "mjd", "epoch", "flag_frac")} for f in R["frames"]])
    meta["date"] = meta["mjd"].map(mjd_to_date)
    meta = meta.rename(columns={"wave": "λ at target (µm)", "flag_frac": "flagged pixels", "epoch": "visit"})
    st.dataframe(meta.round(4), width="stretch", hide_index=True,
                 column_config={"flagged pixels": st.column_config.ProgressColumn(
                     "flagged pixels", min_value=0.0, max_value=1.0, format="%.2f")})
    st.download_button("Export frame metadata (CSV)", meta.to_csv(index=False), file_name="frames.csv")
    q = urllib.parse.urlencode({"target": ss.target, "fov": f"{R['fov']:g}"})
    st.markdown('<p class="note"><b>Share or embed this view</b> by adding this to the app URL:</p>', unsafe_allow_html=True)
    st.code(f"/flipbook?{q}", language="text")
    st.markdown(f'<p class="note">{len(R["all_frames"])} images available, {len(R["frames"])} analysed. New weekly '
                'releases appear automatically because every query hits IRSA live.</p>', unsafe_allow_html=True)
    style.footer(NAME_A, NAME_B, FOOT)


st.navigation(list(PAGES.values()), position="hidden").run()
