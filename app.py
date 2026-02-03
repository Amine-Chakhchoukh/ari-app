import os
from pathlib import Path
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials


# ----------------------------
# Load .env (ONLY)
# ----------------------------
ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
TAB_NAME = os.environ.get("GOOGLE_SHEET_TAB", "trips").strip()
CREDENTIALS_JSON_PATH = os.environ.get("GOOGLE_CREDENTIALS_JSON", str(ROOT / "credentials.json")).strip()
DEFAULT_APPLICATION_DATE_STR = os.environ.get("DEFAULT_APPLICATION_DATE", "").strip()  # optional


FORM_AN_GUIDANCE_URL = "https://www.gov.uk/government/publications/form-an-guidance/form-an-guidance-accessible"


# ----------------------------
# Date helpers
# ----------------------------
def parse_iso_yyyy_mm_dd(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def parse_uk_dd_mm_yyyy(s: str) -> date:
    return datetime.strptime(s.strip(), "%d/%m/%Y").date()


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


# ----------------------------
# Home Office counting rule (Form AN guidance)
# Only whole days abroad count. Do NOT count the day you leave OR the day you return.
# If leave=1st, return=2nd => 0 days absent
# ----------------------------
def whole_days_abroad(leave: date, ret: date) -> int:
    if ret <= leave:
        return 0
    return max(0, (ret - leave).days - 1)


def countable_interval(leave: date, ret: date) -> tuple[date, date] | None:
    """
    Convert [leave, ret] to the *countable* days interval:
    (leave+1) .. (ret-1), inclusive.
    Returns None if no whole days abroad.
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
    Count whole days abroad (as per Form AN) that fall within [window_start, window_end] inclusive.
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
        total += (oe_ - os_).days + 1  # inclusive count
    return total


def is_in_uk_on_day(trips: pd.DataFrame, d: date) -> bool:
    """
    Presence check: are they in the UK on calendar day d?
    Under Form AN 'whole days abroad', the only days that are definitely 'abroad'
    are the days strictly between leave and return.
    So: they are abroad on day d iff (leave < d < return).
    """
    for _, r in trips.iterrows():
        leave = r["start_date"]
        ret = r["end_date"]
        if leave < d < ret:
            return False
    return True


def uk_fmt(d: date) -> str:
    return d.strftime("%d/%m/%Y")


# ----------------------------
# Google Sheets load
# ----------------------------
@st.cache_data(ttl=60)
def load_trips_df(sheet_id: str, tab_name: str, credentials_path: str) -> pd.DataFrame:
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(credentials_path, scopes=scopes)
    gc = gspread.authorize(creds)

    ws = gc.open_by_key(sheet_id).worksheet(tab_name)
    values = ws.get_all_values()

    if not values or len(values) < 2:
        return pd.DataFrame(columns=["start_date", "end_date", "note", "days_absent"])

    header = [h.strip() for h in values[0]]
    rows = values[1:]

    df = pd.DataFrame(rows, columns=header)

    # Expect columns: start_date, end_date, note (case-insensitive)
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

    # Drop invalid rows
    df = df.dropna(subset=["start_date", "end_date"]).copy()

    # Latest first
    df = df.sort_values("start_date", ascending=False).reset_index(drop=True)

    # Compute per-trip absences
    df["days_absent"] = df.apply(lambda r: whole_days_abroad(r["start_date"], r["end_date"]), axis=1)

    return df


# ----------------------------
# App UI
# ----------------------------
st.set_page_config(page_title="Ari – UK Citizenship Absence Checker", layout="centered")

st.title("🇬🇧 Ari – UK Citizenship Absence Checker")

st.markdown(
    f"Counting rule used (official): **only whole days’ absences count** — "
    f"**do not count** the day you leave or the day you return. "
    f"[Form AN guidance]({FORM_AN_GUIDANCE_URL})."
)

# Basic safety checks (quiet)
if not SHEET_ID:
    st.error("Missing GOOGLE_SHEET_ID in .env")
    st.stop()

if not Path(CREDENTIALS_JSON_PATH).exists():
    st.error(f"Missing credentials.json at: {CREDENTIALS_JSON_PATH}")
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
    trips_df = load_trips_df(SHEET_ID, TAB_NAME, CREDENTIALS_JSON_PATH)
except Exception as e:
    st.error(f"Could not load Google Sheet (tab '{TAB_NAME}').\n\n{e}")
    st.stop()

# Windows (simple approximation by days; OK for your use-case)
window_end = app_date  # inclusive for our day-counting logic
window_12m_start = app_date - timedelta(days=365)
window_5y_start = app_date - timedelta(days=5 * 365)

abs_12m = count_absences_in_window(trips_df, window_12m_start, window_end)
abs_5y = count_absences_in_window(trips_df, window_5y_start, window_end)

# Presence 5 years ago check:
# Must be physically in the UK exactly 5 years before application date.
# (We use 5*365 days as a practical approximation; good enough for now.)
five_years_ago_day = app_date - timedelta(days=5 * 365)
present_5y_ago = is_in_uk_on_day(trips_df, five_years_ago_day)

# Criteria + ticks
OK_12M = abs_12m <= 90
OK_5Y = abs_5y <= 450

def tick(ok: bool) -> str:
    return "✅" if ok else "❌"

c1, c2, c3 = st.columns(3)
c1.metric("Last 12 months", f"{abs_12m} days", help="Typical guideline: 90 days max")
c2.metric("Last 5 years", f"{abs_5y} days", help="Typical guideline: 450 days max")
c3.metric("In UK 5 years ago?", f"{tick(present_5y_ago)}", help=f"Checked for: {uk_fmt(five_years_ago_day)}")

st.markdown(
    f"""
**Eligibility signals (common rules):**
- {tick(OK_12M)} **≤ 90 days** absent in last 12 months  
- {tick(OK_5Y)} **≤ 450 days** absent in last 5 years  
- {tick(present_5y_ago)} **In the UK on {uk_fmt(five_years_ago_day)}** (5 years before application)
"""
)

st.divider()

st.subheader("Trips (latest first)")

show = trips_df[["start_date", "end_date", "days_absent", "note"]].copy()
show["start_date"] = show["start_date"].apply(uk_fmt)
show["end_date"] = show["end_date"].apply(uk_fmt)

st.dataframe(show, width="stretch", hide_index=True)

st.caption(
    "Absence days per trip are computed using Form AN guidance: "
    "whole days abroad only (exclude departure and return days)."
)