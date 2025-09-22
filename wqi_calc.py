# wqi_calc.py
import math

def _clip(x, a, b):
    if x < a:
        return a
    if x > b:
        return b
    return x

def _tanh(x):
    try:
        return math.tanh(x)
    except AttributeError:
        e_pos = math.exp(x)
        e_neg = math.exp(-x)
        return (e_pos - e_neg) / (e_pos + e_neg)

def compute_wqi_from_minimal(tds, ph, turbidity, defaults=None):
    if defaults is None:
        defaults = {
            "DO_mg_L": 8.0,
            "BOD_mg_L": 2.0,
            "COD_mg_L": 10.0,
            "nitrate_mg_L": 1.0,
            "phosphate_mg_L": 0.1,
            "fecal_coliform_CFU_100mL": 10.0
        }

    DO = float(defaults.get("DO_mg_L", 8.0))
    BOD = float(defaults.get("BOD_mg_L", 2.0))
    COD = float(defaults.get("COD_mg_L", 10.0))
    nitrate = float(defaults.get("nitrate_mg_L", 1.0))
    phosphate = float(defaults.get("phosphate_mg_L", 0.1))
    fecal = float(defaults.get("fecal_coliform_CFU_100mL", 10.0))

    try:
        tds = float(tds)
    except:
        tds = 0.0
    try:
        ph = float(ph)
    except:
        ph = 7.0
    try:
        turbidity = float(turbidity)
    except:
        turbidity = 0.0

    term_pH = 8.0 * _clip(1.0 - abs(ph - 7.0) / 1.5, 0.0, 1.0)
    term_DO = 15.0 * _clip(DO / 12.0, 0.0, 1.0)
    term_turb = -8.0 * _tanh(turbidity / 15.0)
    term_BOD = -10.0 * _tanh(BOD / 6.0)
    term_COD = -10.0 * _tanh(COD / 40.0)
    term_nitrate = -6.0 * _tanh(nitrate / 6.0)
    term_phosphate = -6.0 * _tanh(phosphate / 0.8)
    term_fecal = -8.0 * _tanh(fecal / 150.0)
    term_tds = -5.0 * _tanh(tds / 600.0)

    wqi = 12.0 + term_pH + term_DO + term_turb + term_BOD + term_COD + term_nitrate + term_phosphate + term_fecal + term_tds
    if wqi < 0:
        wqi = 0.0
    if wqi > 100:
        wqi = 100.0

    if wqi >= 80:
        category = "Excellent"
        desc = "Safe for drinking and all uses"
    elif wqi >= 60:
        category = "Good"
        desc = "Acceptable quality, minor treatment needed"
    elif wqi >= 40:
        category = "Moderate"
        desc = "Needs treatment before use"
    else:
        category = "Poor"
        desc = "Unsafe, high risk of waterborne diseases"

    return round(wqi, 2), category, desc
