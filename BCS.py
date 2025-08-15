# BCS Survey Logic Checker — Lite (clean, rules-json)
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

st.set_page_config(page_title="BCS Survey Logic Checker — Lite", layout="wide")
st.title("📊 BCS Survey Logic Checker — Lite")
st.caption("No cross-wave / client-sample / desk-research dependencies. Uses your schema and brand patterns. Optional custom rules JSON.")

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
        return pd.read_excel(uploaded_file)  # note: .xls may require xlrd installed

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
    close_min = st.slider("C-close minimum when intent is strong", 5, 10, 8)
    cfunc_hi = st.slider("Cfunc (performance) 'high' threshold when B2 is low", 4, 10, 6)

if not data_file:
    st.info("Upload a CSV/XLSX to begin.")
    st.stop()

try:
    data_file.seek(0)  # in case Streamlit reuses the buffer
    df = read_any_table(data_file, enc_override=enc, delim_override=delim, skip_bad=skip_bad)
except Exception as e:
    st.error(f"Failed to read file: {e}")
    st.stop()

res = df.copy()

# ----------------------------
# Helpers & mappings (from your schema)
# ----------------------------
PREFIX = {
    "awareness": "unaided_aware_",      # A1a multi (0/1)
    "usage": "usage_",                  # A2a multi (0/1)
    "impression": "overall_impression_",# B2 1–5
    "consider": "consideration_",       # B3a multi (0/1)
    "close": "closeness_",              # C-close 1–10
    "cfunc": "performance_",            # Cfunc 1–10 in this study
}

COL = {
    "main_brand": "main_brand",                 # A2b (preferred of A2a)
    "pref_future_single": "preference",         # B3b single pick (brand code or label)
    "E1_overall": "overall_satisfaction",       # E1 (1–5)
    "E4_choose_brand": "likelihood_choose_brand",  # E4 (1–5)
}

S = {
    "HD_count": "n_heavy_duty_trucks",   # S3
    "Tractors": "n_tractors",            # S3a1 (Korea only)
    "Rigids": "n_rigids",                # S3a2 (Korea only)
    "Tippers": "n_tippers",              # S3a3 (Korea only)
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
    s = str(x).strip().lower()
    return s in {"1", "true", "t", "yes", "y"}

def in_vals(x, allowed: List[int]) -> bool:
    try:
        return int(float(x)) in allowed
    except Exception:
        return False

def parse_brand_id(val) -> str | None:
    """Return brand code as string (e.g., '38') from code like 38 / 'b38',
    or from label names using BRAND_NAME_TO_CODE (case-insensitive)."""
    s = str(val).strip()
    if not s or s.lower() in {"nan", "none"}:
        return None
    if s.isdigit():
        return s
    sl = s.lower()
    m = re.search(r"b(\d+)$", sl)
    if m:
        return m.group(1)
    if sl in BRAND_NAME_TO_CODE:
        return BRAND_NAME_TO_CODE[sl]
    sl2 = re.sub(r"\s+", " ", sl.replace("-", " ").replace("/", "/"))
    return BRAND_NAME_TO_CODE.get(sl2)

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
                bid = m.group(1)
                tgt = f"{impression_prefix}b{bid}"
                if tgt not in df.columns:
                    continue
                name_bid = f"{name}_b{bid}"
                bad = (to_num(df[col]) == 1) & ~to_num(df[tgt]).isin(allowed)
                res[name_bid] = "OK"
                res.loc[bad, name_bid] = msg
    return res

# ----------------------------
# Checks (Lite set)
# ----------------------------
# 0) Structural: S3a1-3 sum equals S3 (if all exist)
if all(k in df.columns for k in [S["HD_count"], S["Tractors"], S["Rigids"], S["Tippers"]]):
    res["CHK_S3a_sum"] = "OK"
    subsum = to_num(df[S["Tractors"]]).fillna(0) + to_num(df[S["Rigids"]]).fillna(0) + to_num(df[S["Tippers"]]).fillna(0)
    total = to_num(df[S["HD_count"]])
    res.loc[(total.notna()) & (subsum != total), "CHK_S3a_sum"] = "S3a1-3 ≠ S3"

# 3) A1a total sanity (awareness) + cap
aw_cols = list(brand_cols["awareness"].values())
if aw_cols:
    sel_aw = df[aw_cols].applymap(boolish)
    count_aw = sel_aw.sum(axis=1)
    res["CHK_A1a_total_count"] = count_aw
    res["CHK_A1a_total_flag"] = np.where(count_aw > a1a_cap, f">{a1a_cap} brands", "OK")

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

# 10) C-close high when strong intent/impression
if brand_cols["close"]:
    for b in brands:
        ccol = getb(brand_cols["close"], b)
        if not ccol:
            continue
        strong = pd.Series(False, index=df.index)
        if brand_cols["consider"].get(b):
            strong |= df[brand_cols["consider"][b]].apply(boolish)
        if brand_cols["impression"].get(b):
            strong |= df[brand_cols["impression"][b]].apply(lambda x: in_vals(x, [4, 5]))
        bad = strong & ~df[ccol].apply(lambda x: in_vals(x, list(range(close_min, 11))))
        name = f"CHK_Cclose_high_b{b}"
        res[name] = "OK"
        res.loc[bad, name] = f"Expect ≥{close_min}"

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
for pre in ["truck_rating_", "salesdelivery_rating_", "workshop_rating_"]:
    cols = [c for c in df.columns if c.startswith(pre)]
    if cols:
        vals = df[cols].apply(pd.to_numeric, errors="coerce")
        straight = vals.nunique(axis=1) == 1
        name = f"CHK_{pre}straightliner"
        res[name] = "OK"
        res.loc[straight, name] = "Straight-liner"

# 24) If any brand considered but E4 low → flag (coarse sanity, E4 is quota-make specific)
if COL["E4_choose_brand"] in df.columns and brand_cols["consider"]:
    low_e4 = ~df[COL["E4_choose_brand"]].apply(lambda x: in_vals(x, [4, 5]))
    any_consider = pd.DataFrame({b: df[col].apply(boolish) for b, col in brand_cols["consider"].items()}).any(axis=1) if brand_cols["consider"] else False
    res["CHK_E4_low_with_consider"] = "OK"
    res.loc[any_consider & low_e4, "CHK_E4_low_with_consider"] = "Low E4 but considering brands"

# 37) E1 vs F1 proximity (|diff|<=2)
if COL["E1_overall"] in df.columns and "overall_rating_truck" in df.columns:
    e1 = to_num(df[COL["E1_overall"]])
    f1 = to_num(df["overall_rating_truck"])
    res["CHK_E1_vs_F1"] = "OK"
    res.loc[(e1 - f1).abs() > 2, "CHK_E1_vs_F1"] = ">2 pts diff"

# 5/16) S4a1 recent vs A3 (years-ago per brand): if S4a1 recent but no brand with ≤5 years
A3_pre = "last_purchase_b"        # years ago (0..99 where 99=never)
A4_pre = "last_workshop_visit_b"  # years ago (0..99/never)
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
    recent_hd = df[S["LastPurchaseHD_cat"]].apply(lambda x: in_vals(x, [1,2,3,4,5,6]))
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

# ----------------------------
# Apply optional custom rules JSON (if provided)
# ----------------------------
try:
    res = apply_custom_rules(df, res, rules)
except Exception:
    st.warning("Custom rules JSON present but could not be applied. Check the Rule Builder export.")

# ----------------------------
# Output
# ----------------------------
st.subheader("Results preview")
st.dataframe(res, use_container_width=True)

csv = res.to_csv(index=False).encode("utf-8")
st.download_button("💾 Download flagged CSV", csv, file_name="bcs_checked_lite.csv", mime="text/csv")

st.markdown("---")
st.markdown("**Included checks:** S3a sum to S3; A1a cap; A2b∈A1a & ∈A2a; usage/consider→B2≥4; pref(B3b)→B2≥4; strong intent→C-close≥min; Cfunc vs B2 (thresholded); straight-liners in F2/F4/F6; B3a×E4 sanity; E1 vs F1 proximity; S4a1 recent vs A3 years-ago; usage but never workshop. **+ Optional custom rules JSON.**")
