import os
import json
from pathlib import Path
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials


# ----------------------------
# Constants
# ----------------------------
FORM_AN_GUIDANCE_URL = "https://www.gov.uk/government/publications/form-an-guidance/form-an-guidance-accessible"


# ----------------------------
# Load .env if present (LOCAL ONLY)
# Streamlit Cloud won’t load .env, but it WILL provide env vars / st.secrets.
# ----------------------------
ROOT = Path(__file__).resolve().parent
if (ROOT / ".env").exists():
    load_dotenv(ROOT / ".env")


# ----------------------------
# Settings (env vars first; fall back to st.secrets)
# ----------------------------
def _get_setting(key: str, default: str = "") -> str:
    v = os.environ.get(key, "")
    if v.strip():
        return v.strip()

    # Optional fallback if someone puts simple keys in Streamlit secrets
    # (Cloud sets secrets as env vars too, but this doesn’t hurt.)
    try:
        if key in st.secrets:
            return str(st.secrets[key]).strip()
    except Exception:
        pass

    return default.strip()


SHEET_ID = _get_setting("GOOGLE_SHEET_ID", "")
TAB_NAME = _get_setting("GOOGLE_SHEET_TAB", "trips")
DEFAULT_APPLICATION_DATE_STR = _get_setting("DEFAULT_APPLICATION_DATE", "")  # optional

# Local credentials file path (do NOT rely on this in Streamlit Cloud)
CREDENTIALS_JSON_PATH = _get_setting("GOOGLE_CREDENTIALS_JSON", str(ROOT / "credentials.json"))

# Streamlit Cloud secrets (recommended): either a TOML table [gcp_service_account] or a JSON string env var.
# If you use the Secrets UI with:
# [gcp_service_account]
# type="service_account"
# ...
# then st.secrets["gcp_service_account"] will be a dict.
SERVICE_ACCOUNT_JSON_ENV = _get_setting("GCP_SERVICE_ACCOUNT_JSON", "")


# ----------------------------
# Date helpers
# ----------------------------
def safe_parse_date(s) -> date | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def uk_fmt(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def years_ago(d: date, years: int) -> date:
    """
    Exact 'same calendar day' years ago (handles 29 Feb).
    """
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        # 29 Feb -> 28 Feb fallback
        return d.replace(month=2, day=28, year=d.year - years)


def one_year_ago(d: date) -> date:
    return years_ago(d, 1)


# ----------------------------
# Home Office counting rule (Form AN guidance)
# Only WHOLE days abroad count: exclude day you leave AND day you return.
# Abroad days are those strictly between (leave, return).
# ----------------------------
def whole_days_abroad(leave: date, ret: date) -> int:
    if ret <= leave:
        return 0
    # leave=1st, return=2nd => (2-1)-1 = 0 whole days abroad
    return max(0, (ret - leave).days - 1)


def countable_interval(leave: date, ret: date) -> tuple[date, date] | None:
    """
    The days that count as 'abroad' are: leave+1 ... ret-1 (inclusive).
    """
    start = leave + timedelta(days=1)
    end = ret - timedelta(days=1)
    if end < start:
        return None
    return start, end


def interval_overlap(a_start: date, a_end: date, b_start: date, b_end: date) -> tuple[date, date] | None:
    s = max(a_start, b_start)
    e = min(a_end, b_end)
    if e < s:
        return None
    return s, e


def count_absences_in_window(trips: pd.DataFrame, window_start: date, window_end: date) -> int:
    """
    Count WHOLE days abroad (per Form AN) that fall within [window_start, window_end] inclusive.
    """
    total = 0
    for _, r in trips.iterrows():
        leave = r["start_date"]
        ret = r["end_date"]
        if not isinstance(leave, date) or not isinstance(ret, date):
            continue

        ci = countable_interval(leave, ret)
        if ci is None:
            continue
        cs, ce = ci

        ov = interval_overlap(cs, ce, window_start, window_end)
        if ov is None:
            continue

        os_, oe_ = ov
        total += (oe_ - os_).days + 1  # inclusive
    return total


def is_in_uk_on_day(trips: pd.DataFrame, d: date) -> bool:
    """
    Under 'whole days abroad', they are abroad on day d iff (leave < d < return).
    So they are in the UK on d if it is NOT strictly between any leave/return.
    """
    for _, r in trips.iterrows():
        leave = r["start_date"]
        ret = r["end_date"]
        if leave < d < ret:
            return False
    return True


def tick(ok: bool) -> str:
    return "✅" if ok else "❌"


# ----------------------------
# Google auth (local file OR Streamlit secrets)
# ----------------------------
def build_credentials() -> Credentials | None:
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    # 1) Streamlit secrets table: [gcp_service_account]
    try:
        if "gcp_service_account" in st.secrets:
            info = dict(st.secrets["gcp_service_account"])
            return Credentials.from_service_account_info(info, scopes=scopes)
    except Exception:
        pass

    # 2) JSON string env var: GCP_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'
    if SERVICE_ACCOUNT_JSON_ENV.strip():
        try:
            info = json.loads(SERVICE_ACCOUNT_JSON_ENV)
            return Credentials.from_service_account_info(info, scopes=scopes)
        except Exception:
            return None

    # 3) Local credentials.json file
    p = Path(CREDENTIALS_JSON_PATH)
    if p.exists():
        return Credentials.from_service_account_file(str(p), scopes=scopes)

    return None


@st.cache_data(ttl=60)
def load_trips_df(sheet_id: str, tab_name: str) -> pd.DataFrame:
    creds = build_credentials()
    if creds is None:
        raise RuntimeError(
            "Missing Google credentials. Locally: keep credentials.json next to app.py (or set GOOGLE_CREDENTIALS_JSON).\n"
            "In Streamlit Cloud: set Secrets with a [gcp_service_account] block (recommended) "
            "or set env var GCP_SERVICE_ACCOUNT_JSON."
        )

    gc = gspread.authorize(creds)
    ws = gc.open_by_key(sheet_id).worksheet(tab_name)
    values = ws.get_all_values()

    if not values or len(values) < 2:
        return pd.DataFrame(columns=["start_date", "end_date", "note", "days_absent"])

    header = [h.strip() for h in values[0]]
    rows = values[1:]
    df = pd.DataFrame(rows, columns=header)

    col_map = {c.lower().strip(): c for c in df.columns}
    start_col = col_map.get("start_date")
    end_col = col_map.get("end_date")
    note_col = col_map.get("note")

    if not start_col or not end_col:
        raise ValueError("Sheet tab must have columns named: start_date, end_date (and optional note).")

    df = df.rename(columns={start_col: "start_date", end_col: "end_date"})
    if note_col:
        df = df.rename(columns={note_col: "note"})
    else:
        df["note"] = ""

    df["start_date"] = df["start_date"].apply(safe_parse_date)
    df["end_date"] = df["end_date"].apply(safe_parse_date)
    df["note"] = df["note"].fillna("").astype(str)

    df = df.dropna(subset=["start_date", "end_date"]).copy()

    # Latest first
    df = df.sort_values("start_date", ascending=False).reset_index(drop=True)

    df["days_absent"] = df.apply(lambda r: whole_days_abroad(r["start_date"], r["end_date"]), axis=1)
    return df


# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="Ari – UK Citizenship Absence Checker", layout="centered")
st.title("🇬🇧 Ari – UK Citizenship Absence Checker")

st.markdown(
    "Counting rule used (official): **only whole days’ absences count** — "
    "**do not count** the day you leave or the day you return. "
    f"[Form AN guidance]({FORM_AN_GUIDANCE_URL})."
)

# Require sheet settings
if not SHEET_ID:
    st.error("Missing GOOGLE_SHEET_ID. (Local: put it in .env. Cloud: add it to Streamlit Secrets.)")
    st.stop()

# Default application date
default_app_date = date.today()
if DEFAULT_APPLICATION_DATE_STR:
    parsed = safe_parse_date(DEFAULT_APPLICATION_DATE_STR)
    if parsed:
        default_app_date = parsed

app_date = st.date_input("Planned application date", value=default_app_date, format="DD/MM/YYYY")

# Load data
try:
    trips_df = load_trips_df(SHEET_ID, TAB_NAME)
except Exception as e:
    st.error(str(e))
    st.stop()

# Windows using exact "1 year ago" / "5 years ago" calendar logic
window_end = app_date
window_12m_start = one_year_ago(app_date)
window_5y_start = years_ago(app_date, 5)

abs_12m = count_absences_in_window(trips_df, window_12m_start, window_end)
abs_5y = count_absences_in_window(trips_df, window_5y_start, window_end)

# Presence 5 years ago (same calendar day)
five_years_ago_day = years_ago(app_date, 5)
present_5y_ago = is_in_uk_on_day(trips_df, five_years_ago_day)

OK_12M = abs_12m <= 90
OK_5Y = abs_5y <= 450

c1, c2, c3 = st.columns(3)
c1.metric("Last 12 months", f"{abs_12m} days", help="Common guideline: ≤ 90 days (whole days abroad)")
c2.metric("Last 5 years", f"{abs_5y} days", help="Common guideline: ≤ 450 days (whole days abroad)")
c3.metric("In UK 5 years ago?", tick(present_5y_ago), help=f"Checked: {uk_fmt(five_years_ago_day)}")

st.markdown(
    f"""
**Eligibility signals (common rules):**
- {tick(OK_12M)} **≤ 90 days** absent in last 12 months  
- {tick(OK_5Y)} **≤ 450 days** absent in last 5 years  
- {tick(present_5y_ago)} **In the UK on {uk_fmt(five_years_ago_day)}** (same calendar day 5 years before application)
"""
)

st.divider()
st.subheader("Trips (latest first)")

show = trips_df[["start_date", "end_date", "days_absent", "note"]].copy()
show["start_date"] = show["start_date"].apply(uk_fmt)
show["end_date"] = show["end_date"].apply(uk_fmt)

st.dataframe(show, width="stretch", hide_index=True)

st.caption(
    "Absence days per trip are computed from Form AN guidance: "
    "count only whole days abroad (exclude departure and return days)."
)