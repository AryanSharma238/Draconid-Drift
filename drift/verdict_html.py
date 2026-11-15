"""Fill web/verdict.html from a verdict dict."""
import os

_T = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "verdict.html")


def _cls(ok):
    return "ok" if ok else "no"


def render(v):
    c = v["checks"]
    conf = v["confidence"]
    if v["confidence_is_bound"]:
        conf_txt = f"> {100 * v['confidence_floor']:.1f}%"
    else:
        conf_txt = f"{100 * conf:.1f}%"
    color = "#34d399" if (v["moving"] and conf >= 0.99) else ("#fbbf24" if conf >= 0.90 else "#94a3b8")
    h = c["halves"]
    html = open(_T, encoding="utf-8").read()
    rep = {
        "__CONF__": conf_txt, "__COLOR__": color, "__N_NULL__": str(v["n_null"]),
        "__NU__": f"{c['strength']['nu']:.1f}",
        "__DCHI__": f"{c['motion']['dchi2']:+.1f} {'✓' if c['motion']['dchi2'] > 0 else '✗'}",
        "__MCLS__": _cls(c["motion"]["dchi2"] > 0),
        "__HALVES__": f"{h['nu1']:.1f} / {h['nu2']:.1f} {'✓' if h['ok'] else '✗'}", "__HCLS__": _cls(h["ok"]),
        "__LOO__": f"{c['leave_one_out']['nu_min']:.1f} {'✓' if c['leave_one_out']['ok'] else '✗'}",
        "__LCLS__": _cls(c["leave_one_out"]["ok"]),
        "__FLAGS__": f"{100 * c['flags']['frac']:.0f}% {'✓' if c['flags']['ok'] else '✗'}",
        "__FCLS__": _cls(c["flags"]["ok"]),
        "__NULLTXT__": (f"Empirical false-alarm probability {100 * v['fap']:.1f}%"
                        f"{' (floor of ' + str(v['n_null']) + ' scrambles)' if v['confidence_is_bound'] else ''}"
                        f" across {v['n_null']} time-scrambled searches over the same spatial & velocity grid."),
        "__HEADLINE__": v["headline"],
        "__SUB__": (f"{v['n_frames']} frames over {v['baseline_days']:.0f} days · "
                    f"{'Blind scan of the whole patch' if v['mode'] == 'blind' else 'Targeted proper-motion check'}"),
    }
    for k, val in rep.items():
        html = html.replace(k, str(val))
    return html
