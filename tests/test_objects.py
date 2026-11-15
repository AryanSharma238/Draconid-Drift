import numpy as np

from backend.models import make_grid
from drift import classifier, objects
from drift.clean import Cube


def _cube(stars, n=12, size=0.0667, seed=0):
    """Cube of n frames with Gaussian stars [(x, y, peak)] on noise."""
    g = make_grid(150.0, 20.0, size)
    ny = nx = g.npix
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:ny, 0:nx]
    base = np.zeros((ny, nx))
    for x, y, a in stars:
        base += a * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 0.62 ** 2))
    cub = base[None] + rng.normal(0, 0.05, (n, ny, nx))
    var = np.full_like(cub, 0.05 ** 2)
    return Cube(cub, var, np.linspace(60800, 61300, n), np.full(n, 1.0), g.wcs(), "SPHEREx-D4", "TEST", n), g


def test_brightest_and_75pct_rule():
    cube, _ = _cube([(10, 10, 10.0), (30, 12, 8.0), (20, 30, 5.0)])   # 100%, ~80%, ~50%
    got = objects.detect(cube)
    assert [round(o["x"]) for o in got] == [10, 30]                     # the 50% star is excluded
    assert got[0]["rel"] == 1.0 and 0.75 <= got[1]["rel"] < 1.0


def test_nothing_bright_means_no_sources():
    cube, _ = _cube([])
    assert objects.detect(cube) == []


def test_edge_sources_are_dropped():
    cube, _ = _cube([(1, 15, 10.0), (20, 20, 6.0)])
    got = objects.detect(cube)
    assert len(got) == 1 and round(got[0]["x"]) == 20


def test_sky_position_matches_wcs():
    cube, g = _cube([(15, 15, 10.0)])
    o = objects.detect(cube)[0]
    ra, dec = g.wcs().pixel_to_world_values(15, 15)
    assert abs(o["ra"] - ra % 360) < 1e-4 and abs(o["dec"] - dec) < 1e-4


def test_saturated_core_is_found_and_counts_as_brightest():
    cube, _ = _cube([(30, 12, 8.0)])
    cube.cube[:, 18:21, 18:21] = np.nan                     # delivered pixels of a saturated star are NaN
    got = objects.detect(cube)
    assert len(got) == 1 and got[0]["saturated"] and abs(got[0]["x"] - 19) < 1 and abs(got[0]["y"] - 19) < 1


def test_edge_holes_are_not_stars():
    cube, _ = _cube([(30, 12, 8.0)])
    cube.cube[:, 0:3, 0:5] = np.nan
    got = objects.detect(cube)
    assert len(got) == 1 and not got[0]["saturated"]


def _points(f):
    lam = np.geomspace(0.76, 4.9, 300)
    return [dict(lambda_um=float(l), flux=float(f(l)), err=0.01) for l in lam]


def test_features_are_colours_not_brightness():
    a = classifier.features(_points(lambda l: 5.0 * l ** -2))
    b = classifier.features(_points(lambda l: 500.0 * l ** -2))
    assert a.shape == (classifier.N_FEATURES,)
    assert np.allclose(a[:47], b[:47], equal_nan=True)                 # same shape, 100x brighter: identical colours


def test_features_separate_blue_and_red_spectra():
    blue = classifier.features(_points(lambda l: l ** -3))
    red = classifier.features(_points(lambda l: l ** 1.0))
    assert not np.allclose(blue[:24], red[:24], equal_nan=True)


def test_thin_spectrum_is_rejected():
    assert classifier.features([]) is None
    assert classifier.features(_points(lambda l: 1.0)[:3]) is None


def test_classifier_held_out_accuracy_exceeds_97_percent():
    import pandas as pd
    from sklearn.model_selection import GroupKFold
    from scripts.train_classifier import BAR, D, make_model

    df = pd.read_csv(D / "features.csv")
    fcols = [f"f{i}" for i in range(classifier.N_TOTAL_FEATURES)]
    X, y = df[fcols].to_numpy(np.float32), df["cls"].to_numpy()
    cell = (df["ra"] // 10).astype(int) * 100 + ((df["dec"] + 90) // 10).astype(int)
    preds = np.empty_like(y)
    for tr, te in GroupKFold(5).split(X, y, groups=cell):
        m = make_model().fit(X[tr], y[tr])
        preds[te] = m.predict(X[te])
    acc = float((preds == y).mean())
    assert acc >= BAR, f"Held-out accuracy {acc:.2%} is below 97% bar"


def test_trained_model_classifies_all_six_archetypes():
    import pandas as pd
    from scripts.train_classifier import D

    df = pd.read_csv(D / "features.csv")
    fcols = [f"f{i}" for i in range(classifier.N_TOTAL_FEATURES)]
    for cls in classifier.CLASSES:
        row = df[df["cls"] == cls].iloc[0]
        x = row[fcols].to_numpy(np.float32)
        res = classifier.classify_features(x)
        assert res["cls"] == cls and res["prob"] > 0.7


def test_precomputed_stage_files_contain_classified_objects():
    import json
    from pathlib import Path

    stage_dir = Path(__file__).resolve().parent.parent / "data" / "stage"
    for name in ("barnard", "epsindi", "wise0855", "rwaur", "rdra"):
        data = json.loads((stage_dir / f"{name}.json").read_text())
        assert "objects" in data and len(data["objects"]) >= 1
        assert data["objects"][0]["rel"] == 1.0
        assert all(o["rel"] >= 0.75 for o in data["objects"])
        assert all(o["cls"] in classifier.CLASSES for o in data["objects"])
    # wise0855 framed field has two bright field stars meeting the >= 75% rule (100% and 90%)
    w = json.loads((stage_dir / "wise0855.json").read_text())["objects"]
    assert len(w) == 2 and w[0]["rel"] == 1.0 and w[1]["rel"] >= 0.75

