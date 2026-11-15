"""Train drift/classify.py's model on SPHEREx sources labelled by SIMBAD, and validate it on held-out fields.

python scripts/train_classifier.py         (fetches ~30 fields from the SPHEREx S3 mirror, ~10 min the first time)
"""
import csv, io, json, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from drift import pipeline
from drift.classify import CLASSES, FEATURES, MODEL_PATH, features, fit_softmax, label_of, predict, quad
import drift.classify as C
from drift.clean import flatten
from drift.movers import deep_stack

FIELDS = {  # name: (ra, dec) -- galaxy clusters, star fields in the Milky Way, nebulae and clusters
    "virgo": (187.70, 12.39), "coma": (194.95, 27.98), "fornax": (54.62, -35.45), "hercules_cl": (241.31, 17.75),
    "perseus_cl": (49.95, 41.51), "leo_triplet": (169.73, 13.25), "hydra_cl": (159.18, -27.53), "stephan": (339.01, 33.96),
    "cosmos": (150.12, 2.21), "m101": (210.80, 54.35), "abell2199": (247.16, 39.55), "m81": (148.89, 69.07),
    "ngc_253": (11.89, -25.29), "abell3558": (201.99, -31.50), "m51": (202.47, 47.20), "ursa_major_cl": (178.0, 49.0),
    "barnard": (269.45, 4.77), "plane_aql": (285.0, 2.0), "plane_cyg": (305.0, 38.0), "plane_mon": (100.0, 5.0),
    "plane_sco": (255.0, -38.0), "plane_vel": (130.0, -45.0), "pleiades": (56.75, 24.12), "praesepe": (130.1, 19.67),
    "orion": (83.82, -5.39), "rosette": (97.98, 4.95), "lagoon": (270.90, -24.38), "eagle": (274.70, -13.80),
    "taurus": (68.0, 26.0), "m13": (250.42, 36.46), "omega_cen": (201.70, -47.48), "helix": (337.41, -20.84),
    "carina": (161.26, -59.68), "rho_oph": (246.79, -24.54), "m57": (283.40, 33.03), "crab": (83.63, 22.01),
}
SIMBAD = "https://simbad.harvard.edu/simbad/sim-tap/sync"


def simbad(ra, dec, r):
    import httpx
    q = (f"SELECT ra, dec, otype, main_id FROM basic WHERE 1=CONTAINS(POINT('ICRS',ra,dec),CIRCLE('ICRS',{ra},{dec},{r}))")
    t = httpx.get(SIMBAD, params=dict(REQUEST="doQuery", LANG="ADQL", FORMAT="csv", QUERY=q, MAXREC=20000), timeout=60).text
    return list(csv.DictReader(io.StringIO(t)))


def peaks(stack, nsig=8.0):
    s = np.where(np.isfinite(stack), stack, np.nan)
    med = np.nanmedian(s); sig = 1.4826 * np.nanmedian(np.abs(s - med))
    z = np.nan_to_num((s - med) / sig, nan=-99)
    from scipy.ndimage import maximum_filter
    mx = maximum_filter(z, size=5)
    ys, xs = np.nonzero((z == mx) & (z > nsig))
    return list(zip(xs, ys))


def field_data(name, ra, dec):
    out = f"data/train/{name}.npz"
    if os.path.exists(out):
        z = np.load(out); return z["X"], z["y"]
    spec = pipeline.Spec(name, ra, dec, 0, 0, 2000.0, 720, "SPHEREx-D3", "train_" + name)
    cube, _ = pipeline.load_frames(spec)
    if cube.n > 60:
        cube = cube.subset(np.argsort(cube.mjd)[np.linspace(0, cube.n - 1, 60).round().astype(int)])
    cube = flatten(cube)
    stack, _ = deep_stack(cube)
    rows = simbad(*map(float, cube.wcs.wcs.crval), 0.15)
    lab = [(float(r["ra"]), float(r["dec"]), label_of(r["otype"])) for r in rows if r["ra"] and label_of(r["otype"])]
    if not lab:
        np.savez(out, X=np.zeros((0, len(FEATURES))), y=np.zeros(0, int)); return np.zeros((0, len(FEATURES))), np.zeros(0, int)
    lx, ly = cube.wcs.world_to_pixel_values(np.array([l[0] for l in lab]), np.array([l[1] for l in lab]))
    X, y = [], []
    for (px, py) in peaks(stack):
        d = np.hypot(lx - px, ly - py)
        j = int(np.argmin(d))
        if d[j] > 0.8:                     # within 5" of a SIMBAD object, and no other SIMBAD object nearly as close
            continue
        if np.sum(d < 1.5) > 1:
            continue
        fv = features(stack, cube, float(px), float(py))
        if fv is None or not np.isfinite(fv).all():
            continue
        X.append(fv); y.append(CLASSES.index(lab[j][2]))
    X, y = np.array(X).reshape(-1, len(FEATURES)), np.array(y, int)
    np.savez(out, X=X, y=y)
    return X, y


def main():
    data = {}
    for n, (ra, dec) in FIELDS.items():
        t = time.time()
        try:
            X, y = field_data(n, ra, dec)
            data[n] = (X, y)
            print(f"{n:14s} {len(y):4d} sources  star/gal/other = {np.bincount(y, minlength=3).tolist()}  {time.time() - t:.0f}s", flush=True)
        except Exception as e:
            print(n, "FAILED", type(e).__name__, str(e)[:120], flush=True)
    names = [n for n in data if len(data[n][1])]
    # star vs galaxy: logistic model on features + pairwise products. "other" (nebulae, clouds) is a shape rule in predict().
    def pool(ns):
        X = np.vstack([data[n][0] for n in ns]); y = np.concatenate([data[n][1] for n in ns]); return X, y
    def fit(X, y):
        m = y < 2; Q = quad(X[m]); mu, sd = Q.mean(0), Q.std(0) + 1e-9
        W, b = fit_softmax((Q - mu) / sd, y[m], 2, lr=0.1)
        return dict(classes=["star", "galaxy"], features=list(FEATURES), quad=True, mu=mu.tolist(), sd=sd.tolist(), W=W.tolist(), b=b.tolist())
    rng = np.random.default_rng(0); folds = np.array_split(rng.permutation(names), 6)
    conf = np.zeros((3, 3), int)
    for f in folds:          # leave-fields-out: a test field is never in the training set
        mdl = fit(*pool([n for n in names if n not in f]))
        C._MODEL = None; json.dump(mdl, open("/tmp/_fold.json", "w")); C.MODEL_PATH = "/tmp/_fold.json"
        Xe, ye = pool(list(f))
        for x, a in zip(Xe, ye):
            conf[a, CLASSES.index(predict(x)[0])] += 1
    print("held-out confusion (rows = SIMBAD truth, cols = predicted), classes", CLASSES)
    print(conf)
    rec = conf.diagonal() / conf.sum(1).clip(1)
    print("recall", dict(zip(CLASSES, rec.round(3))), "accuracy", round(conf.trace() / conf.sum(), 3))
    X, y = pool(names)
    mdl = fit(X, y); mdl.update(n_train=int(len(y)), counts=np.bincount(y, minlength=3).tolist(), heldout_confusion=conf.tolist())
    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    json.dump(mdl, open(MODEL_PATH, "w"), indent=1)
    print("saved", MODEL_PATH)


if __name__ == "__main__":
    main()
