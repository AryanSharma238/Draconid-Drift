"""Fill web/verdict.html from a verdict dict."""
import os

_T = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "verdict.html")


def _cls(ok):
    return "ok" if ok else "no"


def classify(v):
    """Plain label: what is at this position? Returns (label, colour)."""
    c = v["checks"]
    if c["strength"]["nu"] < -3.0:
        return "UNRELIABLE: negative signal here, the target is probably too bright for SPHEREx", "#b91c1c"
    src = max(c["strength"]["nu"], c["motion"].get("nu_static", 0.0))
    all_ok = c["halves"]["ok"] and c["leave_one_out"]["ok"] and c["flags"]["ok"]
    if src < 6:
        return "NOISE: no source stands out here", "#6b7280"
    if not v["moving"]:
        return "STAR: a bright source that does not move", "#1d4ed8"
    if v["confidence"] >= 0.99 and all_ok:
        return "MOVER: a moving object", "#0a7d3b"
    if v["confidence"] >= 0.95:
        why = "a check fails" if not all_ok else "confidence is below 99%"
        return f"MOVER (unconfirmed): moves, but {why}", "#b36b00"
    return "SOURCE FOUND, motion unclear", "#b36b00"


def render(v):
    c = v["checks"]
    conf = v["confidence"]
    if v["confidence_is_bound"]:
        conf_txt = f"> {100 * v['confidence_floor']:.1f}%"
    else:
        conf_txt = f"{100 * conf:.1f}%"
    color = "#0a7d3b" if (v["moving"] and conf >= 0.99) else ("#b36b00" if conf >= 0.95 else "#6b7280")
    h = c["halves"]
    kind, kind_color = classify(v)
    if kind.startswith("UNRELIABLE"):
        conf_txt, color = "n/a", "#b91c1c"
        v = dict(v, headline="Saturated or over-subtracted data: the motion test does not apply. "
                              "Try a fainter target or a different detector array.")
    html = open(_T, encoding="utf-8").read()
    rep = {
        "__CONF__": conf_txt, "__COLOR__": color,
        "__NU__": f"{c['strength']['nu']:.1f}",
        "__DCHI__": f"{c['motion']['dchi2']:.1f}", "__MCLS__": _cls(c["motion"]["dchi2"] > 0),
        "__HALVES__": f"{h['nu1']:.1f} / {h['nu2']:.1f} {'✓' if h['ok'] else '✗'}", "__HCLS__": _cls(h["ok"]),
        "__LOO__": f"{c['leave_one_out']['nu_min']:.1f} {'✓' if c['leave_one_out']['ok'] else '✗'}",
        "__LCLS__": _cls(c["leave_one_out"]["ok"]),
        "__FLAGS__": f"{100 * c['flags']['frac']:.0f}% flagged {'✓' if c['flags']['ok'] else '✗'}",
        "__FCLS__": _cls(c["flags"]["ok"]),
        "__NULLTXT__": (f"False-alarm probability {100 * v['fap']:.1f}%"
                        f"{' (floor of the ' + str(v['n_null']) + ' scrambles run)' if v['confidence_is_bound'] else ''}"
                        f", from {v['n_null']} scrambled-time searches over the same region and motions."),
        "__HEADLINE__": v["headline"], "__KIND__": kind, "__KCOLOR__": kind_color,
        "__SUB__": (f"{v['n_frames']} frames over {v['baseline_days']:.0f} days · "
                    f"{'blind scan of the whole patch' if v['mode'] == 'blind' else 'check at the target position'}"),
    }
    for k, val in rep.items():
        html = html.replace(k, str(val))
    return html
