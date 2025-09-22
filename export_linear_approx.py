# export_linear_approx.py
import pandas as pd
import numpy as np
from pathlib import Path

df = pd.read_csv("water_quality_data.csv")
X = df[["TDS_mg_L","pH","turbidity_NTU"]].values
y = df["WQI"].values

alpha = 1.0
n, p = X.shape
X_b = np.hstack([np.ones((n,1)), X])
W = np.diag([0.0] + [alpha]*p)
A = X_b.T.dot(X_b) + W
theta = np.linalg.solve(A, X_b.T.dot(y))
b = float(theta[0])
c = [float(x) for x in theta[1:]]

with open("model_coef.py","w") as f:
    f.write("# auto-generated model_coef.py (from CSV)\n")
    f.write("b = {:.6f}\n".format(b))
    f.write("c_tds = {:.6f}\n".format(c[0]))
    f.write("c_ph = {:.6f}\n".format(c[1]))
    f.write("c_turbidity = {:.6f}\n".format(c[2]))
print("Wrote model_coef.py")
