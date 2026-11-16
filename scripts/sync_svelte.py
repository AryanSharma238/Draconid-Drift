"""Keep the Svelte front end (frontend/) identical to the Streamlit one (web/stage/).

web/stage is the single source: its markup, styles and scripts are copied into frontend/, and app.js becomes
frontend/src/lib/stageEngine.js with the Streamlit message bridge swapped for calls to stage_server.py's API.
Run it after any change to web/stage, then build:  python scripts/sync_svelte.py && (cd frontend && npx vite build)
"""
from __future__ import annotations

import pathlib
import re
import shutil

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB, FE = ROOT / "web" / "stage", ROOT / "frontend"

BRIDGE = r'''
/* ---------------------------------------------------------------- FastAPI Stage API bridge */
let args = { presets: [], mid: 2026.3, result: null, error: null };
const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
async function emit(value) {
  if (value.type === "home") { args.result = null; args.error = null; return; }
  if (value.type === "go") {
    try {
      const resp = await post("/api/stage/go", value), data = await resp.json();
      if (!req || req.nonce !== value.nonce) return;
      if (data.error || !resp.ok) { args.error = data.error || `Server error (${resp.status})`; args.result = null; }
      else { args.error = null; args.result = data; }
    } catch (err) {
      if (!req || req.nonce !== value.nonce) return;
      args.error = "Unable to reach the Draconid Drift server."; args.result = null;
    }
    onArgs(); return;
  }
  if (value.type === "facts") {
    let f = {};
    try { f = (await (await post("/api/stage/facts", { name: value.name, ra: value.ra, dec: value.dec })).json()).facts || {}; } catch (err) {}
    args.facts = Object.assign({}, args.facts || {}, { [value.name]: f }); return;
  }
  const r = last_res || args.result;
  if (!r) return;
  if (value.type === "bands") {
    let extra = { bands: [], spectra: {} };
    try {
      const b = await (await post("/api/stage/bands", { key: value.key || null, name: r.name, ra: r.ra, dec: r.dec, pmra: r.pmra || 0,
        pmdec: r.pmdec || 0, epoch: r.epoch, ra_c: r.ra_c, dec_c: r.dec_c, field_deg: r.field_deg, detector: r.detector || "SPHEREx-D3",
        objects: r.objects || [] })).json();
      extra = { bands: b.bands || [], spectra: b.spectra || {} };
      if (b.lc) extra.lc = b.lc;
    } catch (err) {}
    if (args.result && args.result.nonce === value.for) { args.result = Object.assign({}, args.result, extra); onArgs(); }
    return;
  }
  if (value.type === "depth") {
    let d;
    try { d = (await (await post("/api/stage/depth", { ra_c: r.ra_c, dec_c: r.dec_c, field_deg: r.field_deg })).json()).depth; }
    catch (err) { d = { objects: [], error: "The distance catalogues did not answer. Try again in a moment." }; }
    if (args.result && args.result.nonce === value.for) { args.result = Object.assign({}, args.result, { depth: d }); onArgs(); }
  }
}
'''

BOOT = r'''(async () => {
  try {
    const init = await fetch("/api/stage/init").then(r => r.json());
    if (init.presets) args.presets = init.presets;
    if (init.mid) args.mid = init.mid;
  } catch (e) {}
  await boot();
  const qp = new URLSearchParams(window.location.search);
  if (qp.has("ra") && qp.has("dec")) {
    const ra = parseFloat(qp.get("ra")), dec = parseFloat(qp.get("dec")), fov = parseFloat(qp.get("fov") || "180");
    if (Number.isFinite(ra) && Number.isFinite(dec)) { leaveLanding(); go({ key: "custom", name: "Linked location", ra, dec, pmra: 0, pmdec: 0, epoch: args.mid, fov, detector: "SPHEREx-D3" }); }
  }
})();
'''


def main():
    lib = FE / "src" / "lib"
    for f in ("sky.js", "healpix.js", "intro.js", "depth.js", "const.js"):
        t = (WEB / f).read_text()
        t = t.replace("await import(THREE_URL)", "await import(/* @vite-ignore */ THREE_URL)")
        (lib / f).write_text(t)

    js = (WEB / "app.js").read_text()
    imports = "\n".join(l for l in js.splitlines() if l.startswith("import "))
    body = js[js.index("/* ---------------------------------------------------------------- clock"):]
    body = re.sub(r"\nboot\(\);\s*$", "\n" + BOOT, body.rstrip() + "\n")
    assert BOOT in body, "app.js no longer ends with boot();"
    (lib / "stageEngine.js").write_text(f"{imports}\n\nexport function mountStage() {{\n{BRIDGE}\n{body}}}\n")

    html = (WEB / "index.html").read_text()
    markup = html[html.index("<body>") + 6:html.index('<script type="module" src="app.js"></script>')].strip()
    markup = markup.replace('src="icons/', 'src="/icons/').replace('src="demo.mp4"', 'src="/demo.mp4"')
    svelte = ('<script>\n  import { onMount } from "svelte";\n  import { mountStage } from "./lib/stageEngine.js";\n\n'
              '  onMount(() => {\n    mountStage();\n  });\n</script>\n\n' + markup + "\n")
    # Svelte treats { } in markup as expressions: escape any that the formulas in How it works use
    svelte = svelte.replace("{", "&#123;").replace("}", "&#125;")
    svelte = svelte.replace("onMount(() =&#62;", "onMount(() =>")
    head_script, rest = svelte.split("</script>", 1)
    head_script = head_script.replace("&#123;", "{").replace("&#125;", "}")
    (FE / "src" / "App.svelte").write_text(head_script + "</script>" + rest)

    head = html[html.index("<head>") + 6:html.index("</head>")]
    links = "\n".join("  " + l.strip() for l in head.splitlines()
                      if ("<link" in l or "<script" in l) and "style.css" not in l)
    links = links.replace('href="icons/', 'href="/icons/')
    (FE / "index.html").write_text(
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width,initial-scale=1">\n  <title>Draconid Drift</title>\n'
        f'{links}\n</head>\n<body>\n  <div id="svelte-root"></div>\n  <script type="module" src="/src/main.js"></script>\n</body>\n</html>\n')

    (FE / "src" / "app.css").write_text((WEB / "style.css").read_text())
    pub = FE / "public"
    shutil.copy2(WEB / "demo.mp4", pub / "demo.mp4")
    if (pub / "icons").exists():
        shutil.rmtree(pub / "icons")
    shutil.copytree(WEB / "icons", pub / "icons")
    if (WEB / "catalog.json").exists():
        shutil.copy2(WEB / "catalog.json", pub / "catalog.json")
    print("frontend/ now matches web/stage/")


if __name__ == "__main__":
    main()
