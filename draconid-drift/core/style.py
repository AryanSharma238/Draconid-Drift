"""Draconid Drift visual system.
Full-bleed photo hero with a dark overlay, a floating dark pill nav (links left, wordmark
centred, outlined CTA right), a huge light-weight white headline, and a dark pill CTA with ↗.
Type: DM Sans. Palette: near-black ink, white, warm neutral greys. No gradients-as-decoration."""
import base64
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

INK = "#0D1215"
MUTED = "#5F6368"
LINE = "#E6E4E0"
TILE = "#F4F3F1"
ACCENT = "#E8A33D"   # warm sunset amber, used sparingly for data highlights

_HERO = Path(__file__).resolve().parent.parent / "assets" / "hero.jpg"


def _hero_b64() -> str:
    try:
        return base64.b64encode(_HERO.read_bytes()).decode()
    except Exception:
        return ""


CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,300;9..40,400;9..40,500;9..40,600&display=swap');
:root { --ink:#0d1215; --muted:#5f6368; --line:#e6e4e0; --tile:#f4f3f1; --amber:#e8a33d; }
.stApp, .stApp *:not([data-testid*="Icon"]):not([class*="Icon"]):not([class*="material"]):not([data-testid="stIconMaterial"]) {
  font-family: "DM Sans", -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif !important;
  -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }
.stApp { background:#ffffff; color: var(--ink); }
#MainMenu, footer, header[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stDecoration"] {display:none !important;}
.block-container { max-width: 1160px; padding-top: 18px !important; padding-bottom: 6rem; }

/* ── Hero ── */
.hero { position: relative; border-radius: 22px; overflow: hidden; min-height: 640px;
  background-size: cover; background-position: center; margin-bottom: 40px; }
.hero::before { content:""; position:absolute; inset:0;
  background: linear-gradient(180deg, rgba(8,10,12,.35) 0%, rgba(8,10,12,.15) 40%, rgba(8,10,12,.70) 100%); }
.hero > * { position: relative; }
.navpill { margin: 18px 18px 0; height: 72px; border-radius: 999px; background: #0d1215;
  display:grid; grid-template-columns: 1fr auto 1fr; align-items:center; padding: 0 12px 0 34px; }
.navpill .l, .navpill .r { display:flex; gap: 30px; align-items:center; }
.navpill .r { justify-content:flex-end; }
.navpill a { color:#fff !important; text-decoration:none; font-size: 15px; font-weight: 400; opacity:.92; }
.navpill a:hover { opacity: 1; }
.navpill .cta { border: 1.5px solid #fff; border-radius: 999px; padding: 13px 22px; opacity:1; }
.wordmark { color:#fff; font-size: 21px; letter-spacing: .02em; display:flex; align-items:center; gap:2px; }
.wordmark b { font-weight: 600; }
.wordmark span { font-weight: 300; color: #e8a33d; }
.wordmark svg { margin-left: 6px; }
.hero-body { position:absolute; left: 44px; right: 44px; bottom: 44px; display:flex;
  justify-content: space-between; align-items: flex-end; gap: 32px; }
.hero-body { right: 300px !important; }
.hero h1 { color:#fff; font-size: clamp(48px, 7.2vw, 96px); font-weight: 400; line-height: 1.02;
  letter-spacing: -0.035em; margin: 0 0 22px; max-width: 900px; }
.hero p { color: rgba(255,255,255,.80); font-size: 17px; font-weight: 400; line-height: 1.5;
  margin: 0; max-width: 560px; }
.hero .go { background:#0d1215; color:#fff !important; text-decoration:none; white-space:nowrap;
  border-radius: 999px; padding: 18px 28px; font-size: 16px; font-weight: 500; }
.hero .go:hover { background:#000; }

/* ── Navbar (real Streamlit page links, styled as the dark pill) ── */
.st-key-navbar, .st-key-navbar_hero { background:#0d1215; border-radius:999px; padding: 10px 12px 10px 30px;
  min-height: 72px; justify-content:center; }
.st-key-navbar { margin: 4px 0 40px; position: sticky; top: 8px; z-index: 100; box-shadow: 0 8px 30px rgba(13,18,21,.12); }
.st-key-navbar_hero { position: relative; z-index: 20; margin: 18px 18px -90px; }
.st-key-navbar [data-testid="stPageLink"] a, .st-key-navbar_hero [data-testid="stPageLink"] a {
  background: transparent !important; padding: 6px 4px !important; }
.st-key-navbar [data-testid="stPageLink"] p, .st-key-navbar_hero [data-testid="stPageLink"] p {
  color: rgba(255,255,255,.88) !important; font-size: 15px !important; white-space: nowrap; }
.st-key-navbar [data-testid="stPageLink"] a:hover p, .st-key-navbar_hero [data-testid="stPageLink"] a:hover p { color:#fff !important; }
.st-key-navbar [data-testid="stPageLink"] a[aria-current="page"] p { color: #e8a33d !important; }
.st-key-navbar [data-testid="stColumn"]:nth-child(4), .st-key-navbar_hero [data-testid="stColumn"]:nth-child(4) { position: relative; }
.st-key-navbar [data-testid="stColumn"]:nth-child(4) [data-testid="stPageLink"],
.st-key-navbar_hero [data-testid="stColumn"]:nth-child(4) [data-testid="stPageLink"] {
  position:absolute; inset:-14px 0; opacity:0; }
.st-key-navbar .wordmark, .st-key-navbar_hero .wordmark { justify-content:center; }
.st-key-navcta [data-testid="stPageLink"] a { border: 1.5px solid #fff !important; border-radius: 999px !important;
  padding: 10px 16px !important; justify-content:center; }
.st-key-navcta [data-testid="stPageLink"] p { font-size: 14px !important; }
.st-key-navcta [data-testid="stPageLink"] p { color:#fff !important; }
.hero { padding-top: 0; }
.st-key-herocta { position: relative; z-index: 20; margin: -132px 44px 92px auto; width: fit-content; }
.st-key-herocta [data-testid="stPageLink"] a { background:#0d1215 !important; border-radius:999px !important;
  padding: 16px 26px !important; }
.st-key-herocta [data-testid="stPageLink"] p { color:#fff !important; font-size: 16px !important; font-weight: 500 !important; }
.st-key-herocta [data-testid="stPageLink"] a:hover { background:#000 !important; }

/* ── Target cards on Home ── */
[class*="st-key-tcard_"] { background: var(--tile); border-radius: 22px; padding: 26px 22px 20px; min-height: 230px;
  justify-content: space-between; }
.tname { font-size: 22px; letter-spacing: -0.02em; color: var(--ink); margin-bottom: 8px; }
.tdesc { font-size: 15px; line-height: 1.5; color: var(--muted); }
[class*="st-key-tcard_"] .stButton > button { background: var(--ink) !important; color:#fff !important; border:none !important; }

/* ── Search ── */
.kicker { font-size: 13px; font-weight: 500; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin: 0 0 10px; }
.h2 { font-size: clamp(32px, 4vw, 48px); font-weight: 400; letter-spacing: -0.03em; line-height:1.08; margin: 0 0 8px; color: var(--ink); }
.lead { font-size: 18px; color: var(--muted); line-height: 1.5; margin: 0 0 26px; max-width: 680px; }
.stTextInput input { background: #fff !important; border: 1.5px solid #d9d6d0 !important; border-radius: 999px !important;
  height: 58px; font-size: 17px !important; color: var(--ink) !important; padding-left: 26px !important; }
.stTextInput input:focus { border-color: var(--ink) !important; box-shadow: 0 0 0 4px rgba(13,18,21,.08) !important; }

.stTextInput div[data-baseweb="input"], .stTextInput div[data-baseweb="base-input"] {
  border: none !important; background: transparent !important; border-radius: 999px !important; }
[data-testid="stIconMaterial"], .material-symbols-rounded { font-family: "Material Symbols Rounded" !important; }

/* ── Buttons ── */
.stButton > button, .stDownloadButton > button { border-radius: 999px !important; font-weight: 400 !important;
  font-size: 15px !important; padding: 10px 20px !important; background: #fff !important; color: var(--ink) !important;
  border: 1.5px solid #d9d6d0 !important; box-shadow: none !important; transition: all .18s ease; }
.stButton > button:hover { border-color: var(--ink) !important; }
.stButton > button[kind="primary"] { background: var(--ink) !important; color:#fff !important; border: none !important;
  height: 58px; font-size: 16px !important; font-weight: 500 !important; }
.stButton > button[kind="primary"]:hover { background:#000 !important; }
.stDownloadButton > button { border: none !important; padding-left: 0 !important; text-decoration: underline;
  text-underline-offset: 4px; background: transparent !important; }

/* ── Cards & stats ── */
.tile { background: var(--tile); border-radius: 22px; padding: 34px 32px; height: 100%; }
.tile .n { font-size: 13px; color: var(--muted); letter-spacing:.08em; margin-bottom: 26px; }
.tile h3 { margin:0 0 10px; font-size: 26px; font-weight: 400; letter-spacing:-0.02em; color:var(--ink); }
.tile p { margin:0; font-size:16px; line-height:1.55; color: var(--muted); }
.stat { border-top: 1.5px solid var(--ink); padding: 16px 2px 4px; }
.stat .v { font-size: 40px; font-weight: 400; letter-spacing: -0.035em; color: var(--ink); line-height:1.05; }
.stat .l { font-size: 14px; color: var(--muted); margin-top: 6px; }
.stat.hl { border-top-color: var(--amber); }
.stat.hl .v { color: #b8741a; }
.note { font-size: 15px; color: var(--muted); line-height:1.55; }
.note b { color: var(--ink); font-weight:500; }

/* ── Tabs: dark pill switcher ── */
.stTabs [role="tablist"] { gap: 4px; background: var(--tile); padding: 5px; border-radius: 999px;
  width: fit-content; margin: 38px 0 26px; border: none !important; }
.stTabs [data-testid="stTab"] { border-radius: 999px !important; padding: 9px 18px !important; height:auto !important;
  background: transparent !important; border: none !important; }
.stTabs [data-testid="stTab"] { margin: 0 !important; box-shadow:none !important; }
.stTabs [data-testid="stTab"]::after, .stTabs [data-testid="stTab"]::before { display:none !important; }
.stTabs [role="tablist"] > div:not([data-testid="stTab"]) { display:none !important; }
.stTabs [data-testid="stTab"] p { font-size: 14px !important; color: var(--ink) !important; }
.stTabs [data-testid="stTab"][aria-selected="true"] { background: var(--ink) !important; }
.stTabs [data-testid="stTab"][aria-selected="true"] p { color: #fff !important; }
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] { display:none !important; height:0 !important; }
[data-baseweb="tab-list"] { gap: 4px; background: var(--tile); padding: 5px; border-radius: 999px;
  width: fit-content; margin: 38px 0 26px; }
[data-baseweb="tab"] { border-radius: 999px !important; padding: 9px 18px !important; height:auto !important;
  color: var(--ink) !important; background: transparent !important; }
[data-baseweb="tab"] p { font-size: 14px !important; }
[data-baseweb="tab"][aria-selected="true"] { background: var(--ink) !important; }
[data-baseweb="tab"][aria-selected="true"] p { color: #fff !important; }
[data-baseweb="tab-highlight"], [data-baseweb="tab-border"] { display:none !important; }

[data-testid="stExpander"] { border: 1.5px solid var(--line) !important; border-radius: 18px !important; background:#fff; }
[data-testid="stDataFrame"] { border-radius: 14px; overflow: hidden; }
.stPlotlyChart { border-radius: 22px; overflow: hidden; }

.foot { border-top: 1.5px solid var(--line); margin-top: 72px; padding-top: 20px; color: var(--muted);
  font-size: 13px; line-height: 1.6; display:flex; justify-content:space-between; gap: 24px; }
.foot .wm { color: var(--ink); font-size: 16px; }
.foot .wm b { font-weight:600; } .foot .wm span { font-weight:300; color:#b8741a; }
</style>
"""

# Small meteor-streak mark next to the wordmark.
MARK = ('<svg width="34" height="22" viewBox="0 0 34 22" fill="none"><path d="M2 20 L26 4" stroke="#fff" '
        'stroke-width="1.4" stroke-linecap="round" opacity=".55"/><path d="M10 20 L30 7" stroke="#fff" '
        'stroke-width="1.4" stroke-linecap="round"/><circle cx="30" cy="7" r="2.4" fill="#E8A33D"/></svg>')


def inject():
    st.markdown(CSS, unsafe_allow_html=True)


def hero(headline: str, sub: str):
    b64 = _hero_b64()
    bg = f"background-image:url('data:image/jpeg;base64,{b64}')" if b64 else "background:#0d1215"
    st.markdown(f"""
<div class="hero" style="{bg}">
  <div class="hero-body"><div><h1>{headline}</h1><p>{sub}</p></div></div>
</div>""", unsafe_allow_html=True)


def section(kicker: str, title: str, lead: str = "", anchor: str = ""):
    a = f'<div id="{anchor}"></div>' if anchor else ""
    st.markdown(f'{a}<p class="kicker">{kicker}</p><div class="h2">{title}</div>'
                + (f'<p class="lead">{lead}</p>' if lead else ""), unsafe_allow_html=True)


def card(n: str, title: str, body: str):
    st.markdown(f'<div class="tile"><div class="n">{n}</div><h3>{title}</h3><p>{body}</p></div>',
                unsafe_allow_html=True)


def stats(items, highlight: int | None = None):
    cols = st.columns(len(items))
    for i, (c, (v, l)) in enumerate(zip(cols, items)):
        cls = "stat hl" if i == highlight else "stat"
        c.markdown(f'<div class="{cls}"><div class="v">{v}</div><div class="l">{l}</div></div>',
                   unsafe_allow_html=True)


def footer(name_a: str, name_b: str, text: str):
    st.markdown(f'<div class="foot"><div class="wm"><b>{name_a}</b><span>{name_b}</span></div>'
                f'<div>{text}</div></div>', unsafe_allow_html=True)


FONT = dict(family='"DM Sans",-apple-system,Helvetica Neue,sans-serif', color=INK, size=13)


def layout(fig: go.Figure, height=520, square=False) -> go.Figure:
    """Charts: white, hairline grid. Sky images (square=True): black panel."""
    bg = "#0D1215" if square else "#FFFFFF"
    fig.update_layout(
        template="plotly_white", height=height, paper_bgcolor=bg, plot_bgcolor=bg,
        margin=dict(l=18, r=18, t=44, b=18) if square else dict(l=8, r=8, t=36, b=8),
        font=dict(FONT, color="#F4F3F1") if square else FONT,
        hoverlabel=dict(bgcolor="#FFFFFF", bordercolor=LINE, font=dict(FONT, size=13)),
        legend=dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=1.07, x=0,
                    font=dict(color="#F4F3F1" if square else INK)),
    )
    fig.update_xaxes(gridcolor="#EEECE8", zeroline=False, linecolor=LINE)
    fig.update_yaxes(gridcolor="#EEECE8", zeroline=False, linecolor=LINE)
    if square:
        fig.update_xaxes(visible=False, constrain="domain")
        fig.update_yaxes(visible=False, scaleanchor="x", constrain="domain")
    return fig
