# BCS Survey Logic Checker — Lite (clean, rules-json, issues-only digest)
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

st.set_page_config(page_title="BCS Survey Logic Checker", layout="wide")
st.title("📊 BCS Survey Logic Checker")
st.caption(" Identified cases with issues are displayed below: Please click Download to access files locally. Optionally: Upload a custom rules JSON.")

# ----------------------------
# Robust file reading helpers (CSV + Excel, encoding & delimiter auto)
# ----------------------------
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

    # 1) If it's actually an Excel (xlsx) by signature, or extension says xlsx/xls
    if raw.startswith(ZIP_SIGNATURES) or name.endswith((".xlsx", ".xls")):
        uploaded_file.seek(0)
        return pd.read_excel(uploaded_file)  # .xls may need xlrd if you ever use it

    # 2) CSV path with encoding attempts
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

    # 3) Last resort: permissive latin-1 read
    sep = "," if delim_override == "auto" else _norm_delim(delim_override)
    kwargs = dict(encoding="latin-1", sep=sep, engine="python")
    if skip_bad:
        kwargs["on_bad_lines"] = "skip"
    return pd.read_csv(BytesIO(raw), **kwargs)


# ----------------------------
# Upload
# ----------------------------
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
    st.subheader("Thresholds")
    a1a_cap = st.slider("Max #brands in unaided awareness (A1a)", 3, 25, 8)
    close_min = st.slider("C-close 'high intent' minimum (B2=5 or preferred ⇒ ≥this)", 6, 10, 8)
    cfunc_hi = st.slider("Cfunc (performance) 'high' threshold when B2 is low", 4, 10, 6)

    st.caption("Note: when B2=4 or a brand is merely considered, we use a softer closeness minimum of (this − 1), floored at 5.")

    st.markdown("---")
    show_full = st.checkbox("Show full result table (not just issues)", value=False)

if not data_file:
    st.info("Upload a CSV/XLSX to begin.")
    st.stop()

try:
    data_file.seek(0)  # in case Streamlit reuses the buffer
    df = read_any_table(data_file, enc_override=enc, delim_override=delim, skip_bad=skip_bad)
except Exception as e:
    st.error(f"Failed to read file: {e}")
    st.stop()

# Normalize common null tokens early so numerics parse cleanly
df.replace(
    {
        "#NULL!": np.nan,
        "NULL": np.nan,
        "null": np.nan,
        "NaN": np.nan,
        "nan": np.nan,
        "": np.nan,
        "na": np.nan,
        "N/A": np.nan,
        "n/a": np.nan,
    },
    inplace=True,
)

res = df.copy()

# Tiered C-close thresholds derived from the slider
min_hi = int(close_min)                 # for B2=5 or preferred
min_lo = max(5, int(close_min) - 1)     # for B2=4 or considered (floored at 5)

# ----------------------------
# Helpers & mappings (from your schema)
# ----------------------------
PREFIX = {
    "awareness": "unaided_aware_",       # A1a multi (0/1)
    "usage": "usage_",                   # A2a multi (0/1)
    "impression": "overall_impression_", # B2 1–5
    "consider": "consideration_",        # B3a multi (0/1)
    "close": "closeness_",               # C-close 1–10
    "cfunc": "performance_",             # Cfunc 1–10
    "familiarity": "familiarity_",       # B1 1–5
}

COL = {
    "main_brand": "main_brand",                    # A2b (preferred of A2a)
    "pref_future_single": "preference",            # B3b single pick (brand code or label)
    "E1_overall": "overall_satisfaction",          # E1 (1–5)
    "E4_choose_brand": "likelihood_choose_brand",  # E4 (1–5)
    "E4c_pref_strength": "preference_strength",    # E4c (1–5)
}

S = {
    "HD_count": "n_heavy_duty_trucks",     # S3
    "Tractors": "n_tractors",              # S3a1 (Korea only)
    "Rigids": "n_rigids",                  # S3a2 (Korea only)
    "Tippers": "n_tippers",                # S3a3 (Korea only)
    "LastPurchaseHD_cat": "last_purchase_hdt",   # S4a1 coded 1..9
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

# helpers
def to_num(x):
    return pd.to_numeric(x, errors="coerce")


def boolish(x) -> bool:
    """
    Robust 0/1 (or yes/no/true/false) detector:
    - Treat any non-zero numeric (int/float or numeric string like '1', '1.0') as True
    - 0 or '0' as False
    - #NULL!/NaN/empty/None as False
    - 'yes','y','true','t' as True; 'no','n','false','f' as False
    """
    if pd.isna(x):
        return False
    if isinstance(x, (int, np.integer, float, np.floating)):
        try:
            return float(x) != 0.0
        except Exception:
            return False
    s = str(x).strip().lower()
    if s in {"", "nan", "none", "null", "#null!", "na", "n/a"}:
        return False
    if s in {"true", "t", "yes", "y"}:
        return True
    if s in {"false", "f", "no", "n"}:
        return False
    try:
        return float(s) != 0.0
    except Exception:
        return s == "1"


def in_vals(x, allowed: List[int]) -> bool:
    try:
        return int(float(x)) in allowed
    except Exception:
        return False


def parse_brand_id(val) -> str | None:
    """Return brand code as string (e.g., '38') from ints/floats like 38.0,
    tokens like 'b38', or from label names via BRAND_NAME_TO_CODE."""
    if val is None:
        return None
    # numeric types
    if isinstance(val, (int, np.integer)):
        return str(int(val))
    if isinstance(val, float):
        if np.isnan(val):
            return None
        if float(val).is_integer():
            return str(int(val))
        return None  # non-integer float not a valid brand code

    s = str(val).strip()
    if not s:
        return None
    sl = s.lower()
    if sl in {"nan", "none", "null", "#null!", "na"}:
        return None

    # '38' or '38.0'
    m = re.fullmatch(r"(\d+)(?:\.0+)?", sl)
    if m:
        return m.group(1)

    # 'b38' / 'b 38'
    m = re.search(r"\bb\s*(\d+)\b$", sl)
    if m:
        return m.group(1)

    # Try label mapping (normalize)
    sl2 = re.sub(r"\s+", " ", sl.replace("-", " ").replace("/", " / ")).strip()
    if sl2 in BRAND_NAME_TO_CODE:
        return BRAND_NAME_TO_CODE[sl2]

    # Last resort: first number found
    m = re.search(r"(\d+)", sl)
    if m:
        return m.group(1)

    return None


def consider_mark(x):
    """Return True/False for 1/0; NaN/#NULL!/empty-> np.nan. Anything else -> False."""
    if x is None:
        return np.nan
    if pd.isna(x):
        return np.nan
    s = str(x).strip().lower()
    if s in {"", "nan", "none", "null", "#null!", "na", "n/a"}:
        return np.nan
    if s in {"1", "true", "t", "yes", "y"}:
        return True
    if s in {"0", "false", "f", "no", "n"}:
        return False
    try:
        v = float(s)
        if np.isnan(v):
            return np.nan
        return int(v) == 1
    except Exception:
        return False


# ----- Optional custom rules JSON (from Rule Builder) -----
def apply_custom_rules(df: pd.DataFrame, res: pd.DataFrame, rules: dict | None) -> pd.DataFrame:
    """Apply generic custom rules exported from Rule Builder to augment built-in checks.
    Supported types:
      - equals: {"cols":["A","B"]}
      - implies_values: if X∈{…} ⇒ Y∈{…}
      - brand_consider_implies_impression: auto-iterate across brand ids for prefixes
    """
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
                if not col.startswith(consider_prefix):
                    continue
                m = re.search(r"_b(\d+)$", col)
                if not m:
                    continue
                bid = m.group("brand")
                tgt = f"{impression_prefix}b{bid}"
                if tgt not in df.columns:
                    continue
                name_bid = f"{name}_b{bid}"
                bad = (to_num(df[col]) == 1) & ~to_num(df[tgt]).isin(allowed)
                res[name_bid] = "OK"
                res.loc[bad, name_bid] = msg
    return res


# ----------------------------
# Checks (Lite set + extras)
# ----------------------------
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

# 3) A1a total sanity (awareness) — merged cap + S3 sanity (single flag)
aw_cols = list(brand_cols["awareness"].values())
if aw_cols:
    sel_aw = df[aw_cols].applymap(boolish)
    count_aw = sel_aw.sum(axis=1)
    res["CHK_A1a_total_count"] = count_aw
    res["CHK_A1a_total_flag"] = "OK"

    # Fixed cap (from slider)
    over_cap = count_aw > a1a_cap
    res.loc[over_cap, "CHK_A1a_total_flag"] = f">{a1a_cap} brands"

    # Escalate to fleet-size message where applicable (overrides fixed-cap msg)
    if S["HD_count"] in df.columns:
        s3 = to_num(df[S["HD_count"]]).fillna(0)
        allowed = np.maximum(a1a_cap, np.minimum(25, s3 + 2))  # tolerance by fleet size
        over_s3 = count_aw > allowed
        res.loc[over_s3, "CHK_A1a_total_flag"] = "Too many brands vs fleet size"


# 4) Main make (A2b) must be in A1a and A2a
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

# 7) If a brand is used (A2a), B2 overall impression should be 4/5
if brand_cols["usage"] and brand_cols["impression"]:
    for b in brands:
        ucol = getb(brand_cols["usage"], b)
        icol = getb(brand_cols["impression"], b)
        if not ucol or not icol:
            continue
        used = df[ucol].apply(boolish)
        bad = used & ~df[icol].apply(lambda x: in_vals(x, [4, 5]))
        name = f"CHK_B2_for_used_b{b}"
        res[name] = "OK"
        res.loc[bad, name] = "B2 should be 4/5"

# 8) If considering (B3a), B2 should be 4/5
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

# 8b) B1 familiarity checks: used ⇒ B1≥4; aware ⇒ B1 not 1/2
if brand_cols.get("familiarity"):
    for b in brands:
        fcol = getb(brand_cols["familiarity"], b)
        acol = getb(brand_cols["awareness"], b)
        ucol = getb(brand_cols["usage"], b)
        if fcol and ucol and (fcol in df.columns):
            used = df[ucol].apply(boolish) if ucol else pd.Series(False, index=df.index)
            bad = used & ~df[fcol].apply(lambda x: in_vals(x, [4, 5]))
            res.loc[bad, f"CHK_B1_low_for_used_b{b}"] = "Used brand but familiarity <4"
        if fcol and acol and (fcol in df.columns):
            aware = df[acol].apply(boolish) if acol else pd.Series(False, index=df.index)
            bad2 = aware & df[fcol].apply(lambda x: in_vals(x, [1, 2]))
            res.loc[bad2, f"CHK_B1_low_for_aware_b{b}"] = "Aware but familiarity too low"

# 9) If preferred for next purchase (B3b single), B2 for that brand should be 4/5
if COL["pref_future_single"] in df.columns and brand_cols["impression"]:
    pref = df[COL["pref_future_single"]].apply(parse_brand_id)
    for idx, b in pref.items():
        if b is None:
            continue
        icol = getb(brand_cols["impression"], b)
        if not icol:
            continue
        name = f"CHK_B2_for_pref_b{b}"
        res[name] = res.get(name, "OK")
        if not in_vals(df.at[idx, icol], [4, 5]):
            res.loc[idx, name] = "B2 should be 4/5 for preferred brand"

# 10) C-close expectations: tiered thresholds (no double-flagging)
if brand_cols["close"]:
    pref_series = None
    if COL.get("pref_future_single") in df.columns:
        pref_series = df[COL["pref_future_single"]].apply(parse_brand_id)

    for b in brands:
        ccol = getb(brand_cols["close"], b)
        if not ccol:
            continue
        icol = getb(brand_cols["impression"], b)
        cns  = getb(brand_cols["consider"], b)

        close = to_num(df[ccol])
        b2    = to_num(df[icol]) if icol else pd.Series(np.nan, index=df.index)
        considered = df[cns].apply(boolish) if cns else pd.Series(False, index=df.index)
        preferred  = (pref_series == str(b)) if pref_series is not None else pd.Series(False, index=df.index)

        need_lo = (b2 == 4) | considered
        need_hi = (b2 == 5) | preferred

        # First, high bar; then soft bar for the rows that didn't already get flagged by high bar
        mask_hi = need_hi & ~close.isin(range(min_hi, 11))
        res.loc[mask_hi, f"CHK_Cclose_low_hi_b{b}"] = f"Expect ≥{min_hi}"

        mask_lo = (~mask_hi) & need_lo & ~close.isin(range(min_lo, 11))
        res.loc[mask_lo, f"CHK_Cclose_low_lo_b{b}"] = f"Expect ≥{min_lo}"

# 11) Cfunc vs B2 alignment (avoid high cfunc when B2 low)
if brand_cols["cfunc"] and brand_cols["impression"]:
    for b in brands:
        cf = getb(brand_cols["cfunc"], b)
        icol = getb(brand_cols["impression"], b)
        if not cf or not icol:
            continue
        def misaligned(r):
            b2v = to_num(r[icol])
            cfv = to_num(r[cf])
            if pd.isna(b2v) or pd.isna(cfv):
                return False
            return (b2v < 4) and (cfv >= cfunc_hi)
        bad = df.apply(misaligned, axis=1)
        name = f"CHK_Cfunc_vs_B2_b{b}"
        res[name] = "OK"
        res.loc[bad, name] = "Misaligned"

# 23) Straight-liners: truck_rating_*, salesdelivery_rating_*, workshop_rating_*
section_labels = {
    "truck_rating_": "Same score across all truck-rating questions",
    "salesdelivery_rating_": "Same score across all sales & delivery questions",
    "workshop_rating_": "Same score across all workshop questions",
}
for pre, human_msg in section_labels.items():
    cols = [c for c in df.columns if c.startswith(pre)]
    if cols:
        vals = df[cols].apply(pd.to_numeric, errors="coerce")
        straight = vals.nunique(axis=1) == 1
        name = f"CHK_straightliner_{pre.rstrip('_')}"
        res[name] = "OK"
        res.loc[straight, name] = "Straight-liner"

# 24) If any brand considered B3a but E4 low → flag (coarse sanity, E4 is quota-make specific)
#if COL["E4_choose_brand"] in df.columns and brand_cols["consider"]:
#    low_e4 = ~df[COL["E4_choose_brand"]].apply(lambda x: in_vals(x, [4, 5]))
#    any_consider = (
#        pd.DataFrame({b: df[col].apply(boolish) for b, col in brand_cols["consider"].items()}).any(axis=1)
#        if brand_cols["consider"]
#        else False
#    )
#    res["CHK_E4_low_with_consider"] = "OK"
#    res.loc[any_consider & low_e4, "CHK_E4_low_with_consider"] = "Low E4 but considering brands"

# 37) E1 vs F1 proximity (|diff|<=2)
if COL["E1_overall"] in df.columns and "overall_rating_truck" in df.columns:
    e1 = to_num(df[COL["E1_overall"]])
    f1 = to_num(df["overall_rating_truck"])
    res["CHK_E1_vs_F1"] = "OK"
    res.loc[(e1 - f1).abs() > 2, "CHK_E1_vs_F1"] = ">2 pts diff"

# 5/16) S4a1 recent vs A3 (years-ago per brand): if S4a1 recent but no brand with ≤5 years
A3_pre = "last_purchase_b"  # years ago (0..99 where 9=never)
A4_pre = "last_workshop_visit_b"  # years ago (0..9/never)
A4b_pre = "last_workshop2_visit_b"


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
A4b = find_yearago_block(A4b_pre)

if S["LastPurchaseHD_cat"] in df.columns and A3:
    recent_hd = df[S["LastPurchaseHD_cat"]].apply(lambda x: in_vals(x, [1, 2, 3, 4, 5, 6]))
    any_recent_brand = pd.DataFrame({b: to_num(df[c]) for b, c in A3.items()}) <= 5
    has_recent = any_recent_brand.any(axis=1)
    res["CHK_S4a1_vs_A3"] = "OK"
    res.loc[recent_hd & ~has_recent, "CHK_S4a1_vs_A3"] = "No brand with purchase ≤5y despite S4a1 recent"

# Usage but never used authorised workshop (A4 = 99)
if A4 and brand_cols["usage"]:
    for b in brands:
        ucol = getb(brand_cols["usage"], b)
        wcol = A4.get(b)
        if not ucol or not wcol:
            continue
        used = df[ucol].apply(boolish)
        never_ws = to_num(df[wcol]) == 99
        name = f"CHK_A4_never_b{b}"
        res[name] = "OK"
        res.loc[used & never_ws, name] = "Used brand but never used workshop"

# A4 vs A4b gap (>3y), excluding 99
for b in brands:
    s = A4.get(b)
    p = A4b.get(b)
    if not s or not p:
        continue
    sv = to_num(df[s])
    pv = to_num(df[p])
    mask = sv.notna() & pv.notna() & (sv != 99) & (pv != 99) & ((sv - pv).abs() > 3)
    res.loc[mask, f"CHK_A4_vs_A4b_gap_b{b}"] = ">3y gap between service and parts visits"

# G2 (operation range) vs G1 (industry) map (editable)
INDUSTRY_G2_ALLOWED = {
    8: [1, 2],  # Mining -> short/medium
    10: [1, 2],
    11: [1, 2],  # Construction, Waste -> short/medium
    12: [1],  # Public service -> mostly short
    6: [1, 2],
    7: [1, 2],
    13: [1, 2],
    98: [1, 2, 3],  # Agri/Forestry/Defence/Other
    1: [2, 3],
    2: [2, 3],
    3: [2, 3],
    4: [2, 3],
    5: [2, 3],
    9: [2, 3],  # long/medium industries
}
if "transport_type" in df.columns and "operation_range_volvo_hdt" in df.columns:
    g1 = to_num(df["transport_type"])
    g2 = to_num(df["operation_range_volvo_hdt"])
    bad = []
    for i in range(len(df)):
        ind = int(g1.iat[i]) if not pd.isna(g1.iat[i]) else None
        rng = int(g2.iat[i]) if not pd.isna(g2.iat[i]) else None
        if ind is None or rng is None:
            bad.append(False)
        else:
            allowed = INDUSTRY_G2_ALLOWED.get(ind, [1, 2, 3])
            bad.append(rng not in allowed)
    res.loc[bad, "CHK_G2_vs_G1"] = "Operation range unmatched for industry"

# B3a (consider) vs E4 for quota_make (if available)
if "quota_make" in df.columns and COL.get("E4_choose_brand") in df.columns and brand_cols["consider"]:
    qm = df["quota_make"].apply(parse_brand_id)
    e4_hi = df[COL["E4_choose_brand"]].apply(lambda x: in_vals(x, [4, 5]))
    mask = []
    for i, b in qm.items():
        if not b:
            mask.append(False)
            continue
        col = brand_cols["consider"].get(b)
        val = boolish(df.at[i, col]) if (col and col in df.columns) else False
        mask.append(val and not e4_hi.iat[i])
    res.loc[mask, "CHK_B3a_vs_E4_quota"] = "Consider quota make but low likelihood to choose (E4)"

# E1/E4/E4c vs B2 alignment (for quota make)
if "quota_make" in df.columns:
    qm = df["quota_make"].apply(parse_brand_id)
    for i, b in qm.items():
        if not b:
            continue
        b2col = brand_cols["impression"].get(b)
        if not b2col:
            continue
        b2 = to_num(df.at[i, b2col])
        e_vals = []
        for c in [COL.get("E1_overall"), COL.get("E4_choose_brand"), COL.get("E4c_pref_strength")]:
            if c and c in df.columns:
                e_vals.append(to_num(df.at[i, c]))
        hiE = any([v in [4, 5] for v in e_vals if not pd.isna(v)])
        loE = any([v in [1, 2] for v in e_vals if not pd.isna(v)])
        if not pd.isna(b2):
            if (b2 <= 3) and hiE:
                res.loc[i, "CHK_E_hi_vs_B2_low"] = "E* high but B2 low"
            if (b2 >= 4) and loE:
                res.loc[i, "CHK_E_low_vs_B2_hi"] = "E* low but B2 high"

# ----------------------------
# Apply optional custom rules JSON (if provided)
# ----------------------------
try:
    res = apply_custom_rules(df, res, rules)
except Exception:
    st.warning("Custom rules JSON present but could not be applied. Check the Rule Builder export.")

# ----------------------------
# KPI: share of A2b (main brand) that is also in B3a (considered)
# ----------------------------
if COL["main_brand"] in df.columns and brand_cols["consider"]:
    mb = df[COL["main_brand"]].apply(parse_brand_id)

    flags = []
    skipped_unparsed = 0
    skipped_no_col = 0
    for i, b in mb.items():
        if not b:
            flags.append(np.nan)
            skipped_unparsed += 1
            continue
        col = brand_cols["consider"].get(b)
        if not col or col not in df.columns:
            flags.append(np.nan)
            skipped_no_col += 1
            continue

        val = df.at[i, col]
        mark = consider_mark(val)  # treat #NULL!/empty as NaN, not False
        if mark is True:
            flags.append(1.0)
        elif mark is False:
            flags.append(0.0)
        else:
            flags.append(np.nan)

    flags = pd.Series(flags, index=df.index, dtype="float")
    valid = flags.dropna()

    # De-dup respondents if multiple rows per respid
    if not valid.empty and "respid" in df.columns and df["respid"].nunique() < len(df):
        valid = valid.groupby(df.loc[valid.index, "respid"]).max()

    if not valid.empty:
        share = float(valid.mean())
        n_eval = int(valid.count())
        msg = f"A2b main brand is in B3a consider: {share:.1%} (n={n_eval} evaluable; target ~70–80%)."
        if skipped_unparsed or skipped_no_col:
            msg += f"  Skipped: {skipped_unparsed} unparsed A2b, {skipped_no_col} with no matching B3a column."
        st.info(msg)
    else:
        st.warning("A2b→B3a KPI is N/A — no evaluable rows (A2b couldn’t be parsed or no matching B3a columns like 'consideration_b{code}').")

# ----------------------------
# Output — build a clear, issues-only digest + tidy long table
# ----------------------------
# 1) Collapse all CHK_* columns into one readable, grouped string per row
chk_cols = [c for c in res.columns if c.startswith("CHK_")]

def _brand_name_from_chk(colname: str) -> str | None:
    m = re.search(r"_b(\d+)", colname)
    if not m:
        return None
    code = m.group(1)
    # Friendlier fallback if mapping is missing (e.g., code 58)
    return CODE_TO_BRAND.get(code, f"Brand code {code} (unmapped)")

FRIENDLY = {
    "S3a1-3 ≠ S3": "Truck subtypes don’t add up to total (S3).",
    "Straight-liner": "Same score given across a whole section.",
    "Operation range unmatched for industry": "Operation range looks unmatched for this industry.",
    "Misaligned": "High performance score but low overall impression (B2).",
    ">2 pts diff": "Overall satisfaction vs. truck rating differ by >2 points.",
    "B2 should be 4/5": "Overall impression (B2) is low for that brand.",
    "B2 should be 4/5 for preferred brand": "Overall impression (B2) is low for the preferred brand.",
    "No brand with purchase ≤5y despite S4a1 recent": "S4 says recent purchase, but no brand purchased in last 5 years.",
    ">3y gap between service and parts visits": "Service vs. parts visit dates differ by >3 years.",
    "Too many brands vs fleet size": "Awareness count seems high vs. fleet size.",
    "Main brand not in A1a": "Main brand not listed in unaided awareness.",
    "Main brand not in A2a": "Main brand not listed in usage.",
    "Used brand but never used workshop": "Used brand but never used authorized workshop.",
    "E* high but B2 low": "Experience high but overall impression (B2) low.",
    "E* low but B2 high": "Experience low but overall impression (B2) high.",
    "Low E4 but considering brands": "Considering brands but low likelihood to choose (E4).",
}
# Dynamic closeness phrasings for Excel/long list
FRIENDLY.update({
    f"Expect ≥{min_lo}": f"Closeness is a bit low(target ≥{min_lo}).",
    f"Expect ≥{min_hi}": f"Closeness is lower than expected (target ≥{min_hi}).",
})

def _section_from_col(col: str) -> str:
    if "truck_rating_" in col: return "truck"
    if "salesdelivery_rating_" in col: return "sales & delivery"
    if "workshop_rating_" in col: return "workshop"
    return "section"

def _human_list(prefix: str, items: list[str], show=4) -> str:
    uniq = sorted(set(items))
    if len(uniq) <= show:
        return f"{prefix} {', '.join(uniq)}"
    return f"{prefix} {len(uniq)} brands incl. {', '.join(uniq[:show])}"

def digest_row(r: pd.Series) -> str:
    closeness_hi_brands, closeness_lo_brands = [], []
    b2low_brands, misalign_brands = [], []
    straightliner_sections = []
    generic_bits = []

    for c in chk_cols:
        v = r.get(c, "")
        if not isinstance(v, str) or v in ("", "OK") or pd.isna(v):
            continue

        brand = _brand_name_from_chk(c)

        # Group closeness by tier
        if v == f"Expect ≥{min_hi}" and brand:
            closeness_hi_brands.append(brand); continue
        if v == f"Expect ≥{min_lo}" and brand:
            closeness_lo_brands.append(brand); continue

        # Other grouped brand-level items
        if v == "Misaligned" and brand:
            misalign_brands.append(brand); continue
        if v in ("B2 should be 4/5", "B2 should be 4/5 for preferred brand") and brand:
            b2low_brands.append(brand); continue
        if v == "Straight-liner":
            straightliner_sections.append(_section_from_col(c)); continue

        # Everything else → translate if we can, keep brand if present
        friendly = FRIENDLY.get(v, v)
        generic_bits.append(friendly if not brand else f"{brand}: {friendly}")

    parts = []
    if closeness_hi_brands:
        parts.append(_human_list(f"Closeness is lower than expected (target ≥{min_hi}) for:", closeness_hi_brands))
    if closeness_lo_brands:
        parts.append(_human_list(f"Closeness is a bit low (target ≥{min_lo}) for:", closeness_lo_brands))
    if b2low_brands:
        parts.append(_human_list("Overall impression (B2) is low for:", b2low_brands))
    if misalign_brands:
        parts.append(_human_list("High performance but low impression for:", misalign_brands))
    if straightliner_sections:
        parts.append(f"Straight-liner in {len(straightliner_sections)} section(s)")

    parts += generic_bits
    return " | ".join(parts) if parts else "Consistent"

res["Consistency_Check"] = res.apply(digest_row, axis=1)

# Fix display artifacts (Excel/CSV encoding glitches showing as â‰  / â‰≥)
res["Consistency_Check"] = (
    res["Consistency_Check"].astype(str)
    .str.replace("â‰ ", "≠", regex=False)
    .str.replace("â‰≥", "≥", regex=False)
    .str.replace("â‰¥", "≥", regex=False)
)

# Count issues per row
res["Issue_Count"] = res["Consistency_Check"].apply(
    lambda s: 0
    if (s == "Consistent" or pd.isna(s))
    else len([x for x in s.split(" | ") if x.strip()])
)

# 2) Key columns for context
key_cols = [
    c
    for c in [
        "respid",
        "id",
        "company_position",
        "n_heavy_duty_trucks",
        "quota_make",
        "main_brand",
        "preference",
        "overall_satisfaction",
        "likelihood_choose_brand",
        "preference_strength",
    ]
    if c in res.columns
]

# 3) Issues-only digest table
issues_only = res[res["Consistency_Check"] != "Consistent"].copy()
digest_view = (
    issues_only[key_cols + ["Issue_Count", "Consistency_Check"]]
    if key_cols
    else issues_only[["Issue_Count", "Consistency_Check"]]
)

# ----------------------------
# Filters & issue summary (niceties)
# ----------------------------
st.subheader("Issues Digest")

# Build issue tokens and types
issues_exploded = (
    digest_view["Consistency_Check"].astype(str).str.split(r"\s\|\s", expand=False).explode().dropna().str.strip()
)

# Derive 'type' after the optional brand prefix ("Brand: Message")
issue_types = (
    issues_exploded.apply(lambda t: t.split(": ", 1)[1] if ": " in t else t)
    .value_counts()
    .rename_axis("Issue")
    .to_frame("Count")
    .reset_index()
)

# Filters
col1, col2, col3 = st.columns([2, 2, 1])
with col1:
    keyword = st.text_input("Search (brand, rule text, respid/id)", "")
with col2:
    choose_types = st.multiselect(
        "Filter by issue type(s)", options=issue_types["Issue"].tolist(), default=[]
    )
with col3:
    min_flags = st.number_input("Min #flags", min_value=1, max_value=int(digest_view["Issue_Count"].max() or 1), value=1)

# Apply filters
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

# 4) Tidy long list of violations (one row per issue)
rows = []
if len(issues_only):
    for idx, r in issues_only.iterrows():
        for c in chk_cols:
            v = r.get(c, "")
            if isinstance(v, str) and v not in ("", "OK") and not pd.isna(v):
                brand = _brand_name_from_chk(c)
                rows.append(
                    {
                        "row_index": idx,
                        "respid": r.get("respid", np.nan),
                        "id": r.get("id", np.nan),
                        "main_brand": r.get(COL["main_brand"], np.nan),
                        "quota_make": r.get("quota_make", np.nan),
                        "rule": c,
                        "brand": brand,
                        "message": FRIENDLY.get(v, v),  # translate here too
                    }
                )
issues_long = (
    pd.DataFrame(rows)
    if rows
    else pd.DataFrame(columns=["row_index", "respid", "id", "rule", "brand", "message"])
)

# Clean artifacts in detailed messages as well
if not issues_long.empty:
    issues_long["message"] = (
        issues_long["message"].astype(str)
        .str.replace("â‰ ", "≠", regex=False)
        .str.replace("â‰≥", "≥", regex=False)
        .str.replace("â‰¥", "≥", regex=False)
    )

st.subheader("Issues only — Detailed List (one row per flag)")
st.dataframe(issues_long, use_container_width=True)

# 5) Optional: full table
if show_full:
    st.subheader("Full results table")
    st.dataframe(res, use_container_width=True)

# ----------------------------
# Tiny legend for non-technical readers
# ----------------------------
with st.expander("Legend — what the Flags mean"):
    st.markdown(f"""
- **Truck subtypes don’t add up to total (S3):** Counts in S3a1–3 should equal total S3 (only checked when any subtype is answered).
- **Closeness expectations:**  
  • **High intent** (B2=5 or brand is preferred): expect **≥{min_hi}**.  
  • **Moderate intent** (B2=4 or brand is considered): expect **≥{min_lo}**.
- **Overall impression (B2) is low:** Using/considering a brand but B2 &lt; 4.
- **High performance but low impression:** Cfunc high while B2 &lt; 4.
- **Straight-liner:** Same score across a whole section (may indicate low engagement).
- **Service vs parts &gt;3y:** Recency answers disagree by more than 3 years.
- **E1 vs truck &gt;2 pts:** Overall satisfaction and truck rating are far apart.
- **Operation range atypical:** G2 choice looks unusual for the stated industry (G1).
- **Awareness vs fleet size:** Awareness count looks high vs. fleet size (n_heavy_duty_trucks).
    """)

# ----------------------------
# Excel + CSV exports (niceties)
# ----------------------------
def _autofit(ws, data_df):
    # Set sensible column widths + freeze header + add filter
    for i, col in enumerate(data_df.columns):
        try:
            max_len = int(max(data_df[col].astype(str).map(len).max(), len(col))) + 2
        except ValueError:
            max_len = len(col) + 2
        ws.set_column(i, i, min(max_len, 60))
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, max(len(data_df), 1), max(len(data_df.columns) - 1, 0))


def build_excel(digest_df: pd.DataFrame, detail_df: pd.DataFrame, full_df: pd.DataFrame | None = None) -> BytesIO | None:
    # Build a multi-sheet Excel with Issues, Summary, Detailed (+ Full optional)
    # Requires xlsxwriter for conditional formatting.
    try:
        import xlsxwriter  # noqa: F401
    except Exception:
        st.error("Excel export needs the 'xlsxwriter' package.\nTry one of:\n- py -m pip install xlsxwriter (Windows PowerShell)\n- python -m pip install xlsxwriter\n- conda install xlsxwriter")
        return None

    # Summary tables
    if not digest_df.empty:
        exploded = (
            digest_df["Consistency_Check"].astype(str).str.split(r"\s\|\s", expand=False).explode().dropna().str.strip()
        )
        summary_types = (
            exploded.apply(lambda t: t.split(": ", 1)[1] if ": " in t else t)
            .value_counts()
            .rename_axis("Issue")
            .to_frame("Count")
            .reset_index()
        )
    else:
        summary_types = pd.DataFrame(columns=["Issue", "Count"])  # empty

    out = BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
        digest_df.to_excel(writer, index=False, sheet_name="Issues")
        summary_types.to_excel(writer, index=False, sheet_name="Summary")
        detail_df.to_excel(writer, index=False, sheet_name="Detailed_Issues")
        if full_df is not None:
            full_df.to_excel(writer, index=False, sheet_name="Full_Table")

        wb = writer.book
        ws_issues = writer.sheets["Issues"]
        ws_summary = writer.sheets["Summary"]
        ws_detailed = writer.sheets["Detailed_Issues"]
        _autofit(ws_issues, digest_df)
        _autofit(ws_summary, summary_types)
        _autofit(ws_detailed, detail_df)
        if full_df is not None:
            _autofit(writer.sheets["Full_Table"], full_df)

        # Conditional formatting highlights (Issues sheet: Consistency_Check column)
        if "Consistency_Check" in digest_df.columns:
            cc_idx = list(digest_df.columns).index("Consistency_Check")
            nrows = max(len(digest_df), 1)
            col_letter = xlsxwriter.utility.xl_col_to_name(cc_idx)
            rng = f"{col_letter}2:{col_letter}{nrows+1}"
            bold = wb.add_format({"bold": True})
            wrap = wb.add_format({"text_wrap": True})
            ws_issues.set_column(cc_idx, cc_idx, 80, wrap)
            for token in [
                "High performance but low impression",
                "Closeness is lower than expected",
                "Closeness is a bit low",
                "Straight-liner",
                "Overall satisfaction vs. truck rating differ by >2 points",
                "Service vs. parts visit dates differ by >3 years",
                "Awareness count seems high vs. fleet size",
            ]:
                ws_issues.conditional_format(
                    rng,
                    {"type": "text", "criteria": "containing", "value": token, "format": bold},
                )

    out.seek(0)
    return out


def build_issues_only_excel(detail_df: pd.DataFrame) -> BytesIO | None:
    """Single-file Excel for the long issues list with handy summaries."""
    try:
        import xlsxwriter  # noqa: F401
    except Exception:
        st.error("Excel export needs the 'xlsxwriter' package.\nTry one of:\n- py -m pip install xlsxwriter (Windows PowerShell)\n- python -m pip install xlsxwriter\n- conda install xlsxwriter")
        return None

    # Summaries
    if not detail_df.empty:
        summary_by_issue = (
            detail_df["message"].value_counts().rename_axis("Issue").to_frame("Count").reset_index()
        )
        summary_by_rule = (
            detail_df["rule"].value_counts().rename_axis("Rule").to_frame("Count").reset_index()
        )
    else:
        summary_by_issue = pd.DataFrame(columns=["Issue", "Count"])
        summary_by_rule = pd.DataFrame(columns=["Rule", "Count"])

    out = BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
        # Main detailed list
        detail_df.to_excel(writer, index=False, sheet_name="Detailed_Issues")
        _autofit(writer.sheets["Detailed_Issues"], detail_df)

        # Summaries for quick filtering
        summary_by_issue.to_excel(writer, index=False, sheet_name="Summary_by_Issue")
        _autofit(writer.sheets["Summary_by_Issue"], summary_by_issue)

        summary_by_rule.to_excel(writer, index=False, sheet_name="Summary_by_Rule")
        _autofit(writer.sheets["Summary_by_Rule"], summary_by_rule)

    out.seek(0)
    return out


# CSV (use utf-8-sig so Excel opens cleanly)
digest_csv = filtered_digest.to_csv(index=False).encode("utf-8-sig")
st.download_button(
    "💾 Download issues digest (CSV)", digest_csv, file_name="bcs_issues_digest.csv", mime="text/csv"
)

#long_csv = issues_long.to_csv(index=False).encode("utf-8-sig")
#st.download_button(
#    "💾 Download issues long list (CSV)", long_csv, file_name="bcs_issues_long.csv", mime="text/csv"
#)

# One-click Excel (multi-sheet with digest + detailed)
excel_bytes = build_excel(filtered_digest, issues_long, res if show_full else None)
if excel_bytes is not None:
    st.download_button(
        "📘 Download Issues-only Excel (Summary)",
        data=excel_bytes.getvalue(),
        file_name="logic_issues.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

# NEW: Issues-only Excel (just the detailed list + summaries)
issues_only_bytes = build_issues_only_excel(issues_long)
if issues_only_bytes is not None:
    st.download_button(
        "📘 Download Issues-only Excel (Detailed list)",
        data=issues_only_bytes.getvalue(),
        file_name="issues_only.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

st.markdown("---")
st.markdown(
    "**Checks included:** S3a sum to S3 (only if any subtype answered); A1a cap + A1a vs S3 sanity; "
    "A2b∈A1a & ∈A2a; B1 familiarity (used/aware); usage/consider→B2≥4; pref(B3b)→B2≥4; "
    f"C-close tiered: B2=5/preferred→≥{min_hi}, B2=4/considered→≥{min_lo}; "
    "Cfunc vs B2 (thresholded); straight-liners in F2/F4/F6; consider vs E4 sanity; E1 vs F1 proximity; "
    "S4a1 recent vs A3; usage but never workshop; A4 vs A4b gap; G2 vs G1; quota B3a vs E4; E* vs B2 alignment. "
    "**+ Optional custom rules JSON.**"
)
