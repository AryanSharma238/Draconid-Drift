"""Train the object classifier and measure how accurate it really is.

    python scripts/train_classifier.py [--min-prob 0.5]

Accuracy is measured on objects the model never saw: 5-fold cross-validation where folds are SKY REGIONS (objects
within the same ~10 degree cell always share a fold), so neighbours can not leak between train and test. The bar for
shipping is 97% (docs: engine must exceed 97% on common object types; rare objects are out of scope). The report
gives accuracy on every object, and accuracy + coverage when the model may answer "unclear" below --min-prob.
The final model (fit on everything) is only written when the all-objects accuracy clears the bar, or with --force.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from drift import classifier  # noqa: E402

BAR = 0.97
D = Path(__file__).resolve().parent.parent / "data" / "classify"


def make_model():
    from sklearn.ensemble import HistGradientBoostingClassifier

    return HistGradientBoostingClassifier(
        max_depth=4, learning_rate=0.08, max_iter=250, l2_regularization=1.0, random_state=0
    )


def _augment_modality_dropout(X: np.ndarray, y: np.ndarray, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Augment training folds with partial-modality views (single-detector SPHEREx + catalog, or SPHEREx-only)
    so live stage classification stays accurate even when only one detector cube or only catalog bands are present."""
    n_spx = classifier.N_SPX_BASE + classifier.N_SPX_BROAD
    X_cat_only = X.copy()
    X_cat_only[:, :n_spx] = np.nan
    X_spx_only = X.copy()
    X_spx_only[:, n_spx:] = np.nan
    return np.vstack([X, X_cat_only, X_spx_only]), np.concatenate([y, y, y])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-prob", type=float, default=classifier.MIN_PROB)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    from sklearn.metrics import confusion_matrix
    from sklearn.model_selection import GroupKFold

    df = pd.read_csv(D / "features.csv")
    fcols = [f"f{i}" for i in range(classifier.N_TOTAL_FEATURES)]
    X, y = df[fcols].to_numpy(np.float32), df["cls"].to_numpy()
    cell = (df["ra"] // 10).astype(int) * 100 + ((df["dec"] + 90) // 10).astype(int)
    print(f"{len(df)} objects:", df["cls"].value_counts().to_dict())
    if df["cls"].value_counts().min() < 30:
        print("WARNING: a class has under 30 objects; its accuracy is not well measured")

    prob = np.zeros((len(df), len(classifier.CLASSES)))
    classes = np.array(classifier.CLASSES)
    for tr, te in GroupKFold(5).split(X, y, groups=cell):
        m = make_model().fit(X[tr], y[tr])
        p = m.predict_proba(X[te])
        for j, c in enumerate(m.classes_):
            prob[te, list(classes).index(c)] = p[:, j]
    pred = classes[prob.argmax(1)]
    conf = prob.max(1)
    acc = float((pred == y).mean())
    keep = conf >= a.min_prob
    print(f"\nHELD-OUT accuracy, every object      : {acc:.2%}   (bar {BAR:.0%})")
    print(f"HELD-OUT accuracy, confident only    : {float((pred[keep] == y[keep]).mean()):.2%} "
          f"at coverage {keep.mean():.1%} (min-prob {a.min_prob})")
    print("\nper class (recall, held out):")
    for c in classifier.CLASSES:
        s = y == c
        if s.any():
            print(f"  {c:7s} n={int(s.sum()):4d}  recall={float((pred[s] == c).mean()):.1%}  "
                  f"precision={float((y[pred == c] == c).mean()) if (pred == c).any() else float('nan'):.1%}")
    print("\nconfusion (rows true, cols predicted):", list(classes))
    print(confusion_matrix(y, pred, labels=classes))
    if acc >= BAR or a.force:
        import joblib

        X_aug, y_aug = _augment_modality_dropout(X, y)
        joblib.dump(make_model().fit(X_aug, y_aug), classifier.MODEL_PATH)
        print("\nmodel written to", classifier.MODEL_PATH)
        return 0
    print(f"\nNOT written: held-out accuracy {acc:.2%} is below the {BAR:.0%} bar.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
