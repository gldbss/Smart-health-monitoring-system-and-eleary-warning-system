#!/usr/bin/env python3
"""
create_model_coef_from_pkl.py

Usage examples:
  python create_model_coef_from_pkl.py --pkl wqi_model_rf.pkl
  python create_model_coef_from_pkl.py --pkl wqi_model_rf.pkl --out model_coef.py --n 1000
  python create_model_coef_from_pkl.py --pkl wqi_model_rf.pkl --csv mydata.csv   # use CSV features if available

What it does:
 - Loads the provided pickle / joblib model
 - Builds a representative X (TDS, pH, turbidity) matrix (or loads features from CSV if given)
 - Obtains model predictions y_pred = model.predict(X)
 - Fits a Ridge linear approximation (closed-form) to y_pred
 - Writes model_coef.py with variables: b, c_tds, c_ph, c_turbidity
 - Prints fit-quality stats (RMSE, R^2) so you can check approximation quality
"""
import argparse
import os
import sys
import pickle
from pathlib import Path

import numpy as np

try:
    # joblib is preferred for some saved models
    import joblib
    _HAS_JOBLIB = True
except Exception:
    _HAS_JOBLIB = False

def try_load_model(path):
    # Try joblib first (if available), then pickle.load
    if _HAS_JOBLIB:
        try:
            print("Trying joblib.load() ...")
            m = joblib.load(path)
            print("Loaded with joblib.")
            return m
        except Exception as e:
            print("joblib.load failed:", e)
    # fallback to pickle
    try:
        print("Trying pickle.load() ...")
        with open(path, "rb") as f:
            m = pickle.load(f)
        print("Loaded with pickle.")
        return m
    except Exception as e:
        raise RuntimeError(f"Failed to load model from {path}: {e}")

def generate_synthetic_X(n_samples=800, seed=42):
    # Generates representative samples for (TDS_mg_L, pH, turbidity_NTU)
    # Distribution chosen to resemble typical ranges:
    rng = np.random.RandomState(seed)
    tds = rng.uniform(10, 2000, size=n_samples)         # ppm
    ph = rng.uniform(6.2, 8.8, size=n_samples)          # pH range used earlier
    turbidity = rng.uniform(0.0, 50.0, size=n_samples)  # NTU
    X = np.vstack([tds, ph, turbidity]).T
    return X

def load_X_from_csv(csv_path, feature_names=None):
    import pandas as pd
    df = pd.read_csv(csv_path)
    # try common column names if feature_names not supplied
    if feature_names is None:
        candidates = {
            "tds": ["TDS_mg_L", "TDS", "tds", "tds_mg_l"],
            "ph": ["pH", "ph", "PH"],
            "turb": ["turbidity_NTU", "turbidity", "turbidity_NTU", "turb"]
        }
        found = {}
        for key, names in candidates.items():
            for n in names:
                if n in df.columns:
                    found[key] = n
                    break
            if key not in found:
                raise RuntimeError(f"Could not find a column for {key} in CSV. Candidates: {names}")
        X = df[[found["tds"], found["ph"], found["turb"]]].values.astype(float)
        return X
    else:
        X = df[feature_names].values.astype(float)
        return X

def fit_ridge_closed_form(X, y, alpha=1.0):
    # X: (n, p) -> build X_b with intercept column
    n, p = X.shape
    X_b = np.hstack([np.ones((n,1)), X])  # (n, p+1)
    W = np.diag([0.0] + [alpha]*p)       # no regularization on intercept
    A = X_b.T.dot(X_b) + W
    theta = np.linalg.solve(A, X_b.T.dot(y))
    b = float(theta[0])
    coefs = [float(x) for x in theta[1:]]
    return b, coefs

def approx_predict_linear(b, coefs, X):
    # compute predictions: y = b + X @ coefs
    return b + X.dot(np.array(coefs))

def compute_metrics(y_true, y_pred):
    # RMSE and R^2
    mse = np.mean((y_true - y_pred)**2)
    rmse = np.sqrt(mse)
    # R2
    ss_res = np.sum((y_true - y_pred)**2)
    ss_tot = np.sum((y_true - np.mean(y_true))**2)
    r2 = 1.0 - ss_res/ss_tot if ss_tot != 0 else float('nan')
    return rmse, r2

def write_model_coef(out_path, b, c_tds, c_ph, c_turbidity):
    with open(out_path, "w") as f:
        f.write("# auto-generated model_coef.py (approximation)\n")
        f.write("# Generated from pkl approximation\n")
        f.write("b = {:.6f}\n".format(b))
        f.write("c_tds = {:.6f}\n".format(c_tds))
        f.write("c_ph = {:.6f}\n".format(c_ph))
        f.write("c_turbidity = {:.6f}\n".format(c_turbidity))

def main():
    p = argparse.ArgumentParser(description="Create model_coef.py from a scikit-learn .pkl / joblib model.")
    p.add_argument("--pkl", required=True, help="Path to wqi_model_rf.pkl (or joblib .pkl)")
    p.add_argument("--out", default="model_coef.py", help="Output path for model_coef.py")
    p.add_argument("--n", type=int, default=1000, help="Number of synthetic samples to generate (if no CSV)")
    p.add_argument("--seed", type=int, default=123, help="Random seed for synthetic data")
    p.add_argument("--alpha", type=float, default=1.0, help="Ridge regularization alpha")
    p.add_argument("--csv", default=None, help="Optional CSV path with columns for TDS, pH, turbidity to build X from real data")
    args = p.parse_args()

    pkl_path = args.pkl
    if not os.path.exists(pkl_path):
        print("ERROR: pickle file not found:", pkl_path)
        sys.exit(1)

    print("Loading model from:", pkl_path)
    model = try_load_model(pkl_path)

    if not hasattr(model, "predict"):
        print("ERROR: loaded object has no predict() method. Type:", type(model))
        sys.exit(1)

    # Prepare X
    if args.csv:
        print("Loading X from CSV:", args.csv)
        try:
            X = load_X_from_csv(args.csv)
        except Exception as e:
            print("Failed to load CSV:", e)
            sys.exit(1)
    else:
        print("Generating synthetic X: n =", args.n)
        X = generate_synthetic_X(n_samples=args.n, seed=args.seed)

    # Try to call model.predict(X). Many sklearn pipelines/estimators accept raw 2D numpy arrays.
    try:
        print("Calling model.predict(X) ...")
        y_pred = model.predict(X)
    except Exception as e:
        # Helpful debug: if it's a pipeline with named_steps, try to locate last estimator
        print("model.predict failed:", e)
        # Try pipeline fallback heuristics
        try:
            if hasattr(model, "named_steps"):
                print("Detected Pipeline object. Trying pipeline.predict(X) again ...")
                y_pred = model.predict(X)
            else:
                raise e
        except Exception as e2:
            print("Still failed to get predictions from model:", e2)
            print("You must run this script on the same environment that created the .pkl and ensure pipeline accepts raw X.")
            sys.exit(1)

    y_pred = np.asarray(y_pred).astype(float).ravel()
    print("Got predictions from model. y_pred shape:", y_pred.shape)

    # Fit Ridge closed-form approximation
    print("Fitting Ridge closed-form (alpha = {}) ...".format(args.alpha))
    b, coefs = fit_ridge_closed_form(X, y_pred, alpha=args.alpha)
    c_tds, c_ph, c_turbidity = coefs[0], coefs[1], coefs[2]
    print("Fitted coefficients:")
    print("  b =", b)
    print("  c_tds =", c_tds)
    print("  c_ph =", c_ph)
    print("  c_turbidity =", c_turbidity)

    # Evaluate approximation quality on the same X
    y_lin = approx_predict_linear(b, coefs, X)
    rmse, r2 = compute_metrics(y_pred, y_lin)
    print("Approximation quality (vs model predictions) on same X:")
    print("  RMSE =", rmse)
    print("  R2   =", r2)

    # Save model_coef.py
    out_path = args.out
    write_model_coef(out_path, b, c_tds, c_ph, c_turbidity)
    print("Wrote model_coef.py to", out_path)
    print("Preview:")
    print(Path(out_path).read_text())

    print("\nCOPY model_coef.py to the ESP (Thonny / ampy / mpfshell / WebREPL).")
    print("Then reboot the ESP. main.py will import and use these coefficients for inference.")

if __name__ == "__main__":
    main()
