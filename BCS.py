# BCS Survey Logic Checker — Lite (rules-json, issues-only digest)
# Streamlit app that runs row-level logic checks using your column schema only.
# Excludes last-wave, client-sample, and desk-research inputs & checks.

import re
from typing import Dict, List, Set

import numpy as np
import pandas as pd
import streamlit as st
import json
import csv
from io import BytesIO

# -------------------------------------------------------------------
# App setup + solid background colors (main + sidebar)
# -------------------------------------------------------------------
st.set_page_config(page_title="BCS Survey Logic Checker", layout="wide")

def set_background_solid(main="#EAF3FF", sidebar="#F5F7FB"):
    st.markdown(f"""
    <style>
      /* MAIN AREA */
      [data-testid="stAppViewContainer"],
      [data-testid="stAppViewContainer"] .main,
      [data-testid="stAppViewContainer"] .block-container {{
        background-color: {main} !important;
      }}
      /* SIDEBAR */
      [data-testid="stSidebar"],
      [data-testid="stSidebar"] > div,
      [data-testid="stSidebar"] .block-container {{
        background-color: {sidebar} !important;
      }}
      header[data-testid="stHeader"] {{ background: transparent; }}
      [data-testid="stDataFrame"],
      [data-testid="stTable"] {{ background-color: transparent !important; }}
    </style>
    """, unsafe_allow_html=True)

set_background_solid()  # ocean-blue main, soft-gray sidebar

st.title("📊 BCS Survey Logic Checker")
st.caption("Identified cases with issues are displayed below. Optionally: Upload a custom rules JSON to add new rules.")

# -------------------------------------------------------------------
# Constants (no sliders)
# -------------------------------------------------------------------
A1A_CAP = 7                       # 12) Unaided awareness: flag if > 7 mentions
MIN_CLOSE_B2_EQ5 = 8              # 4) C-close threshold when B2 = 5
MIN_CLOSE_LO    = 7               # 4) Softer C-close threshold (B2=4, or considered/preferred)
CFUNC_HIGH      = 7               # 5) High Cfunc cutoff
B2_LOW_MAX      = 2               # 5/6/7/8) “Low” B2 = 1–2
B2_HIGH_MIN     = 3               # 6/7/8) “High” B2 = 3–5

# S4a1 ↔ A3 mapping constants
SURVEY_YEAR = 2025                 # Set to your wave year
S4_TO_YEAR = {1: 2025, 2: 2024, 3: 2023, 4: 2022, 5: 2021, 6: 2020}
S4_TO_YEARS_AGO = {code: SURVEY_YEAR - yr for code, yr in S4_TO_YEAR.items()}  # {1:0, 2:1, ..., 6:5}

# -------------------------------------------------------------------
# Rule registry: numbers, short names, meanings, and colors
# -------------------------------------------------------------------
RULES = {
    1:  {"title": "A2b→B3a KPI", "meaning": "Share of A2b main brand that is also in B3a.", "color": "#DDEBF7"},
    2:  {"title": "> A1a cap", "meaning": f"Sum of A1a unaided brands > {A1A_CAP}.", "color": "#FFE0E0"},
    3:  {"title": "A1a vs S3 high", "meaning": "Awareness count seems high vs fleet size.", "color": "#FFD7B5"},
    4:  {"title": "C-close high bar", "meaning": "Closeness lower than expected (target ≥8).", "color": "#FFF2B2"},
    5:  {"title": "C-close soft bar", "meaning": "Closeness a bit low (target ≥7).", "color": "#FFF8CC"},
    6:  {"title": "B3a vs E4 (quota)", "meaning": "Considered quota make but E4 low.", "color": "#FAD7E2"},
    7:  {"title": "Cfunc vs B2", "meaning": "High performance but B2 very low.", "color": "#E6E0FF"},
    8:  {"title": "E4 low vs B2 high", "meaning": "Likelihood to choose low but B2 high.", "color": "#D8F3F1"},
    9:  {"title": "E4 high vs B2 low", "meaning": "Likelihood to choose high but B2 low.", "color": "#CDE7F9"},
    10: {"title": "A2b not in A1a", "meaning": "Main brand not listed in unaided awareness.", "color": "#E0ECFF"},
    11: {"title": "A2a vs A4", "meaning": "Used brand but never used authorized workshop.", "color": "#E2F0D9"},
    12: {"title": "A2b not in A2a", "meaning": "Main brand not listed in usage.", "color": "#FDE2CF"},
    13: {"title": "B3a → B2≥4", "meaning": "Considered brand but B2 ≤3.", "color": "#FAD2E1"},
    14: {"title": "B3b → B2≥4", "meaning": "Preferred brand but B2 ≤3.", "color": "#CDE7B0"},
    15: {"title": "E1 high vs B2 low", "meaning": "Overall satisfaction high but B2 low.", "color": "#D1FADF"},
    16: {"title": "E1 low vs B2 high", "meaning": "Overall satisfaction low but B2 high.", "color": "#BEE3F8"},
    17: {"title": "E1 vs F1", "meaning": "F1 1–2 with E1 4–5, or F1 4–5 with E1 1–2.", "color": "#E5E5E5"},
    18: {"title": "E4c low vs B2 high", "meaning": "Preference strength low but B2 high.", "color": "#D9EAD3"},
    19: {"title": "E4c high vs B2 low", "meaning": "Preference strength high but B2 low.", "color": "#D9D2E9"},
    20: {"title": "S4a1 ↔ A3", "meaning": "Last purchase (S4a1) doesn’t match A3.", "color": "#F6E0B5"},
    21: {"title": "A4 vs A4b", "meaning": "Service vs parts visit differ by >3 years.", "color": "#F4CCCC"},
    22: {"title": "Straight-liner (all 3)", "meaning": "Same score across F2/F4/F6.", "color": "#EAD1DC"},
    23: {"title": "B1≤2 given B2", "meaning": "Familiarity very low for a rated brand.", "color": "#FCE5CD"},
    24: {"title": "Aware but B1≤2", "meaning": "Aware but familiarity too low.", "color": "#FFF2CC"},  # (kept in legend; logic optional)
    25: {"title": "G2 vs G1", "meaning": "Operation range unmatched for industry.", "color": "#CCE5FF"},
}

# -------------------------------------------------------------------
# Robust file reading helpers (CSV + Excel, encoding & delimiter auto)
# -------------------------------------------------------------------
COMMON_ENCODINGS = ["utf-8", "utf-8-sig", "cp1252", "latin-1"]
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")  # .xlsx/.zip magic

def _sniff_sep(sample_text: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample_text[:4096], delimiters=",;\t|")
        return dialect.delimiter
    except Exception:
        return ","  # fallback

def _norm_delim(sel: str) -> str:
    return {"\\t": "\t"}.get(sel, sel)

def read_any_table(uploaded_file, enc_override="auto", delim_override="auto", skip_bad=True) -> pd.DataFrame:
    name = (uploaded_file.name or "").lower()
    raw = uploaded_file.read()  # bytes

    if raw.startswith(ZIP_SIGNATURES) or name.endswith((".xlsx", ".xls")):
        uploaded_file.seek(0)
        return pd.read_excel(uploaded_file)

    encodings = COMMON_ENCODINGS if enc_override == "auto" else [enc_override]
    for enc_try in encodings:
        try:
            text = raw.decode(enc_try, errors="strict")
            sep = _sniff_sep(text) if delim_override == "auto" else _norm_delim(delim_override)
            kwargs = dict(encoding=enc_try, sep=sep, engine="python")
            if skip_bad:
                kwargs["on_bad_lines"] = "skip"
            return pd.read_csv(BytesIO(raw), **kwargs)
        except Exception:
            continue

    sep = "," if delim_override == "auto" else _norm_delim(delim_override)
    kwargs = dict(encoding="latin-1", sep=sep, engine="python")
    if skip_bad:
        kwargs["on_bad_lines"] = "skip"
    return pd.read_csv(BytesIO(raw), **kwargs)

# -------------------------------------------------------------------
# Upload
# -------------------------------------------------------------------
with st.sidebar:
    st.header("Input")
    data_file = st.file_uploader("Current wave data", type=["csv", "xlsx", "xls"])
    rules_file = st.file_uploader("Optional: custom rules JSON", type=["json"])
    rules = json.load(rules_file) if rules_file else None

    st.markdown("---")
    st.subheader("Parser overrides")
    enc = st.selectbox("Encoding", ["auto", "utf-8", "utf-8-sig", "cp1252", "latin-1"], index=0)
    delim = st.selectbox("Delimiter", ["auto", ",", ";", "\\t", "|"], index=0)
    skip_bad = st.checkbox("Skip bad lines", value=True)

    st.markdown("---")
    show_full = st.checkbox("Show full result table (not just issues)", value=False)

if not data_file:
    st.info("Upload a CSV/XLSX to begin.")
    st.stop()

try:
    data_file.seek(0)
    df = read_any_table(data_file, enc_override=enc, delim_override=delim, skip_bad=skip_bad)
except Exception as e:
    st.error(f"Failed to read file: {e}")
    st.stop()

# Normalize common null tokens early so numerics parse cleanly
df.replace(
    {"#NULL!": np.nan, "NULL": np.nan, "null": np.nan, "NaN": np.nan, "nan": np.nan,
     "": np.nan, "na": np.nan, "N/A": np.nan, "n/a": np.nan},
    inplace=True,
)

res = df.copy()

# -------------------------------------------------------------------
# Helpers & mappings (from your schema)
# -------------------------------------------------------------------
PREFIX = {
    "awareness":   "unaided_aware_",        # A1a multi (0/1)
    "usage":       "usage_",                # A2a multi (0/1) — note: not used in new B2 rule
    "impression":  "overall_impression_",   # B2 1–5
    "consider":    "consideration_",        # B3a multi (0/1)
    "close":       "closeness_",            # C-close 1–10
    "cfunc":       "performance_",          # Cfunc 1–10
    "familiarity": "familiarity_",          # B1 1–5
}

COL = {
    "main_brand":           "main_brand",                    # A2b (preferred of A2a)
    "pref_future_single":   "preference",                    # B3b single pick
    "E1_overall":           "overall_satisfaction",          # E1 (1–5)
    "E4_choose_brand":      "likelihood_choose_brand",       # E4 (1–5)
    "E4c_pref_strength":    "preference_strength",           # E4c (1–5)
}

S = {
    "HD_count":          "n_heavy_duty_trucks",  # S3
    "Tractors":          "n_tractors",           # S3a1
    "Rigids":            "n_rigids",             # S3a2
    "Tippers":           "n_tippers",            # S3a3
    "LastPurchaseHD_cat":"last_purchase_hdt",    # S4a1 (coded 1..9)
}

BRAND_NAME_TO_CODE = {
    "ashok leyland": "1", "asia motor works": "2", "beijing auto/baic/beiqi futian": "3", "chevrolet": "4",
    "cnhtc/steyr": "5", "tata / tata daewoo": "6", "daf": "7", "dongfeng": "8", "eicher": "9",
    "erf": "10", "foden": "11", "force motors": "12", "ford": "13", "freightliner": "14", "hino": "15",
    "hongyan/sichuan auto/saic": "16", "hyundai": "17", "international": "18", "isuzu": "19", "iveco": "20",
    "jie fang/faw": "21", "kenworth": "22", "mack": "23", "mahindra": "24", "man": "25",
    "mercedes benz": "26", "fuso": "27", "ud trucks": "28", "norinco": "29", "peterbilt": "30",
    "renault trucks": "31", "scania": "32", "sterling": "33", "tata motors": "34", "tatra": "35",
    "western star": "36", "volkswagen": "37", "volvo": "38", "yan an/shaanxi auto": "39",
    "swaraj mazda limited": "40", "bharat benz": "41", "nissan diesel": "42", "cat": "43",
    "dennis eagle": "44", "jac": "45", "camc": "46", "foton": "47", "sinotruck / sitrak": "48",
    "sany": "49", "shacman": "50", "powerland": "51", "powerstar": "52", "howo": "53", "hitachi": "54",
    "quester": "55", "lgmg": "56", "liugong": "57", "other": "98",
}
CODE_TO_BRAND = {v: k.title() for k, v in BRAND_NAME_TO_CODE.items()}
BRAND_SUFFIX_RE = re.compile(r"_b(?P<brand>\d+)$", re.IGNORECASE)

# Discover brands present for each block
brand_cols: Dict[str, Dict[str, str]] = {blk: {} for blk in PREFIX}
for c in df.columns:
    for blk, pre in PREFIX.items():
        if c.startswith(pre):
            m = BRAND_SUFFIX_RE.search(c)
            if m:
                brand_cols[blk][m.group("brand")] = c

brands: Set[str] = set().union(*[set(d.keys()) for d in brand_cols.values()])
getb = lambda mapping, b: mapping.get(str(b))

# ---------------- Helpers ----------------
def to_num(x):
    return pd.to_numeric(x, errors="coerce")

def boolish(x) -> bool:
    if pd.isna(x): return False
    if isinstance(x, (int, np.integer, float, np.floating)):
        try: return float(x) != 0.0
        except Exception: return False
    s = str(x).strip().lower()
    if s in {"", "nan", "none", "null", "#null!", "na", "n/a"}: return False
    if s in {"true","t","yes","y"}: return True
    if s in {"false","f","no","n"}: return False
    try: return float(s) != 0.0
    except Exception: return s == "1"

def in_vals(x, allowed: List[int]) -> bool:
    try: return int(float(x)) in allowed
    except Exception: return False

def parse_brand_id(val) -> str | None:
    if val is None: return None
    if isinstance(val, (int, np.integer)): return str(int(val))
    if isinstance(val, float):
        if np.isnan(val): return None
        if float(val).is_integer(): return str(int(val))
        return None
    s = str(val).strip()
    if not s: return None
    sl = s.lower()
    if sl in {"nan","none","null","#null!","na"}: return None
    m = re.fullmatch(r"(\d+)(?:\.0+)?", sl)
    if m: return m.group(1)
    m = re.search(r"\bb\s*(\d+)\b$", sl)
    if m: return m.group(1)
    sl2 = re.sub(r"\s+", " ", sl.replace("-", " ").replace("/", " / ")).strip()
    if sl2 in BRAND_NAME_TO_CODE: return BRAND_NAME_TO_CODE[sl2]
    m = re.search(r"(\d+)", sl)
    if m: return m.group(1)
    return None

def consider_mark(x):
    if x is None or pd.isna(x): return np.nan
    s = str(x).strip().lower()
    if s in {"","nan","none","null","#null!","na","n/a"}: return np.nan
    if s in {"1","true","t","yes","y"}: return True
    if s in {"0","false","f","no","n"}: return False
    try:
        v = float(s)
        if np.isnan(v): return np.nan
        return int(v) == 1
    except Exception:
        return False

# ----- Optional custom rules JSON -----
def apply_custom_rules(df: pd.DataFrame, res: pd.DataFrame, rules: dict | None) -> pd.DataFrame:
    if not rules:
        return res
    for r in rules.get("rules", []):
        t = r.get("type")
        name = r.get("name") or f"CHK_{t}"
        msg = r.get("message", "Violation")
        if t == "equals":
            a, b = r.get("cols", [None, None])
            if a in df.columns and b in df.columns:
                bad = df[a].astype(str) != df[b].astype(str)
                res[name] = "OK"
                res.loc[bad, name] = msg
        elif t == "implies_values":
            cond_col = r.get("if", {}).get("col")
            then_col = r.get("then", {}).get("col")
            cond_vals = r.get("if", {}).get("in", [])
            allow_vals = r.get("then", {}).get("allowed", [])
            if cond_col in df.columns and then_col in df.columns:
                cond = df[cond_col].isin(cond_vals)
                bad = cond & ~df[then_col].isin(allow_vals)
                res[name] = np.where(cond, "OK", res.get(name, "")).astype(str)
                res.loc[bad, name] = msg
        elif t == "brand_consider_implies_impression":
            consider_prefix = r.get("consider_prefix", "consideration_")
            impression_prefix = r.get("impression_prefix", "overall_impression_")
            allowed = r.get("allowed", [4, 5])
            for col in df.columns:
                if not col.startswith(consider_prefix): continue
                m = re.search(r"_b(\d+)$", col)
                if not m: continue
                bid = m.group("brand")
                tgt = f"{impression_prefix}b{bid}"
                if tgt not in df.columns: continue
                name_bid = f"{name}_b{bid}"
                bad = (to_num(df[col]) == 1) & ~to_num(df[tgt]).isin(allowed)
                res[name_bid] = "OK"
                res.loc[bad, name_bid] = msg
    return res

# -------------------------------------------------------------------
# Checks
# -------------------------------------------------------------------
# 0) Structural: S3a1-3 sum equals S3 (only if any S3a value present)
if all(k in df.columns for k in [S["HD_count"], S["Tractors"], S["Rigids"], S["Tippers"]]):
    parts = pd.DataFrame({
        "tractors": to_num(df[S["Tractors"]]),
        "rigids":   to_num(df[S["Rigids"]]),
        "tippers":  to_num(df[S["Tippers"]]),
    })
    has_any_s3a = parts.notna().any(axis=1)
    subsum = parts.fillna(0).sum(axis=1)
    total = to_num(df[S["HD_count"]])
    res["CHK_S3a_sum"] = "OK"
    res.loc[has_any_s3a & total.notna() & (subsum != total), "CHK_S3a_sum"] = "S3a1-3 ≠ S3"

# 3) A1a total sanity (merged cap + S3 sanity)
aw_cols = list(brand_cols["awareness"].values())
if aw_cols:
    sel_aw = df[aw_cols].applymap(boolish)
    count_aw = sel_aw.sum(axis=1)
    res["CHK_A1a_total_count"] = count_aw
    res["CHK_A1a_total_flag"] = "OK"
    over_cap = count_aw > A1A_CAP
    res.loc[over_cap, "CHK_A1a_total_flag"] = f">{A1A_CAP} brands"
    if S["HD_count"] in df.columns:
        s3 = to_num(df[S["HD_count"]]).fillna(0)
        allowed = np.maximum(A1A_CAP, np.minimum(25, s3 + 4))
        over_s3 = count_aw > allowed
        res.loc[over_s3, "CHK_A1a_total_flag"] = "Too many brands vs fleet size"

# 4) Main make (A2b) must be in A1a and A2a (kept)
if COL["main_brand"] in df.columns:
    res["CHK_A2b_in_A1a"] = "OK"
    res["CHK_A2b_in_A2a"] = "OK"
    mb = df[COL["main_brand"]].apply(parse_brand_id)
    for i, b in mb.items():
        if b is None:
            continue
        a1col = getb(brand_cols["awareness"], b)
        ucol = getb(brand_cols["usage"], b)
        if a1col is not None and not boolish(df.at[i, a1col]):
            res.loc[i, "CHK_A2b_in_A1a"] = "Main brand not in A1a"
        if ucol is not None and not boolish(df.at[i, ucol]):
            res.loc[i, "CHK_A2b_in_A2a"] = "Main brand not in A2a"

# 8) If considering (B3a), B2 should be 4/5 (kept)
if brand_cols["consider"] and brand_cols["impression"]:
    for b in brands:
        ccol = getb(brand_cols["consider"], b)
        icol = getb(brand_cols["impression"], b)
        if not ccol or not icol:
            continue
        cons = df[ccol].apply(boolish)
        bad = cons & ~df[icol].apply(lambda x: in_vals(x, [4, 5]))
        name = f"CHK_B2_for_consider_b{b}"
        res[name] = "OK"
        res.loc[bad, name] = "B2 should be 4/5"

# 8b) Familiarity: if there is a B2 rating for the brand AND B1 ≤ 2 → flag
if brand_cols.get("familiarity") and brand_cols.get("impression"):
    for b in brands:
        fcol = getb(brand_cols["familiarity"], b)
        icol = getb(brand_cols["impression"], b)
        if fcol and icol and (fcol in df.columns) and (icol in df.columns):
            has_b2 = to_num(df[icol]).notna()
            low_b1 = df[fcol].apply(lambda x: in_vals(x, [1, 2]))
            bad = has_b2 & low_b1
            res.loc[bad, f"CHK_B1_low_given_B2_b{b}"] = "Familiarity (B1) ≤ 2 despite having B2 rating"
            
# 24) Aware (A1a) but familiarity (B1) ≤ 2  — brand-wise
if brand_cols.get("awareness") and brand_cols.get("familiarity"):
    for b in brands:
        aw   = getb(brand_cols["awareness"], b)
        fcol = getb(brand_cols["familiarity"], b)
        if not aw or not fcol:
            continue
        aware  = df[aw].apply(boolish)
        low_b1 = df[fcol].apply(lambda x: in_vals(x, [1, 2]))
        bad = aware & low_b1
        res.loc[bad, f"CHK_Aware_B1_low_b{b}"] = "Aware but familiarity too low"            

# 9) If preferred for next purchase (B3b single), B2 for that brand should be 4/5 (kept)
if COL["pref_future_single"] in df.columns and brand_cols["impression"]:
    pref = df[COL["pref_future_single"]].apply(parse_brand_id)
    for idx, b in pref.items():
        if b is None: continue
        icol = getb(brand_cols["impression"], b)
        if not icol: continue
        name = f"CHK_B2_for_pref_b{b}"
        res[name] = res.get(name, "OK")
        if not in_vals(df.at[idx, icol], [4, 5]):
            res.loc[idx, name] = "B2 should be 4/5 for preferred brand"

# 10) C-close expectations (constants)
if brand_cols["close"]:
    pref_series = df[COL["pref_future_single"]].apply(parse_brand_id) if COL.get("pref_future_single") in df.columns else None
    for b in brands:
        ccol = getb(brand_cols["close"], b)
        if not ccol: continue
        icol = getb(brand_cols["impression"], b)
        cns  = getb(brand_cols["consider"], b)

        close = to_num(df[ccol])
        b2    = to_num(df[icol]) if icol else pd.Series(np.nan, index=df.index)
        considered = df[cns].apply(boolish) if cns else pd.Series(False, index=df.index)
        preferred  = (pref_series == str(b)) if pref_series is not None else pd.Series(False, index=df.index)

        # B2=5 & considered -> ≥8
        mask_cons_hi = (b2 == 5) & considered & ~close.isin(range(MIN_CLOSE_B2_EQ5, 11))
        res.loc[mask_cons_hi, f"CHK_Cclose_B2eq5_considered_b{b}"] = "Expect 8–10 when B2=5 & considered"

        # B2=5 -> ≥8
        mask_b2_hi = (b2 == 5) & ~mask_cons_hi & ~close.isin(range(MIN_CLOSE_B2_EQ5, 11))
        res.loc[mask_b2_hi, f"CHK_Cclose_B2eq5_b{b}"] = f"Expect ≥{MIN_CLOSE_B2_EQ5}"

        # B2=4 -> ≥7
        mask_b2_lo = (b2 == 4) & ~close.isin(range(MIN_CLOSE_LO, 11))
        res.loc[mask_b2_lo, f"CHK_Cclose_B2eq4_b{b}"] = f"Expect ≥{MIN_CLOSE_LO}"

        # Preferred or considered -> ≥7
        mask_pref_cons = (~(b2 == 5)) & (~(b2 == 4)) & (preferred | considered) & ~close.isin(range(MIN_CLOSE_LO, 11))
        res.loc[mask_pref_cons, f"CHK_Cclose_pref_or_cons_b{b}"] = f"Expect ≥{MIN_CLOSE_LO}"

# 11) Cfunc vs B2 alignment: B2 ≤ 2 AND Cfunc ≥ 7
if brand_cols["cfunc"] and brand_cols["impression"]:
    for b in brands:
        cf = getb(brand_cols["cfunc"], b)
        icol = getb(brand_cols["impression"], b)
        if not cf or not icol: continue
        def misaligned(r):
            b2v = to_num(r[icol])
            cfv = to_num(r[cf])
            if pd.isna(b2v) or pd.isna(cfv): return False
            return (b2v <= B2_LOW_MAX) and (cfv >= CFUNC_HIGH)
        bad = df.apply(misaligned, axis=1)
        res.loc[bad, f"CHK_Cfunc_vs_B2_b{b}"] = "High performance but B2 very low"

# 23) Straight-liners across F2 (truck), F4 (sales/delivery), F6 (workshop)
def _section_vals(prefix: str) -> pd.DataFrame:
    cols = [c for c in df.columns if c.startswith(prefix)]
    return df[cols].apply(pd.to_numeric, errors="coerce") if cols else pd.DataFrame(index=df.index)

truck_vals = _section_vals("truck_rating_")
sales_vals = _section_vals("salesdelivery_rating_")
work_vals  = _section_vals("workshop_rating_")

if not truck_vals.empty and not sales_vals.empty and not work_vals.empty:
    def flat_and_value(vals: pd.DataFrame):
        answered = vals.notna().sum(axis=1)
        flat = (answered >= 2) & (vals.nunique(axis=1) == 1)
        const_val = vals.bfill(axis=1).iloc[:, 0]
        const_val[~flat] = np.nan
        return flat, const_val

    t_flat, t_v = flat_and_value(truck_vals)
    s_flat, s_v = flat_and_value(sales_vals)
    w_flat, w_v = flat_and_value(work_vals)

    all_flat_same = t_flat & s_flat & w_flat & (t_v == s_v) & (t_v == w_v)
    res.loc[all_flat_same, "CHK_straightliner_all_F2F4F6"] = "Straight-liner — same score across truck, sales & delivery, and workshop"

# 24) Consider (B3a) vs E4 for quota_make (kept, light sanity)
if "quota_make" in df.columns and COL.get("E4_choose_brand") in df.columns and brand_cols["consider"]:
    qm = df["quota_make"].apply(parse_brand_id)
    e4_hi = df[COL["E4_choose_brand"]].apply(lambda x: in_vals(x, [4, 5]))
    mask = []
    for i, b in qm.items():
        if not b:
            mask.append(False); continue
        col = brand_cols["consider"].get(b)
        val = boolish(df.at[i, col]) if (col and col in df.columns) else False
        mask.append(val and not e4_hi.iat[i])
    res.loc[mask, "CHK_B3a_vs_E4_quota"] = "Consider quota make but low likelihood to choose (E4)"

# 37) E1 vs F1 — change spec
if COL["E1_overall"] in df.columns and "overall_rating_truck" in df.columns:
    e1 = to_num(df[COL["E1_overall"]])
    f1 = to_num(df["overall_rating_truck"])
    res["CHK_E1_F1"] = "OK"
    bad1 = f1.isin([1, 2]) & e1.isin([4, 5])
    bad2 = f1.isin([4, 5]) & e1.isin([1, 2])
    res.loc[bad1, "CHK_E1_F1"] = "F1 low (1–2) but E1 high (4–5)"
    res.loc[bad2, "CHK_E1_F1"] = "F1 high (4–5) but E1 low (1–2)"

# S4a1 vs A3 — MAPPED LOGIC (calendar-year bucket → years-ago)
A3_pre = "last_purchase_b"               # years ago per brand (0..99; 9/99/≥90 = never)
A4_pre = "last_workshop_visit_b"
A4b_pre= "last_workshop2_visit_b"

def find_yearago_block(prefix: str) -> Dict[str, str]:
    m: Dict[str, str] = {}
    for c in df.columns:
        if c.startswith(prefix):
            mm = BRAND_SUFFIX_RE.search(c)
            if mm:
                m[mm.group("brand")] = c
    return m

A3 = find_yearago_block(A3_pre)
A4 = find_yearago_block(A4_pre)
A4b= find_yearago_block(A4b_pre)

if S["LastPurchaseHD_cat"] in df.columns and A3:
    s4 = to_num(df[S["LastPurchaseHD_cat"]])  # codes 1..9
    a3_df = pd.DataFrame({b: to_num(df[c]) for b, c in A3.items()})

    a3_is_never = a3_df.eq(9) | a3_df.ge(90)
    a3_is_finite = a3_df.notna() & ~a3_is_never

    ok = pd.Series(True, index=df.index)

    # A) S4a1 = 1..6 → expect exact A3 years-ago match: 0..5
    for code, yrs_ago in S4_TO_YEARS_AGO.items():
        mask = s4.eq(code)
        if mask.any():
            match_any = a3_df.eq(yrs_ago).any(axis=1)
            ok[mask] = ok[mask] & match_any

    # B) S4a1 = 7 (“2019 or earlier”) → any A3 ≥ 6
    mask_2019_or_earlier = s4.eq(7)
    if mask_2019_or_earlier.any():
        any_ge6 = a3_df.ge(6).any(axis=1)
        ok[mask_2019_or_earlier] = ok[mask_2019_or_earlier] & any_ge6

    # C) S4a1 = 8 (“Don’t know”) → skip (leave ok=True)

    # D) S4a1 = 9 (“Never”) → no A3 should show a purchase
    mask_never = s4.eq(9)
    if mask_never.any():
        any_claimed_purchase = a3_is_finite.any(axis=1)
        ok[mask_never] = ok[mask_never] & ~any_claimed_purchase

    res["CHK_S4a1_vs_A3_mapped"] = "OK"
    bad = ~ok & s4.notna()
    msgs = []
    for i in df.index[bad]:
        code = int(s4.iat[i]) if not pd.isna(s4.iat[i]) else None
        if code in S4_TO_YEAR:
            yrs_ago = S4_TO_YEARS_AGO[code]
            year_txt = S4_TO_YEAR[code]
            msgs.append((i, f"S4a1 says {year_txt}; no A3 brand at {yrs_ago} year(s) ago"))
        elif code == 7:
            msgs.append((i, "S4a1 says 2019 or earlier; no A3 ≥6 years ago"))
        elif code == 9:
            msgs.append((i, "S4a1 says never; but some A3 show a purchase"))
        else:
            msgs.append((i, "S4a1 vs A3 mismatch"))
    for i, m in msgs:
        res.loc[i, "CHK_S4a1_vs_A3_mapped"] = m

# Usage but never used authorised workshop (A4 = 99) — kept
if A4 and brand_cols["usage"]:
    for b in brands:
        ucol = getb(brand_cols["usage"], b)
        wcol = A4.get(b)
        if not ucol or not wcol: continue
        used = df[ucol].apply(boolish)
        never_ws = to_num(df[wcol]) == 99
        res.loc[used & never_ws, f"CHK_A4_never_b{b}"] = "Used brand but never used workshop"

# A4 vs A4b gap (>3y), excluding 99 — kept
for b in brands:
    s = A4.get(b); p = A4b.get(b)
    if not s or not p: continue
    sv = to_num(df[s]); pv = to_num(df[p])
    mask = sv.notna() & pv.notna() & (sv != 99) & (pv != 99) & ((sv - pv).abs() > 3)
    res.loc[mask, f"CHK_A4_vs_A4b_gap_b{b}"] = ">3y gap between service and parts visits"

# E1/E4/E4c vs B2 alignment for quota make (updated cutoffs: B2 high≥3, low≤2)
if "quota_make" in df.columns:
    qm = df["quota_make"].apply(parse_brand_id)
    for i, b in qm.items():
        if not b: continue
        b2col = brand_cols["impression"].get(b)
        if not b2col: continue
        b2 = to_num(df.at[i, b2col])
        if pd.isna(b2): continue

        # E1
        e1_col = COL.get("E1_overall")
        if e1_col and e1_col in df.columns:
            e1 = to_num(df.at[i, e1_col])
            if (b2 >= B2_HIGH_MIN) and in_vals(e1, [1, 2]):
                res.loc[i, "CHK_E1_low_vs_B2_high"] = "E1 low but B2 high"
            if (b2 <= B2_LOW_MAX) and in_vals(e1, [4, 5]):
                res.loc[i, "CHK_E1_high_vs_B2_low"] = "E1 high but B2 low"

        # E4
        e4_col = COL.get("E4_choose_brand")
        if e4_col and e4_col in df.columns:
            e4 = to_num(df.at[i, e4_col])
            if (b2 >= B2_HIGH_MIN) and in_vals(e4, [1, 2]):
                res.loc[i, "CHK_E4_low_vs_B2_high"] = "E4 low but B2 high"
            if (b2 <= B2_LOW_MAX) and in_vals(e4, [4, 5]):
                res.loc[i, "CHK_E4_high_vs_B2_low"] = "E4 high but B2 low"

        # E4c
        e4c_col = COL.get("E4c_pref_strength")
        if e4c_col and e4c_col in df.columns:
            e4c = to_num(df.at[i, e4c_col])
            if (b2 >= B2_HIGH_MIN) and in_vals(e4c, [1, 2]):
                res.loc[i, "CHK_E4c_low_vs_B2_high"] = "E4c low but B2 high"
            if (b2 <= B2_LOW_MAX) and in_vals(e4c, [4, 5]):
                res.loc[i, "CHK_E4c_high_vs_B2_low"] = "E4c high but B2 low"

# -------------------------------------------------------------------
# Apply optional custom rules JSON
# -------------------------------------------------------------------
try:
    res = apply_custom_rules(df, res, rules)
except Exception:
    st.warning("Custom rules JSON present but could not be applied. Check the Rule Builder export.")

# -------------------------------------------------------------------
# KPI: share of A2b (main brand) that is also in B3a (considered)
# -------------------------------------------------------------------
if COL["main_brand"] in df.columns and brand_cols["consider"]:
    mb = df[COL["main_brand"]].apply(parse_brand_id)
    flags = []; skipped_unparsed = 0; skipped_no_col = 0
    for i, b in mb.items():
        if not b:
            flags.append(np.nan); skipped_unparsed += 1; continue
        col = brand_cols["consider"].get(b)
        if not col or col not in df.columns:
            flags.append(np.nan); skipped_no_col += 1; continue
        mark = consider_mark(df.at[i, col])
        flags.append(1.0 if mark is True else (0.0 if mark is False else np.nan))

    flags = pd.Series(flags, index=df.index, dtype="float")
    valid_raw = flags.dropna()
    n_raw = int(valid_raw.count()); total_rows = len(df)

    dedup_applied = False
    valid = valid_raw.copy()
    if "respid" in df.columns and df["respid"].nunique(dropna=False) < len(df):
        dedup_applied = True
        gkey = df.loc[valid_raw.index, "respid"]
        mask = gkey.notna()
        grouped = valid_raw[mask].groupby(gkey[mask]).max()
        valid = pd.concat([grouped, valid_raw[~mask]], axis=0)

    n_eval = int(valid.count())
    if n_eval > 0:
        share = float(valid.mean())
        msg = (
            f"A2b main brand is in B3a consider: {share:.1%} "
            f"(n={n_eval} evaluable{' after de-dup to unique respid' if dedup_applied else ''}; "
            f"raw rows evaluable={n_raw}; total rows={total_rows}). "
            f"Skipped (pre de-dup): {skipped_unparsed} unparsed A2b, {skipped_no_col} with no matching B3a column."
        )
        st.info(msg)
    else:
        st.warning("A2b→B3a KPI is N/A — no evaluable rows (A2b couldn’t be parsed, no matching B3a column, or B3a blank).")

# -------------------------------------------------------------------
# Output — issues digest + detailed list
# -------------------------------------------------------------------
chk_cols = [c for c in res.columns if c.startswith("CHK_")]

def _brand_name_from_chk(colname: str) -> str | None:
    m = re.search(r"_b(\d+)", colname)
    if not m: return None
    code = m.group(1)
    return CODE_TO_BRAND.get(code, f"Brand code {code} (unmapped)")

FRIENDLY = {
    "S3a1-3 ≠ S3": "Truck subtypes don’t add up to total (S3).",
    "Operation range unmatched for industry": "Operation range looks unmatched for this industry.",
    "High performance but B2 very low": "High performance score but overall impression (B2) is 1–2.",
    "B2 should be 4/5": "Overall impression (B2) is low for that brand.",
    "B2 should be 4/5 for preferred brand": "Overall impression (B2) is low for the preferred brand.",
    "No brand with purchase ≤5y despite S4a1 recent": "S4 says recent purchase, but no brand purchased in last 5 years.",
    ">3y gap between service and parts visits": "Service vs. parts visit dates differ by >3 years.",
    "Too many brands vs fleet size": "Awareness count seems high vs. fleet size.",
    "Main brand not in A1a": "Main brand not listed in unaided awareness.",
    "Main brand not in A2a": "Main brand not listed in usage.",
    "Used brand but never used workshop": "Used brand but never used authorized workshop.",
     "Aware but familiarity too low": "Aware but familiarity too low.",
    "F1 low (1–2) but E1 high (4–5)": "Truck rating low but overall satisfaction high.",
    "F1 high (4–5) but E1 low (1–2)": "Truck rating high but overall satisfaction low.",
    "E1 high but B2 low":  "Overall satisfaction (E1) high but overall impression (B2) low.",
    "E1 low but B2 high":  "Overall satisfaction (E1) low but overall impression (B2) high.",
    "E4 high but B2 low":  "Likelihood to choose (E4) high but overall impression (B2) low.",
    "E4 low but B2 high":  "Likelihood to choose (E4) low but overall impression (B2) high.",
    "E4c high but B2 low": "Preference strength (E4c) high but overall impression (B2) low.",
    "E4c low but B2 high": "Preference strength (E4c) low but overall impression (B2) high.",
    "Familiarity (B1) ≤ 2 despite having B2 rating": "Familiarity very low for a rated brand.",
    "Straight-liner — same score across truck, sales & delivery, and workshop": "Same score used in all three sections.",
    f"Expect ≥{MIN_CLOSE_B2_EQ5}": f"Closeness is lower than expected (target ≥{MIN_CLOSE_B2_EQ5}).",
    f"Expect ≥{MIN_CLOSE_LO}": f"Closeness is a bit low (target ≥{MIN_CLOSE_LO}).",
    "Expect 8–10 when B2=5 & considered": "Closeness should be 8–10 when B2=5 and brand is considered.",
}

def _human_list(prefix: str, items: list[str], show=4) -> str:
    uniq = sorted(set(items))
    if len(uniq) <= show:
        return f"{prefix} {', '.join(uniq)}"
    return f"{prefix} {len(uniq)} brands incl. {', '.join(uniq[:show])}"

def digest_row(r: pd.Series) -> str:
    closeness_hi_brands, closeness_lo_brands = [], []
    b2low_brands, misalign_brands = [], []
    generic_bits = []
    for c in chk_cols:
        v = r.get(c, "")
        if not isinstance(v, str) or v in ("", "OK") or pd.isna(v): continue
        brand = _brand_name_from_chk(c)

        if v == f"Expect ≥{MIN_CLOSE_B2_EQ5}" and brand:
            closeness_hi_brands.append(brand); continue
        if v == f"Expect ≥{MIN_CLOSE_LO}" and brand:
            closeness_lo_brands.append(brand); continue

        if v == "High performance but B2 very low" and brand:
            misalign_brands.append(brand); continue
        if v in ("B2 should be 4/5", "B2 should be 4/5 for preferred brand") and brand:
            b2low_brands.append(brand); continue

        friendly = FRIENDLY.get(v, v)
        generic_bits.append(friendly if not brand else f"{brand}: {friendly}")

    parts = []
    if closeness_hi_brands:
        parts.append(_human_list(f"Closeness is lower than expected (target ≥{MIN_CLOSE_B2_EQ5}) for:", closeness_hi_brands))
    if closeness_lo_brands:
        parts.append(_human_list(f"Closeness is a bit low (target ≥{MIN_CLOSE_LO}) for:", closeness_lo_brands))
    if b2low_brands:
        parts.append(_human_list("Overall impression (B2) is low for:", b2low_brands))
    if misalign_brands:
        parts.append(_human_list("High performance but very low impression for:", misalign_brands))

    parts += generic_bits
    return " | ".join(parts) if parts else "Consistent"

res["Consistency_Check"] = res.apply(digest_row, axis=1)
res["Consistency_Check"] = (
    res["Consistency_Check"].astype(str)
    .str.replace("â‰ ", "≠", regex=False)
    .str.replace("â‰≥", "≥", regex=False)
    .str.replace("â‰¥", "≥", regex=False)
)

# Count issues per row
res["Issue_Count"] = res["Consistency_Check"].apply(
    lambda s: 0 if (s == "Consistent" or pd.isna(s)) else len([x for x in s.split(" | ") if x.strip()])
)

# Context cols
key_cols = [c for c in [
    "respid","id","company_position","n_heavy_duty_trucks","quota_make",
    "main_brand","preference","overall_satisfaction","likelihood_choose_brand","preference_strength",
] if c in res.columns]

# Issues-only digest table
issues_only = res[res["Consistency_Check"] != "Consistent"].copy()
digest_view = (issues_only[key_cols + ["Issue_Count","Consistency_Check"]] if key_cols
               else issues_only[["Issue_Count","Consistency_Check"]])

# Filters & summary
st.subheader("Issues Digest")
issues_exploded = (
    digest_view["Consistency_Check"].astype(str).str.split(r"\s\|\s", expand=False).explode().dropna().str.strip()
)
issue_types = (
    issues_exploded.apply(lambda t: t.split(": ", 1)[1] if ": " in t else t)
    .value_counts().rename_axis("Issue").to_frame("Count").reset_index()
)

col1, col2, col3 = st.columns([2,2,1])
with col1:
    keyword = st.text_input("Search (brand, rule text, respid/id)", "")
with col2:
    choose_types = st.multiselect("Filter by issue type(s)", options=issue_types["Issue"].tolist(), default=[])
with col3:
    min_flags = st.number_input("Min #flags", min_value=1, max_value=int(digest_view["Issue_Count"].max() or 1), value=1)

filtered_digest = digest_view.copy()
if keyword:
    pat = re.escape(keyword)
    filtered_digest = filtered_digest[
        filtered_digest.apply(lambda r: r.astype(str).str.contains(pat, case=False, na=False).any(), axis=1)
    ]
if choose_types:
    def row_has_any_type(s: str) -> bool:
        toks = [t.strip() for t in str(s).split(" | ") if t.strip()]
        types_in_row = [t.split(": ", 1)[1] if ": " in t else t for t in toks]
        return any(t in types_in_row for t in choose_types)
    filtered_digest = filtered_digest[filtered_digest["Consistency_Check"].apply(row_has_any_type)]
if min_flags > 1:
    filtered_digest = filtered_digest[filtered_digest["Issue_Count"] >= min_flags]

st.dataframe(filtered_digest, use_container_width=True)

# Detailed list (one row per flag)
rows = []
if len(issues_only):
    for idx, r in issues_only.iterrows():
        for c in chk_cols:
            v = r.get(c, "")
            if isinstance(v, str) and v not in ("", "OK") and not pd.isna(v):
                brand = _brand_name_from_chk(c)
                rows.append({
                    "row_index": idx,
                    "respid": r.get("respid", np.nan),
                    "id": r.get("id", np.nan),
                    "main_brand": r.get(COL["main_brand"], np.nan),
                    "quota_make": r.get("quota_make", np.nan),
                    "rule": c,
                    "brand": brand,
                    "message": FRIENDLY.get(v, v),
                })
issues_long = (pd.DataFrame(rows)
               if rows
               else pd.DataFrame(columns=["row_index","respid","id","rule","brand","message"]))
if not issues_long.empty:
    issues_long["message"] = (
        issues_long["message"].astype(str)
        .str.replace("â‰ ", "≠", regex=False)
        .str.replace("â‰≥", "≥", regex=False)
        .str.replace("â‰¥", "≥", regex=False)
    )

st.subheader("Issues only — Detailed List (one row per flag)")
st.dataframe(issues_long, use_container_width=True)

# Optional: full table
if show_full:
    st.subheader("Full results table")
    st.dataframe(res, use_container_width=True)

# Legend
with st.expander("Legend — what the Flags mean"):
    st.markdown(f"""
- **Truck subtypes don’t add up to total (S3):** Counts in S3a1–3 should equal S3 (when any subtype is answered).
- **Closeness expectations:**  
  • **B2=5 (or B2=5 & considered):** expect **≥{MIN_CLOSE_B2_EQ5}** (8–10).  
  • **B2=4** or **preferred/considered**: expect **≥{MIN_CLOSE_LO}**.
- **B3a (consider) ⇒ B2≥4:** Considering a brand but B2 is low.
- **High performance but B2 very low:** Cfunc≥{CFUNC_HIGH} with B2 in 1–2.
- **Straight-liner across F2/F4/F6:** Same score across all questions in all 3 sections.
- **S4a1 vs A3 (mapped):** 2025→A3=0, 2024→1, …, 2020→5; “2019 or earlier” ⇒ A3≥6; “Never” ⇒ no A3 purchase; “Don’t know” is skipped.
- **E vs B2 alignment:** E1/E4/E4c ≤2 with B2≥{B2_HIGH_MIN}, or E1/E4/E4c ≥4 with B2≤{B2_LOW_MAX}.
- **F1 vs E1 consistency:** If F1 is 1–2 then E1 shouldn’t be 4–5, and vice versa.
- **Awareness vs fleet size:** Awareness count looks high vs. fleet size (n_heavy_duty_trucks).
    """)

# -------------------------------------------------------------------
# Rule # mapping and cell targeting for highlighting
# -------------------------------------------------------------------
_BRAND_IN_CHK = re.compile(r"_b(\d+)$")

def _rule_id_for_flag(chk_col: str, val: str) -> int | None:
    # A1a total: decide Rule 2 vs Rule 3 by message text
    if chk_col == "CHK_A1a_total_flag":
        if isinstance(val, str) and "Too many brands vs fleet size" in val:
            return 3
        if isinstance(val, str) and val.startswith(">"):
            return 2
        return None

    patterns = [
        (r"^CHK_B2_for_consider_b\d+$", 13),
        (r"^CHK_B2_for_pref_b\d+$",     14),
        (r"^CHK_Cclose_B2eq5.*_b\d+$",  4),
        (r"^CHK_Cclose_B2eq4_b\d+$",    5),
        (r"^CHK_Cclose_pref_or_cons_b\d+$", 5),
        (r"^CHK_Cfunc_vs_B2_b\d+$",     7),
        (r"^CHK_A4_never_b\d+$",        11),
        (r"^CHK_A4_vs_A4b_gap_b\d+$",   21),
        (r"^CHK_B1_low_given_B2_b\d+$", 23),
        (r"^CHK_straightliner_all_F2F4F6$", 22),
        (r"^CHK_Aware_B1_low_b\d+$", 24),
        (r"^CHK_A2b_in_A1a$",           10),
        (r"^CHK_A2b_in_A2a$",           12),
        (r"^CHK_B3a_vs_E4_quota$",      6),
        (r"^CHK_E1_F1$",                17),
        (r"^CHK_E1_high_vs_B2_low$",    15),
        (r"^CHK_E1_low_vs_B2_high$",    16),
        (r"^CHK_E4_high_vs_B2_low$",    9),
        (r"^CHK_E4_low_vs_B2_high$",    8),
        (r"^CHK_E4c_high_vs_B2_low$",   19),
        (r"^CHK_E4c_low_vs_B2_high$",   18),
        (r"^CHK_S4a1_vs_A3_mapped$",    20),
        (r"^CHK_G2_vs_G1$",             25),
        (r"^CHK_S3a_sum$",              None),  # optional to color or not
    ]
    for pat, rid in patterns:
        if re.fullmatch(pat, chk_col):
            return rid
    return None

def _targets_for_flag(chk_col: str, row_i: int) -> list[str]:
    m = _BRAND_IN_CHK.search(chk_col)
    brand = m.group(1) if m else None

    def bcol(block: str, b: str | None) -> str | None:
        return brand_cols.get(block, {}).get(b) if (b and brand_cols.get(block)) else None

    targets: list[str] = []

    # Brand-level rules
    if chk_col.startswith("CHK_B2_for_consider_b"):
        targets += [bcol("impression", brand), bcol("consider", brand)]
    elif chk_col.startswith("CHK_B2_for_pref_b"):
        targets += [bcol("impression", brand)]
    elif chk_col.startswith("CHK_Cclose_"):
        targets += [bcol("close", brand)]
    elif chk_col.startswith("CHK_Cfunc_vs_B2_b"):
        targets += [bcol("cfunc", brand), bcol("impression", brand)]
    elif chk_col.startswith("CHK_B1_low_given_B2_b"):
        targets += [bcol("familiarity", brand)]
    elif chk_col.startswith("CHK_A4_never_b"):
        targets += [bcol("usage", brand)]
        ws = f"last_workshop_visit_b{brand}"
        if ws in res.columns: targets += [ws]
    elif chk_col.startswith("CHK_Aware_B1_low_b"):
        targets += [bcol("familiarity", brand)]  # (optionally also add bcol("awareness", brand))
    elif chk_col.startswith("CHK_A4_vs_A4b_gap_b"):
        s = f"last_workshop_visit_b{brand}"
        p = f"last_workshop2_visit_b{brand}"
        if s in res.columns: targets += [s]
        if p in res.columns: targets += [p]

    # Non-brand rules
    elif chk_col == "CHK_A2b_in_A1a" or chk_col == "CHK_A2b_in_A2a":
        if COL["main_brand"] in res.columns: targets += [COL["main_brand"]]
    elif chk_col == "CHK_B3a_vs_E4_quota":
        if COL.get("E4_choose_brand") in res.columns: targets += [COL["E4_choose_brand"]]
    elif chk_col == "CHK_E1_F1":
        if COL.get("E1_overall") in res.columns: targets += [COL["E1_overall"]]
        if "overall_rating_truck" in res.columns: targets += ["overall_rating_truck"]
    elif chk_col in {"CHK_E1_high_vs_B2_low","CHK_E1_low_vs_B2_high"}:
        if COL.get("E1_overall") in res.columns: targets += [COL["E1_overall"]]
    elif chk_col in {"CHK_E4_high_vs_B2_low","CHK_E4_low_vs_B2_high"}:
        if COL.get("E4_choose_brand") in res.columns: targets += [COL["E4_choose_brand"]]
    elif chk_col in {"CHK_E4c_high_vs_B2_low","CHK_E4c_low_vs_B2_high"}:
        if COL.get("E4c_pref_strength") in res.columns: targets += [COL["E4c_pref_strength"]]
    elif chk_col == "CHK_S4a1_vs_A3_mapped":
        if S["LastPurchaseHD_cat"] in res.columns: targets += [S["LastPurchaseHD_cat"]]
    elif chk_col == "CHK_S3a_sum":
        for k in [S["HD_count"], S["Tractors"], S["Rigids"], S["Tippers"]]:
            if k in res.columns: targets += [k]
    elif chk_col == "CHK_A1a_total_flag":
        for k in ["CHK_A1a_total_flag","CHK_A1a_total_count"]:
            if k in res.columns: targets += [k]
    elif chk_col == "CHK_straightliner_all_F2F4F6":
        targets += [c for c in res.columns if c.startswith(("truck_rating_","salesdelivery_rating_","workshop_rating_"))]

    return [c for c in targets if c and (c in res.columns)]

# -------------------------------------------------------------------
# Excel + CSV exports
# -------------------------------------------------------------------
def _autofit(ws, data_df):
    for i, col in enumerate(data_df.columns):
        try:
            max_len = int(max(data_df[col].astype(str).map(len).max(), len(col))) + 2
        except ValueError:
            max_len = len(col) + 2
        ws.set_column(i, i, min(max_len, 60))
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, max(len(data_df), 1), max(len(data_df.columns) - 1, 0))

def build_excel(digest_df: pd.DataFrame, detail_df: pd.DataFrame, full_df: pd.DataFrame | None = None) -> BytesIO | None:
    try:
        import xlsxwriter  # noqa: F401
    except Exception:
        st.error("Excel export needs the 'xlsxwriter' package.")
        return None

    if not digest_df.empty:
        exploded = (
            digest_df["Consistency_Check"].astype(str).str.split(r"\s\|\s", expand=False).explode().dropna().str.strip()
        )
        summary_types = (
            exploded.apply(lambda t: t.split(": ", 1)[1] if ": " in t else t)
            .value_counts().rename_axis("Issue").to_frame("Count").reset_index()
        )
    else:
        summary_types = pd.DataFrame(columns=["Issue","Count"])

    out = BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
        digest_df.to_excel(writer, index=False, sheet_name="Issues")
        summary_types.to_excel(writer, index=False, sheet_name="Summary")
        detail_df.to_excel(writer, index=False, sheet_name="Detailed_Issues")
        if full_df is not None:
            full_df.to_excel(writer, index=False, sheet_name="Full_Table")

        wb = writer.book
        ws_issues  = writer.sheets["Issues"]
        ws_summary = writer.sheets["Summary"]
        ws_detail  = writer.sheets["Detailed_Issues"]
        _autofit(ws_issues, digest_df)
        _autofit(ws_summary, summary_types)
        _autofit(ws_detail, detail_df)
        if full_df is not None:
            _autofit(writer.sheets["Full_Table"], full_df)

        if "Consistency_Check" in digest_df.columns:
            from xlsxwriter.utility import xl_col_to_name
            cc_idx = list(digest_df.columns).index("Consistency_Check")
            nrows = max(len(digest_df), 1)
            col_letter = xl_col_to_name(cc_idx)
            rng = f"{col_letter}2:{col_letter}{nrows+1}"
            bold = wb.add_format({"bold": True})
            wrap = wb.add_format({"text_wrap": True})
            ws_issues.set_column(cc_idx, cc_idx, 80, wrap)
            for token in [
                "High performance but very low impression",
                "Closeness is lower than expected",
                "Closeness is a bit low",
                "Straight-liner",
                "F1 low (1–2) but E1 high (4–5)",
                "F1 high (4–5) but E1 low (1–2)",
                "Awareness count seems high vs. fleet size",
            ]:
                ws_issues.conditional_format(
                    rng, {"type": "text", "criteria": "containing", "value": token, "format": bold}
                )
    out.seek(0)
    return out

def build_issues_only_excel(detail_df: pd.DataFrame) -> BytesIO | None:
    try:
        import xlsxwriter  # noqa: F401
    except Exception:
        st.error("Excel export needs the 'xlsxwriter' package.")
        return None

    if not detail_df.empty:
        summary_by_issue = detail_df["message"].value_counts().rename_axis("Issue").to_frame("Count").reset_index()
        summary_by_rule  = detail_df["rule"].value_counts().rename_axis("Rule").to_frame("Count").reset_index()
    else:
        summary_by_issue = pd.DataFrame(columns=["Issue","Count"])
        summary_by_rule  = pd.DataFrame(columns=["Rule","Count"])

    out = BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
        detail_df.to_excel(writer, index=False, sheet_name="Detailed_Issues")
        _autofit(writer.sheets["Detailed_Issues"], detail_df)
        summary_by_issue.to_excel(writer, index=False, sheet_name="Summary_by_Issue")
        _autofit(writer.sheets["Summary_by_Issue"], summary_by_issue)
        summary_by_rule.to_excel(writer, index=False, sheet_name="Summary_by_Rule")
        _autofit(writer.sheets["Summary_by_Rule"], summary_by_rule)
    out.seek(0)
    return out

# NEW: Full dataset with color-coded highlights + hover "Rule #"
def build_full_highlighted_excel(full_df: pd.DataFrame) -> BytesIO | None:
    try:
        import xlsxwriter  # noqa: F401
    except Exception:
        st.error("Excel export needs the 'xlsxwriter' package.")
        return None

    chk_cols_local = [c for c in full_df.columns if c.startswith("CHK_")]
    cell_rules: dict[tuple[int, str], set[int]] = {}

    # collect cells to color
    for idx, row in full_df.iterrows():
        for chk in chk_cols_local:
            val = row.get(chk, "")
            if not isinstance(val, str) or val in ("", "OK") or pd.isna(val):
                continue
            rid = _rule_id_for_flag(chk, val)
            if rid is None:
                continue
            for tgt in _targets_for_flag(chk, idx):
                cell_rules.setdefault((idx, tgt), set()).add(rid)

    out = BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
        full_df.to_excel(writer, index=False, sheet_name="Data")
        wb = writer.book
        ws = writer.sheets["Data"]

        # formats per rule
        rule_formats = {rid: wb.add_format({"bg_color": meta["color"]}) for rid, meta in RULES.items()}

        cols = list(full_df.columns)
        # apply color + comments
        for (ridx, colname), rids in cell_rules.items():
            if colname not in full_df.columns:
                continue
            cidx = cols.index(colname)
            # map DataFrame index to row number in sheet (header row offset)
            excel_r = int(list(full_df.index).index(ridx)) + 1
            excel_c = cidx
            first_rid = sorted(rids)[0]
            fmt = rule_formats.get(first_rid)
            val = full_df.at[ridx, colname]
            if pd.isna(val):
                ws.write_blank(excel_r, excel_c, None, fmt)
            else:
                ws.write(excel_r, excel_c, val, fmt)
            ws.write_comment(excel_r, excel_c, "; ".join([f"Rule {x}" for x in sorted(rids)]), {"visible": False})

        # tidy columns
        for i, col in enumerate(cols):
            try:
                max_len = int(max(full_df[col].astype(str).map(len).max(), len(col))) + 2
            except ValueError:
                max_len = len(col) + 2
            ws.set_column(i, i, min(max_len, 60))
        ws.freeze_panes(1, 0)
        ws.autofilter(0, 0, max(len(full_df), 1), max(len(cols) - 1, 0))

        # legend sheet
        legend_cols = ["Rule", "Title", "Meaning", "Color"]
        legend_rows = [[f"Rule {rid}", RULES[rid]["title"], RULES[rid]["meaning"], ""] for rid in sorted(RULES)]
        legend_df = pd.DataFrame(legend_rows, columns=legend_cols)
        legend_df.to_excel(writer, index=False, sheet_name="Legend")
        wsl = writer.sheets["Legend"]
        for i, rid in enumerate(sorted(RULES)):
            fill = wb.add_format({"bg_color": RULES[rid]["color"]})
            wsl.write(i+1, 3, "", fill)
        for i, col in enumerate(legend_cols):
            wsl.set_column(i, i, max(12, len(col) + 2))

    out.seek(0)
    return out

# CSV + Excel downloads
digest_csv = filtered_digest.to_csv(index=False).encode("utf-8-sig")
st.download_button("💾 Download issues digest (CSV)", digest_csv,
                   file_name="bcs_issues_digest.csv", mime="text/csv")

excel_bytes = build_excel(filtered_digest, issues_long, res if show_full else None)
if excel_bytes is not None:
    st.download_button("📘 Download Issues-only Excel (Summary)",
                       data=excel_bytes.getvalue(),
                       file_name="logic_issues.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

issues_only_bytes = build_issues_only_excel(issues_long)
if issues_only_bytes is not None:
    st.download_button("📘 Download Issues-only Excel (Detailed list)",
                       data=issues_only_bytes.getvalue(),
                       file_name="issues_only.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# NEW: Full dataset with color-coded highlights (Data + Legend)
full_highlight_bytes = build_full_highlighted_excel(res)
if full_highlight_bytes is not None:
    st.download_button(
        "📗 Download FULL dataset with highlights (Excel)",
        data=full_highlight_bytes.getvalue(),
        file_name="full_dataset_highlighted.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

st.markdown("---")
st.markdown(
    "**Checks included:** S3a sum to S3; A1a cap (>7) + A1a vs S3 sanity; "
    "A2b∈A1a & ∈A2a; B3a→B2≥4; C-close: B2=5→≥8; B2=4/consider/preferred→≥7; "
    "Cfunc≥7 with B2≤2; F2/F4/F6 straight-liner (all three same); S4a1 mapped vs A3 (2019-or-earlier & never handled); "
    "E1/E4/E4c vs B2 alignment (B2 high≥3 vs low≤2); F1 vs E1 consistency; "
    "usage but never workshop; A4 vs A4b >3y; G2 vs G1; quota B3a vs E4. "
    "**+ Optional custom rules JSON.**"
)

# In-app color legend (matches Excel)
st.subheader("Rule Legend (colors used in Excel)")
legend_md = []
for rid in sorted(RULES):
    swatch = f"<span style='background:{RULES[rid]['color']};display:inline-block;width:14px;height:14px;border:1px solid #999;margin-right:6px;vertical-align:middle;'></span>"
    legend_md.append(f"{swatch}<strong>Rule {rid}</strong> — {RULES[rid]['title']}: {RULES[rid]['meaning']}")
st.markdown("<br>".join(legend_md), unsafe_allow_html=True)
