"""
smard_data_loader.py — Local SMARD CSV Parser
==============================================
Loads the SMARD quarter-hourly generation and demand CSV files that
were downloaded externally and placed in data/.

File formats (semicolon-separated):

  Germany_generation_data_2025.csv
    Start date;End date;Biomass [MWh] Original resolutions;...
    Jan 1, 2025 12:00 AM;Jan 1, 2025 12:15 AM;990.50;...

  Germany_demand_2025_quarterhour.csv
    Start date;End date;grid load [MWh] Original resolutions;Residual load ...
    Jan 1, 2025 12:00 AM;Jan 1, 2025 12:15 AM;11,470.25;962.25

Conventions in raw files:
  - Thousands separator: comma  →  "11,470.25" = 11470.25 MWh
  - Missing values: "-"          →  replaced with NaN
  - Units: MWh per 15-min interval  →  multiply by 4 to get average MW

Output: pandas DataFrames at hourly resolution (MW), UTC-indexed.

This module is imported by build_pypsa_network.py and run_pypsa_market_sim.py.
The SMARD API download functions are retained below but not called by the
main pipeline; they are available if you need to fetch additional years.
"""

import os
import re
import pandas as pd
import numpy as np
from typing import Optional

# ── Date parser ───────────────────────────────────────────────────────────────
# SMARD dates: "Jan 1, 2025 12:00 AM"  →  German local time (CET/CEST)
_DATE_FORMAT = "%b %d, %Y %I:%M %p"


def _parse_smard_dates(series: pd.Series) -> pd.DatetimeIndex:
    """Parse SMARD date strings to timezone-aware DatetimeIndex (UTC)."""
    # Strip extra whitespace
    series = series.str.strip()
    dt = pd.to_datetime(series, format=_DATE_FORMAT)
    # SMARD timestamps are in Europe/Berlin local time
    dt = dt.dt.tz_localize("Europe/Berlin", ambiguous="infer",
                            nonexistent="shift_forward")
    return dt.dt.tz_convert("UTC")


def _parse_numeric(series: pd.Series) -> pd.Series:
    """
    Parse SMARD numeric column:
      - Remove thousands comma: "11,470.25" → "11470.25"
      - Replace "-" with NaN
      - Cast to float
    """
    s = series.astype(str).str.strip()
    s = s.str.replace(",", "", regex=False)   # thousands separator
    s = s.replace("-", np.nan)
    s = s.replace("–", np.nan)
    s = s.replace("", np.nan)
    return pd.to_numeric(s, errors="coerce")


def _clean_column_name(col: str) -> str:
    """
    Strip unit suffix and normalise column names.
    'Biomass [MWh] Original resolutions' → 'Biomass'
    'grid load [MWh] Original resolutions' → 'grid load'
    """
    col = col.strip()
    col = re.sub(r"\s*\[.*?\].*$", "", col)   # remove '[MWh] ...'
    return col.strip()


# ── Generation loader ─────────────────────────────────────────────────────────

def load_generation(path: str,
                    resample_to_hourly: bool = True) -> pd.DataFrame:
    """
    Load SMARD generation CSV, resample to hourly average MW.

    Parameters
    ----------
    path : str
        Path to Germany_generation_data_2025.csv (or similar)
    resample_to_hourly : bool
        If True (default), resample 15-min MWh → hourly MW.
        If False, return 15-min data in MW (×4 conversion applied).

    Returns
    -------
    pd.DataFrame
        Index: UTC DatetimeIndex (hourly or 15-min)
        Columns: technology names (e.g. "Biomass", "Wind onshore", ...)
        Values: MW (average power in the interval)
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Generation CSV not found: {path}\n"
            "Place the SMARD download at this path and re-run."
        )

    raw = pd.read_csv(path, sep=";", encoding="utf-8-sig", dtype=str)

    # Clean column names
    raw.columns = [_clean_column_name(c) for c in raw.columns]

    # Parse timestamps from 'Start date' column
    if "Start date" not in raw.columns:
        raise ValueError(
            f"Expected 'Start date' column, found: {list(raw.columns[:5])}"
        )
    timestamps = _parse_smard_dates(raw["Start date"])

    # Parse all numeric columns (skip Start date, End date)
    data_cols = [c for c in raw.columns
                 if c not in ("Start date", "End date")]

    df = pd.DataFrame(index=timestamps)
    for col in data_cols:
        df[col] = _parse_numeric(raw[col]).values

    # Remove duplicate timestamps (e.g., DST overlap hour)
    df = df[~df.index.duplicated(keep="first")]
    df = df.sort_index()

    # Convert MWh/15min → MW (average power)
    df = df * 4

    if resample_to_hourly:
        df = df.resample("1h").mean()
        df = _impute_missing(df, label="generation")

    return df


# ── Demand loader ─────────────────────────────────────────────────────────────

def load_demand(path: str,
                resample_to_hourly: bool = True) -> pd.Series:
    """
    Load SMARD demand CSV, return hourly grid load in MW.

    Parameters
    ----------
    path : str
        Path to Germany_demand_2025_quarterhour.csv
    resample_to_hourly : bool
        Resample to hourly if True (default).

    Returns
    -------
    pd.Series
        Index: UTC DatetimeIndex (hourly)
        Values: MW  (total German grid load)
        Name: "grid load"
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Demand CSV not found: {path}\n"
            "Place the SMARD download at this path and re-run."
        )

    raw = pd.read_csv(path, sep=";", encoding="utf-8-sig", dtype=str)
    raw.columns = [_clean_column_name(c) for c in raw.columns]

    timestamps = _parse_smard_dates(raw["Start date"])

    # Grid load column
    load_col = next(
        (c for c in raw.columns if "grid load" in c.lower()), None
    )
    if load_col is None:
        # Try common alternatives
        load_col = next(
            (c for c in raw.columns
             if any(k in c.lower() for k in ["load", "verbrauch", "nachfrage"])),
            None
        )
    if load_col is None:
        raise ValueError(
            f"Cannot find grid load column. Available: {list(raw.columns)}"
        )

    series = pd.Series(
        _parse_numeric(raw[load_col]).values,
        index=timestamps,
        name="grid load"
    )
    series = series[~series.index.duplicated(keep="first")].sort_index()

    # MWh/15min → MW
    series = series * 4

    if resample_to_hourly:
        series = series.resample("1h").mean()
        series = _impute_missing(series, label="demand")

    return series


# ── Helpers ───────────────────────────────────────────────────────────────────

def _impute_missing(data, label: str = ""):
    """
    Forward-fill up to 2 hours (sensor dropout), then linear interpolation.
    Reports NaN statistics before and after.
    """
    if isinstance(data, pd.DataFrame):
        n_nan_before = data.isna().sum().sum()
    else:
        n_nan_before = data.isna().sum()

    data = data.ffill(limit=2).interpolate(method="linear")

    if isinstance(data, pd.DataFrame):
        n_nan_after = data.isna().sum().sum()
    else:
        n_nan_after = data.isna().sum()

    if n_nan_before > 0:
        print(f"  [{label}] Imputed {n_nan_before} NaN values "
              f"→ {n_nan_after} remaining")
    return data


def build_smard_bundle(generation_path: str,
                       demand_path: str) -> dict:
    """
    Load both files, align on a common hourly UTC index, and return
    a dict with keys 'generation' (DataFrame) and 'demand' (Series).
    """
    print("  Loading SMARD generation data ...")
    gen = load_generation(generation_path)
    print(f"    Shape: {gen.shape}  |  "
          f"{gen.index[0]} → {gen.index[-1]}")

    print("  Loading SMARD demand data ...")
    dem = load_demand(demand_path)
    print(f"    Length: {len(dem)}  |  "
          f"{dem.index[0]} → {dem.index[-1]}")

    # Align on common index
    common_idx = gen.index.intersection(dem.index)
    gen = gen.reindex(common_idx)
    dem = dem.reindex(common_idx)

    missing_pct_gen = gen.isna().sum().sum() / gen.size * 100
    missing_pct_dem = dem.isna().sum() / len(dem) * 100
    print(f"  Common index: {len(common_idx)} hours")
    print(f"  Missing data: generation {missing_pct_gen:.2f}%  |  "
          f"demand {missing_pct_dem:.2f}%")

    return {"generation": gen, "demand": dem, "index": common_idx}


# ── Validation ────────────────────────────────────────────────────────────────

def validate_smard_bundle(bundle: dict) -> None:
    """
    Sanity checks on the loaded SMARD data.
    These checks are independent of RUN_MODE and always available.
    """
    gen = bundle["generation"]
    dem = bundle["demand"]

    print("\n── SMARD Data Validation ──────────────────────────────────────────")

    # 1. Temporal coverage: expect ~8760 hours for 2025 (non-leap year)
    n_hours = len(bundle["index"])
    expected = 8760
    print(f"  Temporal coverage : {n_hours} h  (expected {expected})")
    if n_hours < expected * 0.99:
        print(f"  [WARNING] Coverage is {n_hours/expected*100:.1f}% of full year")

    # 2. Peak load plausibility (Germany peak ~70–85 GW in 2025)
    peak_load = dem.max()
    print(f"  Peak grid load    : {peak_load/1e3:.1f} GW  (expected 70–85 GW)")
    if not (60_000 < peak_load < 95_000):
        print(f"  [WARNING] Peak load {peak_load:.0f} MW outside expected range")

    # 3. Annual load energy
    annual_load_twh = dem.sum() / 1e6
    print(f"  Annual load       : {annual_load_twh:.1f} TWh  (expected ~480–510 TWh)")

    # 4. Wind generation peak (onshore + offshore)
    wind_cols = [c for c in gen.columns if "wind" in c.lower()]
    if wind_cols:
        peak_wind = gen[wind_cols].sum(axis=1).max()
        print(f"  Peak wind (total) : {peak_wind/1e3:.1f} GW")

    # 5. Solar peak (expect ~50–65 GW in 2025)
    solar_cols = [c for c in gen.columns if "photo" in c.lower()
                  or "solar" in c.lower() or "pv" in c.lower()]
    if solar_cols:
        peak_solar = gen[solar_cols].sum(axis=1).max()
        print(f"  Peak solar        : {peak_solar/1e3:.1f} GW  (expected 50–70 GW)")

    # 6. NaN fraction after imputation
    nan_frac = gen.isna().sum().sum() / gen.size
    print(f"  NaN fraction (gen): {nan_frac*100:.3f}%  (must be < 1%)")
    if nan_frac > 0.01:
        print("  [WARNING] High NaN fraction — check input CSV completeness")

    # 7. Column inventory
    print(f"\n  Generation columns ({len(gen.columns)}):")
    for col in gen.columns:
        annual_twh = gen[col].sum() / 1e6
        peak_gw    = gen[col].max() / 1e3
        print(f"    {col:<30s}: annual {annual_twh:5.1f} TWh | "
              f"peak {peak_gw:5.1f} GW")

    print("\n  ✓ Validation complete")


# ── SMARD API (retained for optional future use) ─────────────────────────────
# The functions below are NOT called by the main pipeline.
# They remain available for fetching data for other years.

def _smard_api_fetch_year(filter_id: int, year: int) -> pd.Series:
    """
    [OPTIONAL] Fetch a single SMARD time series via API.
    Not used in the main pipeline; retained for utility.
    """
    import requests, time as _time

    SMARD_BASE = "https://www.smard.de/app/chart_data"
    REGION = "DE"

    index_url = (f"{SMARD_BASE}/{filter_id}/{REGION}/quarterhour"
                 f"/index_{REGION}.json")
    resp = requests.get(index_url, timeout=30)
    resp.raise_for_status()
    timestamps = resp.json()["timestamps"]

    year_start = pd.Timestamp(f"{year}-01-01", tz="Europe/Berlin")
    year_end   = pd.Timestamp(f"{year+1}-01-01", tz="Europe/Berlin")

    records = []
    for ts in timestamps:
        ts_dt = pd.Timestamp(ts, unit="ms", tz="UTC").tz_convert("Europe/Berlin")
        if not (year_start <= ts_dt < year_end):
            continue
        url = (f"{SMARD_BASE}/{filter_id}/{REGION}/quarterhour"
               f"/{filter_id}_{REGION}_quarterhour_{ts}.json")
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            records.extend(r.json().get("series", []))
            _time.sleep(0.15)
        except Exception as e:
            print(f"    API chunk {ts} failed: {e}")

    if not records:
        return pd.Series(dtype=float)

    df = pd.DataFrame(records, columns=["epoch_ms", "value"])
    df["ts"] = pd.to_datetime(df["epoch_ms"], unit="ms", utc=True)
    df = df.drop_duplicates("ts").set_index("ts").sort_index()
    return df["value"] * 4   # MWh/15min → MW


# ── Standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pypsa_config import GENERATION_CSV, DEMAND_CSV

    print("smard_data_loader.py — standalone test")
    print("=" * 55)
    bundle = build_smard_bundle(GENERATION_CSV, DEMAND_CSV)
    validate_smard_bundle(bundle)