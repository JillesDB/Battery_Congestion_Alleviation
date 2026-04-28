"""
congestion_cost_alleviation.py
================================
GridBooster Battery — Congestion Cost Alleviation Calculator

Methodology
-----------
The Kupferzell GridBooster (250 MW / 500 MWh, TransnetBW / Fluence 2025) is
classified as a *Fully Integrated Network Component* (FINC) under Article 54 of
EU Directive 2019/944 and operates exclusively as a transmission asset: it
maintains a pre-defined state of charge on standby and never cycles in normal
operation.

Physical mechanism — *virtual transmission*
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Under the N-1 security criterion, a transmission line with thermal rating
S_nom [MW] is normally dispatched at most to its N-1 secure thermal limit
(which, in the PyPSA-EUR formulation used here, IS s_nom; the 2% threshold
CONGESTION_THRESHOLD = 0.98 is a numerical-precision guard only).

When a battery of power capacity P_bat [MW] is co-located at the bottleneck
bus and declared as a contingency-reserve asset, the TSO may raise the active
dispatch ceiling for adjacent corridor lines to

    S_eff = S_nom + α · P_bat                                              (1)

where α ∈ (0, 1] is the *virtual-transmission multiplier* — the PTDF-weighted
fraction of battery power that directly offloads the constrained line.  The
battery never physically discharges; instead, its guaranteed millisecond-
response to an N-1 trip provides the security headroom that allows the line to
carry more power in the pre-contingency state (Fluence 2020; Consentec /
Fluence 2023).

For the Kupferzell NW→SE corridor (lines connecting the 380 kV Kupferzell
substation to Großgartach, Gönnheim and the greater Stuttgart/Heilbronn
backbone), α ≈ 0.9–1.0 based on the corridor's radial topology.  The default
value α = 1.0 (full 1:1 virtual capacity expansion) is conservative in the
sense of not overcounting.

Calculation per hour t
~~~~~~~~~~~~~~~~~~~~~~~
Step 1 – identify congested lines
    For each corridor line l in hour t:
        congested_{l,t} = 1  if  loading_fraction_{l,t} ≥ CONGESTION_THRESHOLD
                          0  otherwise

Step 2 – compute per-line congestion overload
    overload_{l,t} = max(0,  loading_fraction_{l,t} · S_nom_l  −
                             CONGESTION_THRESHOLD · S_nom_l)         [MW]
    This is the power by which line l exceeds the security dispatch ceiling.
    It measures the *volume of congestion* that redispatch must correct.

Step 3 – aggregate across corridor lines
    total_overload_t = Σ_l  overload_{l,t}                           [MW]
    This is the total corridor congestion volume that must be redispatched.

Step 4 – battery capacity expansion
    ΔC_t = min(α · P_bat,  total_overload_t)                         [MW]
    The battery can relieve at most α · P_bat MW of overload.  If the
    overload is smaller than this (mildly congested hours), the battery
    only removes the actual excess — no overcounting of zero-cost headroom.
    We preserve ΔC_t = 0 for uncongested hours (no binding constraint,
    no avoided cost).

Step 5 – unit congestion cost mapping
    Monthly average redispatch unit costs c̄_m [EUR / MWh] are mapped onto
    every hour t belonging to month m.  The monthly cost is sourced from the
    SMARD "Realisierte Erzeugung: Redispatch" statistics.

    Two input modes are supported (see `cost_mode` parameter):
    • "unit_cost"    — input dict values are already in EUR / MWh.
                       Hourly unit cost c_t = c̄_m(t).
    • "total_monthly" — input dict values are total monthly redispatch costs
                        in EUR.  The script converts:
                            c̄_m = total_EUR_m / congested_MWh_m
                        where congested_MWh_m is the sum of all overload_MWh
                        observed in that month.  Falls back to a uniform
                        EUR/MWh estimate if congested volume is near-zero.

Step 6 – cost alleviation
    CA_t = ΔC_t · c̄_m(t)                                     [EUR / hour]
    This is the avoided redispatch cost in hour t.

Output columns appended to the hourly line-loading data
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    overload_mw            — per-line overload above threshold [MW]
    total_corridor_overload_mw — corridor-aggregated overload [MW]
    battery_cap_expansion_mw   — effective ΔC_t = min(α·P_bat, overload) [MW]
    hourly_unit_cost_eur_mwh   — c̄_m(t) from the monthly cost dict [EUR/MWh]
    battery_cost_alleviation_eur — CA_t = ΔC_t · c̄_m(t) [EUR]

References
----------
Consentec / Fluence (2023).  "Improving project economics of Grid Booster
    batteries by combining rate-based and market-based revenues on Storage as
    Transmission Assets."  Final report.
Fluence (2020).  "Building Virtual Transmission: Critical Elements of Energy
    Storage for Network Services."  White paper.
Frysztacki et al. (2021).  "The strong effect of network resolution on
    electricity system models with high shares of wind and solar."
    Applied Energy 291, 116726.
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from plotting import plot_congestion_alleviation_map

try:
    import pypsa
except ImportError:  # pragma: no cover - optional for pure CSV workflows
    pypsa = None

# ─── Default study constants (override via CLI or function arguments) ──────────
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_BATTERY_MW:      float = 250.0   # Kupferzell GridBooster rated power [MW]
DEFAULT_ALPHA:           float = 1.0     # Virtual-transmission multiplier α
DEFAULT_CONGESTION_THRESHOLD: float = 0.98  # Match PyPSA solve threshold
DEFAULT_REDISPATCH_COST_YEAR: str = "mean"
DUAL_TOL: float = 1e-3  # Minimum |μ| [EUR/MWh] to classify a line-hour as congested
REDISPATCH_COSTS_CSV = PROJECT_DIR / "data" / "redispatch_monthly_costs.csv"
REDISPATCH_COST_YEAR_CHOICES = ("2022", "2023", "2024", "2025", "mean")
DEFAULT_MINIMUM_VOLTAGE: float = 0.0
PYPSA_EUR_DIR = PROJECT_DIR.parent / "pypsa-eur"

# SMARD monthly average redispatch unit costs (EUR / MWh), calendar year 2023.
# Source: BNetzA / SMARD "Realisierte Erzeugung Redispatch" published statistics.
# These are PLACEHOLDERS — replace with data downloaded from
#   https://www.smard.de/home/marktdaten?marketDataAttributes=…
# or from the BNetzA annual redispatch report.
# Keys: integer month number 1–12.  Values: EUR / MWh.
DEFAULT_MONTHLY_REDISPATCH_COSTS: dict[int, float] = {
    1:  85.0,   # January
    2:  78.0,   # February
    3:  72.0,   # March
    4:  65.0,   # April
    5:  58.0,   # May
    6:  52.0,   # June
    7:  55.0,   # July
    8:  60.0,   # August
    9:  68.0,   # September
    10: 75.0,   # October
    11: 82.0,   # November
    12: 90.0,   # December
}

# ─── Input column schemas ──────────────────────────────────────────────────────
# The two postprocessing scripts produce slightly different column names.
# We normalise them all into a canonical set on load.
_CANONICAL_COLS = {
    # research_workflow.py / pypsa_count_congestion_occurrence.py
    "loading_fraction":  "loading_fraction",
    "s_nom_mw":          "s_nom_mw",
    "line":              "line_id",          # rename → line_id
    "timestamp":         "timestamp",
    # postprocess_congestion.py (kupferzell_hourly_utilisation.csv)
    "loading_pu":        "loading_fraction", # rename → loading_fraction
    "line_id":           "line_id",
    "p0_abs_mw":         "p0_abs_mw",
    "margin_to_limit_mw":"margin_to_limit_mw",
}


# ══════════════════════════════════════════════════════════════════════════════
# I/O HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _detect_results_dir(base_dir: Path | None) -> Path:
    """
    Walk upward from the script location to find a ``results/`` folder, then
    list run-config subfolders.  Returns the best candidate or ``base_dir``.
    """
    if base_dir is not None and base_dir.exists():
        return base_dir

    candidates = [
        Path("results"),
        Path("outputs"),
        Path("../results"),
        Path("../outputs"),
    ]
    for c in candidates:
        if c.is_dir():
            return c.resolve()
    return Path(".").resolve()


def _is_raw_occurrence_csv(csv_path: Path) -> bool:
    """Return True only for raw congestion-occurrence hourly CSVs."""
    name = csv_path.name
    return (
        name.startswith("kupferzell_line_proximity_hourly_")
        and "battery" not in name
        and "monthly_summary" not in name
        and "_kpi" not in name
        and name.endswith(".csv")
    )


def _default_congestion_alleviation_dir(csv_path: Path) -> Path:
    """Return the sibling congestion_alleviation folder for a raw occurrence CSV."""
    if csv_path.parent.name == "congestion_occurrence":
        return csv_path.parent.with_name("congestion_alleviation")
    return csv_path.parent / "congestion_alleviation"


def _infer_scenario_from_csv_path(csv_path: Path) -> str:
    text = str(csv_path).lower()
    parts = [p.lower() for p in csv_path.parts]
    for part in parts:
        if "kupferzell" in part and "simple" in part:
            return "kupferzell_simple"
        if "kupferzell" in part and "full" in part:
            return "kupferzell_full"
    if "kupferzell" in text and "simple" in text:
        return "kupferzell_simple"
    if "kupferzell" in text and "full" in text:
        return "kupferzell_full"
    return "default"


def _discover_network_path(csv_path: Path, network_path: Path | None = None) -> Path | None:
    """Resolve solved network path used for plotting line-level maps."""
    if network_path is not None:
        if network_path.exists():
            return network_path
        warnings.warn(f"Provided --network path not found: {network_path}", stacklevel=2)
        return None

    scenario = _infer_scenario_from_csv_path(csv_path)
    roots = [PYPSA_EUR_DIR / "results", PYPSA_EUR_DIR / "resources"]
    patterns = {
        "kupferzell_simple": "*kupferzell*simple*/networks/base_s_*_elec_.nc",
        "kupferzell_full": "*kupferzell*full*/networks/base_s_*_elec_.nc",
        "default": "*/networks/base_s_*_elec_.nc",
    }
    pattern = patterns.get(scenario, patterns["default"])

    candidates: list[Path] = []
    for root in roots:
        if root.exists():
            candidates.extend(sorted(root.glob(pattern)))

    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _save_alleviation_map(
    df: pd.DataFrame,
    out_csv: Path,
    network_path: Path | None,
    minimum_voltage: float,
) -> Path | None:
    """Generate a congestion-alleviation map aligned with the occurrence-map style."""
    if network_path is None:
        warnings.warn(
            "Could not resolve solved network path for alleviation map; skipping map output.",
            stacklevel=2,
        )
        return None

    if pypsa is None:
        warnings.warn(
            "pypsa is not available in this environment; skipping alleviation map output.",
            stacklevel=2,
        )
        return None

    n = pypsa.Network(network_path)
    line_saved_cost = (
        df.groupby("line_id", as_index=True)["battery_cost_alleviation_eur"]
        .sum()
        .astype(float)
    )

    map_path = out_csv.with_name(f"figure_{out_csv.stem}_congestion_alleviation_map.png")
    plot_congestion_alleviation_map(
        line_saved_cost_eur=line_saved_cost,
        buses=n.buses,
        lines=n.lines,
        output_path=str(map_path),
        minimum_voltage=minimum_voltage,
    )
    return map_path


def _month_label_to_number(label: str) -> int:
    text = str(label).strip().lower()
    if text.isdigit():
        month = int(text)
        if 1 <= month <= 12:
            return month

    month_map = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    short = text[:3]
    if short in month_map:
        return month_map[short]
    raise ValueError(f"Unrecognised month label in redispatch cost CSV: {label!r}")


def _resolve_cost_column(df: pd.DataFrame, cost_source: str) -> str:
    source = str(cost_source).strip().lower()
    if source not in REDISPATCH_COST_YEAR_CHOICES:
        raise ValueError(
            f"redispatch_cost_year must be one of {REDISPATCH_COST_YEAR_CHOICES}, got {cost_source!r}"
        )

    normalized = {str(col).strip().lower(): col for col in df.columns}
    if source == "mean":
        candidates = ["mean", "mean_2023_2025", "mean 2023-2025", "mean2023-2025"]
    else:
        candidates = [source, f"{source} eur/mwh", f"{source} eur", f"{source}_eur/mwh"]

    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]

    matches = [orig for key, orig in normalized.items() if source in key]
    if len(matches) == 1:
        return matches[0]

    raise ValueError(
        f"Could not find a redispatch cost column for source {cost_source!r} in {list(df.columns)}"
    )


def load_monthly_redispatch_costs(
    cost_source: str = DEFAULT_REDISPATCH_COST_YEAR,
    csv_path: Path | str = REDISPATCH_COSTS_CSV,
) -> dict[int, float]:
    """Load monthly redispatch unit costs from the CSV dataset."""
    csv_path = Path(csv_path)
    if not csv_path.exists():
        warnings.warn(
            f"Redispatch cost CSV not found at {csv_path}; falling back to built-in defaults.",
            UserWarning,
            stacklevel=2,
        )
        return dict(DEFAULT_MONTHLY_REDISPATCH_COSTS)

    df = pd.read_csv(csv_path)
    month_col = next((c for c in df.columns if str(c).strip().lower() == "month"), None)
    if month_col is None:
        raise ValueError(f"Redispatch cost CSV {csv_path} must contain a 'month' column.")

    df = df.copy()
    df["month"] = df[month_col].map(_month_label_to_number)

    selected_col = _resolve_cost_column(df, cost_source)
    selected = pd.to_numeric(df[selected_col], errors="coerce")

    year_cols = [c for c in df.columns if str(c).strip() in {"2022", "2023", "2024", "2025"}]
    if str(cost_source).strip().lower() == "mean" and selected.isna().any() and year_cols:
        year_values = df[year_cols].apply(pd.to_numeric, errors="coerce")
        selected = selected.fillna(year_values.mean(axis=1))
    elif selected.isna().any():
        mean_col = None
        for candidate in ("mean", "mean_2023_2025", "mean 2023-2025", "mean2023-2025"):
            if candidate in {str(c).strip().lower() for c in df.columns}:
                mean_col = _resolve_cost_column(df, "mean")
                break
        if mean_col is not None:
            selected = selected.fillna(pd.to_numeric(df[mean_col], errors="coerce"))
        if selected.isna().any() and year_cols:
            year_values = df[year_cols].apply(pd.to_numeric, errors="coerce")
            selected = selected.fillna(year_values.mean(axis=1))

    if selected.isna().any():
        legacy_fallback = pd.Series(DEFAULT_MONTHLY_REDISPATCH_COSTS)
        selected = selected.fillna(df["month"].map(legacy_fallback))

    if selected.isna().any():
        missing_months = sorted(df.loc[selected.isna(), "month"].unique().tolist())
        raise ValueError(
            f"Redispatch cost CSV {csv_path} still has missing months after fallback: {missing_months}"
        )

    return {int(month): float(value) for month, value in zip(df["month"], selected)}


def _find_hourly_csv(results_root: Path) -> list[Path]:
    """
    Recursively find Kupferzell hourly utilisation CSV files under results_root.
    Matches both naming conventions from the two postprocessing scripts.
    """
    patterns = [
        "kupferzell_line_proximity_hourly_*.csv",
    ]
    found = []
    for pat in patterns:
        found.extend(p for p in results_root.rglob(pat) if _is_raw_occurrence_csv(p))
    return sorted(set(found))


def load_hourly_line_loading(csv_path: Path) -> pd.DataFrame:
    """
    Load and normalise the Kupferzell corridor hourly line-loading CSV.

    Handles both schemas:
    • ``kupferzell_line_proximity_hourly_{year}.csv``
      (from research_workflow.py / pypsa_count_congestion_occurrence.py)
      Columns: timestamp, line, bus0, bus1, s_nom_mw, loading_fraction,
               distance_to_capacity_fraction, at_or_above_capacity_threshold

    • ``kupferzell_hourly_utilisation.csv``
      (from archive/legacy_scripts/postprocess_congestion.py)
      Columns: timestamp, line_id, bus0, bus1, s_nom_mw, p0_mw, p0_abs_mw,
               loading_pu, loading_pct, margin_to_limit_mw, congested

    Returns a normalised DataFrame with at minimum:
        timestamp        — pd.Timestamp
        line_id          — str, transmission line identifier
        s_nom_mw         — float, thermal rating [MW]
        loading_fraction — float, |p0| / s_nom ∈ [0, ∞)
    """
    df = pd.read_csv(csv_path, dtype=str)

    # ── Column normalisation ───────────────────────────────────────────────
    rename = {}
    if "line" in df.columns and "line_id" not in df.columns:
        rename["line"] = "line_id"
    if "loading_pu" in df.columns and "loading_fraction" not in df.columns:
        rename["loading_pu"] = "loading_fraction"
    if rename:
        df = df.rename(columns=rename)

    required = {"timestamp", "line_id", "s_nom_mw", "loading_fraction"}
    missing  = required - set(df.columns)
    if missing:
        raise ValueError(
            f"CSV {csv_path.name} is missing required columns: {missing}\n"
            f"Available columns: {list(df.columns)}"
        )

    # ── Type coercion ──────────────────────────────────────────────────────
    df["timestamp"]        = pd.to_datetime(df["timestamp"])
    df["s_nom_mw"]         = pd.to_numeric(df["s_nom_mw"],         errors="coerce")
    df["loading_fraction"] = pd.to_numeric(df["loading_fraction"], errors="coerce")

    # Optional convenience columns
    for col in ("p0_abs_mw", "margin_to_limit_mw", "loading_pct",
                "bus0", "bus1", "at_or_above_capacity_threshold"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce") \
                if col not in ("bus0", "bus1") else df[col]

    df = df.dropna(subset=["timestamp", "s_nom_mw", "loading_fraction"])
    df = df.sort_values(["timestamp", "line_id"]).reset_index(drop=True)

    print(f"  Loaded {len(df):,} rows from {csv_path.name}")
    print(f"    Lines : {df['line_id'].nunique()} corridor lines")
    print(f"    Period: {df['timestamp'].min().date()} → {df['timestamp'].max().date()}")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# MONTHLY COST HANDLING
# ══════════════════════════════════════════════════════════════════════════════

def _build_unit_cost_map(
    monthly_costs: dict[int, float],
    df:            pd.DataFrame,
    cost_mode:     str,
    threshold:     float,
) -> dict[tuple[int, int], float]:
    """
    Convert the monthly cost dictionary into a mapping
    {(year, month) → EUR/MWh unit redispatch cost}.

    Parameters
    ----------
    monthly_costs : dict
        Keys: integer month (1–12) or (year, month) tuple.
        Values: EUR/MWh (if cost_mode=="unit_cost") OR
                total EUR for that month (if cost_mode=="total_monthly").
    df : pd.DataFrame
        Normalised hourly corridor loading (used only in total_monthly mode
        to compute the observed congested MWh per month).
    cost_mode : {"unit_cost", "total_monthly"}
        How to interpret the monthly cost values.
    threshold : float
        Congestion threshold (same value used in the LOPF solve).

    Returns
    -------
    dict mapping (year, month) → float [EUR / MWh]
    """
    # Determine year(s) present in the loading data
    years = df["timestamp"].dt.year.unique().tolist()

    # Normalise keys: allow plain int month or (year, month) tuple
    def _normalise_key(k):
        if isinstance(k, (tuple, list)):
            return tuple(k)
        return None, int(k)  # month-only key → year=None (applies to all years)

    raw = {_normalise_key(k): v for k, v in monthly_costs.items()}

    unit_map: dict[tuple[int, int], float] = {}

    if cost_mode == "unit_cost":
        # Values are already EUR/MWh — broadcast across all observed years
        for (yr, mo), cost_val in raw.items():
            target_years = years if yr is None else [yr]
            for y in target_years:
                unit_map[(y, mo)] = float(cost_val)

    elif cost_mode == "total_monthly":
        # Compute congested MWh per (year, month) from the loading data
        df_tmp = df.copy()
        df_tmp["year"]  = df_tmp["timestamp"].dt.year
        df_tmp["month"] = df_tmp["timestamp"].dt.month
        df_tmp["overload_mw"] = np.maximum(
            0.0,
            df_tmp["loading_fraction"] * df_tmp["s_nom_mw"]
            - threshold * df_tmp["s_nom_mw"]
        )
        # Each row is one 15-min or 1-hour slot; assume hourly (Δt = 1h)
        # 15-min data: multiply by 0.25.  We infer from median gap between rows.
        timestamps_sorted = df_tmp["timestamp"].drop_duplicates().sort_values()
        if len(timestamps_sorted) > 1:
            median_gap_min = (timestamps_sorted.diff().dropna()
                              .dt.total_seconds().median() / 60)
        else:
            median_gap_min = 60.0
        dt_h = median_gap_min / 60.0  # time-step in hours

        grp = (df_tmp.groupby(["year", "month"])["overload_mw"]
               .sum() * dt_h)         # → congested MWh per (year, month)

        for (yr, mo), total_eur in raw.items():
            target_years = years if yr is None else [yr]
            for y in target_years:
                congested_mwh = grp.get((y, mo), 0.0)
                if congested_mwh > 1.0:  # at least 1 MWh observed
                    unit_map[(y, mo)] = float(total_eur) / congested_mwh
                else:
                    # No observed congestion in this month: store zero cost
                    # (battery alleviation will also be zero, so result is consistent)
                    unit_map[(y, mo)] = 0.0
    else:
        raise ValueError(
            f"cost_mode must be 'unit_cost' or 'total_monthly', got '{cost_mode}'"
        )

    return unit_map


def _map_hourly_unit_cost(
    df:         pd.DataFrame,
    unit_map:   dict[tuple[int, int], float],
) -> pd.Series:
    """
    Map the (year, month) → EUR/MWh lookup onto every row of df.
    Rows with no matching entry receive NaN (flagged for the user).

    Returns a pd.Series aligned to df.index.
    """
    year_month = list(zip(df["timestamp"].dt.year, df["timestamp"].dt.month))
    costs = pd.array([unit_map.get(ym, np.nan) for ym in year_month], dtype=float)
    n_missing = np.isnan(costs).sum()
    if n_missing > 0:
        missing_ym = sorted({ym for ym, c in zip(year_month, costs) if np.isnan(c)})
        warnings.warn(
            f"{n_missing} rows have no matching monthly cost entry for: {missing_ym}.\n"
            f"Battery_Cost_Alleviation will be NaN for those rows.  "
            f"Add the corresponding entries to monthly_costs.",
            UserWarning, stacklevel=2
        )
    return pd.Series(costs, index=df.index, name="hourly_unit_cost_eur_mwh")


# ══════════════════════════════════════════════════════════════════════════════
# CORE CALCULATION
# ══════════════════════════════════════════════════════════════════════════════

def compute_cost_alleviation(
    df:                  pd.DataFrame,
    battery_mw:          float = DEFAULT_BATTERY_MW,
    alpha:               float = DEFAULT_ALPHA,
    congestion_threshold: float = DEFAULT_CONGESTION_THRESHOLD,
    monthly_costs:       dict[int, float] = None,
    cost_mode:           str  = "unit_cost",
) -> pd.DataFrame:
    """
    Compute the hourly congestion cost alleviation of the GridBooster battery.

    Parameters
    ----------
    df : pd.DataFrame
        Normalised hourly corridor line-loading data (from load_hourly_line_loading).
        May contain multiple rows per timestamp (one per corridor line).
    battery_mw : float
        Rated power capacity of the battery [MW].  Default: 250 MW (Kupferzell).
    alpha : float
        Virtual-transmission multiplier α ∈ (0, 1].  Fraction of battery power
        that translates to expanded line dispatch ceiling.  Default: 1.0.
    congestion_threshold : float
        Fraction of s_nom above which a line is considered congested.
        Must match the threshold used in the PyPSA solve.  Default: 0.98.
    monthly_costs : dict
        Monthly redispatch cost dict.  Keys: int month 1–12 (or (year, month)
        tuples for multi-year datasets).  Values: EUR/MWh (if cost_mode is
        "unit_cost") or total EUR/month (if cost_mode is "total_monthly").
    cost_mode : str
        "unit_cost" or "total_monthly" — see _build_unit_cost_map docstring.

    Returns
    -------
    pd.DataFrame
        One row per (timestamp × line_id).  Original columns are preserved;
        the following columns are appended:

        overload_mw
            Per-line overload above congestion_threshold [MW].
            = max(0, loading_fraction × s_nom_mw − threshold × s_nom_mw)
            Zero for uncongested hours / lines.

        total_corridor_overload_mw
            Sum of overload_mw across all corridor lines in the same timestamp.
            Same value repeated for all lines in that timestamp.
            This is the *volume of congestion* the battery must relieve.

        battery_cap_expansion_mw
            Per-line battery capacity expansion allocated to line l in hour t
            [MW], computed by pro-rata allocation:
                corridor_expansion = min(α·P_bat, total_corridor_overload_mw)
                battery_cap_expansion_{l,t}
                    = overload_{l,t} / total_corridor_overload_mw
                      × corridor_expansion
            Zero for uncongested lines/hours.
            Summing this column across all lines in a given timestamp
            recovers corridor_expansion exactly (no double-counting).
            Interpretation: the portion of the battery's virtual capacity
            relief attributed to line l in hour t.

        hourly_unit_cost_eur_mwh
            Average redispatch unit cost for the month containing this
            timestamp, mapped from monthly_costs [EUR / MWh].

        battery_cost_alleviation_eur
            Avoided redispatch cost in this hour [EUR]:
            = battery_cap_expansion_mw × hourly_unit_cost_eur_mwh × Δt_h
            where Δt_h is the inferred time-step (1 h for hourly data,
            0.25 h for 15-min data).
    """
    if monthly_costs is None:
        monthly_costs = load_monthly_redispatch_costs(DEFAULT_REDISPATCH_COST_YEAR)

    # ── 1. Per-line overload above threshold ───────────────────────────────
    df = df.copy()
    df["overload_mw"] = np.maximum(
        0.0,
        df["loading_fraction"] * df["s_nom_mw"]
        - congestion_threshold * df["s_nom_mw"]
    )

    # ── 2. Corridor-aggregated overload per timestamp ──────────────────────
    corridor_overload = (
        df.groupby("timestamp")["overload_mw"]
        .sum()
        .rename("total_corridor_overload_mw")
    )
    df = df.merge(corridor_overload, on="timestamp", how="left")

    # ── 3. Per-line battery capacity expansion (pro-rata allocation) ──────
    # The battery provides a single pool of virtual capacity α·P_bat [MW].
    # This pool is allocated back to each line proportionally to its share
    # of the total corridor overload:
    #
    #   corridor_expansion_t = min(α·P_bat, Σ_l overload_{l,t})
    #
    #   line_expansion_{l,t} = overload_{l,t}
    #                           × corridor_expansion_t / Σ_l overload_{l,t}
    #
    # When total overload ≤ α·P_bat (battery fully covers corridor):
    #   line_expansion_{l,t} = overload_{l,t}        (full per-line relief)
    # When total overload > α·P_bat (battery partially covers corridor):
    #   line_expansion_{l,t} = overload_{l,t} / total × α·P_bat  (pro-rata)
    #
    # This ensures Σ_l line_expansion_{l,t} = corridor_expansion_t exactly,
    # and rows can be summed across lines without double-counting.
    max_expansion_mw = alpha * battery_mw
    corridor_expansion = np.minimum(max_expansion_mw,
                                    df["total_corridor_overload_mw"])

    # Avoid division by zero for uncongested timestamps
    safe_total = df["total_corridor_overload_mw"].replace(0.0, np.nan)
    df["battery_cap_expansion_mw"] = (
        df["overload_mw"] / safe_total * corridor_expansion
    ).fillna(0.0)

    # ── 4. Infer time-step from data ───────────────────────────────────────
    ts_unique = df["timestamp"].drop_duplicates().sort_values()
    if len(ts_unique) > 1:
        median_gap_min = (ts_unique.diff().dropna()
                          .dt.total_seconds().median() / 60)
    else:
        median_gap_min = 60.0
    dt_h = median_gap_min / 60.0  # time-step in hours
    if abs(dt_h - 1.0) > 0.01 and abs(dt_h - 0.25) > 0.01:
        warnings.warn(
            f"Inferred time-step is {dt_h:.3f} h (median gap = {median_gap_min:.1f} min). "
            f"Expected 1 h (hourly) or 0.25 h (15-min).  Proceeding anyway.",
            UserWarning, stacklevel=2
        )

    # ── 5. Unit cost mapping ───────────────────────────────────────────────
    unit_map = _build_unit_cost_map(monthly_costs, df, cost_mode, congestion_threshold)
    df["hourly_unit_cost_eur_mwh"] = _map_hourly_unit_cost(df, unit_map)

    # ── 6. Cost alleviation per row ────────────────────────────────────────
    # CA_t = ΔC_t [MW] × c̄_m [EUR/MWh] × Δt [h]  →  EUR
    df["battery_cost_alleviation_eur"] = (
        df["battery_cap_expansion_mw"]
        * df["hourly_unit_cost_eur_mwh"]
        * dt_h
    )

    return df


# ══════════════════════════════════════════════════════════════════════════════
# SHADOW-PRICE PIPELINE  (paper primary method)
# ══════════════════════════════════════════════════════════════════════════════

def compute_congestion_volume_mwh(
    f_base: pd.DataFrame,
    f_boost: pd.DataFrame,
    mu_base: pd.DataFrame,
    dt_h: float = 1.0,
    dual_tol: float = DUAL_TOL,
) -> pd.DataFrame:
    """Derive the avoided congestion volume [MWh] per (line, hour).

    Parameters
    ----------
    f_base : pd.DataFrame
        Absolute power flows from the BASE LOPF [MW].
        Shape: timestamps × lines (index=timestamp, columns=line_id).
        Source: n.lines_t.p0.abs() saved after base solve.
    f_boost : pd.DataFrame
        Absolute power flows from the BOOST LOPF [MW] — same network but with
        corridor s_nom expanded by α·P_bat to simulate the battery's virtual-
        transmission effect.
        Shape: timestamps × lines.
    mu_base : pd.DataFrame
        Shadow prices (dual variables on line upper-bound constraints) from the
        BASE solve [EUR/MW·h].
        Shape: timestamps × lines.
        Source: n.lines_t.mu_upper saved after base solve with keep_shadowprices=True.
    dt_h : float
        Time step in hours (1.0 for hourly, 0.25 for quarter-hourly).
    dual_tol : float
        Minimum |μ| to classify a line-hour as congested.

    Returns
    -------
    pd.DataFrame (timestamps × lines)
        Avoided flow volume [MWh] per (line, hour).  Zero in uncongested hours.

    Notes
    -----
    Shadow prices identify WHEN a line is congested (binary mask μ > dual_tol).
    They are NOT used to compute EUR — that mapping comes from historical costs.
    The volume comes from Δf = |f_boost| − |f_base|, clipped to ≥ 0.
    Lines where the boost LOPF carries less flow than the base (possible due
    to network re-routing) are clipped to zero.
    """
    common_lines = (
        f_base.columns
        .intersection(f_boost.columns)
        .intersection(mu_base.columns)
    )
    common_times = (
        f_base.index
        .intersection(f_boost.index)
        .intersection(mu_base.index)
    )

    f_b  = f_base.loc[common_times, common_lines].abs()
    f_bo = f_boost.loc[common_times, common_lines].abs()
    mu   = mu_base.loc[common_times, common_lines].abs()

    congested: pd.DataFrame = mu > dual_tol
    delta_f: pd.DataFrame = (f_bo - f_b).clip(lower=0.0)
    volume_mwh: pd.DataFrame = delta_f.where(congested, other=0.0) * dt_h

    return volume_mwh


def _infer_dt(index: pd.Index) -> float:
    if len(index) < 2:
        return 1.0
    dt = index.to_series().sort_values().diff().dropna().dt.total_seconds().median() / 3600.0
    return float(dt) if pd.notna(dt) and dt > 0 else 1.0


def compute_congestion_volume_simple(
    mu_base: pd.DataFrame,
    s_nom_series: pd.Series,
    battery_mw: float = DEFAULT_BATTERY_MW,
    alpha: float = DEFAULT_ALPHA,
    dual_tol: float = DUAL_TOL,
    dt_h: float = 1.0,
) -> pd.DataFrame:
    """Simple mode: flat α×P_bat relief in the most-congested line's hours only.

    Shadow prices are used ONLY as binary congestion flags (|μ| > dual_tol).
    The single corridor line with the most congested hours is the target line —
    identical criterion to the one-line method.  Full α×P_bat MW is credited
    in each hour where that line is congested; μ magnitudes are not used.
    No LOPF re-solve needed; actual Δf ≤ α×P_bat is measured by one-line.
    """
    mu_abs = mu_base.abs()
    # Normalise dtype: mu_upper columns are strings, s_nom index may be int.
    mu_abs.columns = mu_abs.columns.astype(str)
    s_nom_norm = s_nom_series.copy()
    s_nom_norm.index = s_nom_norm.index.astype(str)
    corridor_lines = mu_abs.columns.intersection(s_nom_norm.index)
    if corridor_lines.empty:
        raise ValueError(
            f"No corridor lines found after dtype normalisation. "
            f"mu_upper columns (first 5): {mu_abs.columns[:5].tolist()}  "
            f"s_nom index (first 5): {s_nom_norm.index[:5].tolist()}"
        )
    mu_c = mu_abs[corridor_lines]
    congested_mask: pd.DataFrame = mu_c > dual_tol

    hours_per_line: pd.Series = congested_mask.sum(axis=0)
    target_line: str = str(hours_per_line.idxmax())
    target_congested: pd.Series = congested_mask[target_line]

    max_mw = alpha * battery_mw
    battery_applied = pd.Series(
        np.where(target_congested, max_mw, 0.0),
        index=mu_base.index,
        dtype=float,
    )
    return pd.DataFrame(
        {
            "target_line":        target_line,
            "congested":          target_congested.values,
            "battery_mw_applied": battery_applied.values,
            "volume_avoided_mwh": (battery_applied * dt_h).values,
        },
        index=mu_base.index,
    )


def run_shadow_simple(
    mu_base_csv: Path,
    s_nom_series: pd.Series,
    monthly_costs: dict[int, float],
    battery_mw: float = DEFAULT_BATTERY_MW,
    alpha: float = DEFAULT_ALPHA,
    dt_h: float = 1.0,
    dual_tol: float = DUAL_TOL,
) -> tuple[pd.DataFrame, dict]:
    mu_b = pd.read_csv(mu_base_csv, index_col=0, parse_dates=True)
    hourly = compute_congestion_volume_simple(
        mu_b,
        s_nom_series=s_nom_series,
        battery_mw=battery_mw,
        alpha=alpha,
        dual_tol=dual_tol,
        dt_h=dt_h,
    )
    month_cost = pd.Series(
        [monthly_costs.get(ts.month, np.nan) for ts in hourly.index],
        index=hourly.index,
        dtype=float,
    )
    hourly["hourly_unit_cost_eur_mwh"] = month_cost.values
    hourly["cost_avoided_eur"] = hourly["volume_avoided_mwh"] * month_cost
    total_volume = float(hourly["volume_avoided_mwh"].sum())
    total_cost = float(hourly["cost_avoided_eur"].sum())
    target_line = str(hourly["target_line"].iloc[0]) if "target_line" in hourly.columns else "unknown"
    summary = {
        "mode": "simple",
        "target_line": target_line,
        "total_timestamps": len(hourly),
        "congested_timestamps": int(hourly["congested"].sum()),
        "congestion_share_pct": round(100.0 * hourly["congested"].mean(), 2),
        "total_volume_mwh": round(total_volume, 1),
        "total_cost_eur": round(total_cost, 2),
        "cost_per_mw_pa": round(total_cost / battery_mw, 2) if battery_mw > 0 else np.nan,
    }
    return hourly, summary


def compute_congestion_volume_mwh_perline(
    f_base: pd.DataFrame,
    boost_manifest: list[dict],
    mu_base: pd.DataFrame,
    dt_h: float = 1.0,
    dual_tol: float = DUAL_TOL,
    battery_mw: float = DEFAULT_BATTERY_MW,
    alpha: float = DEFAULT_ALPHA,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Optimal mode: pick best boosted line per hour from per-line boost runs."""
    base = f_base.abs()
    common_t = base.index.intersection(mu_base.index)
    common_lines = base.columns.intersection(mu_base.columns)
    mu = mu_base.loc[common_t, common_lines].abs()
    congested = mu > dual_tol

    best_line = pd.Series(index=common_t, data=pd.NA, dtype="object")
    best_delta = pd.Series(index=common_t, data=0.0, dtype=float)
    line_delta_cols: dict[str, pd.Series] = {}

    for item in boost_manifest:
        line_id = str(item.get("line_id"))
        csv_path = item.get("f_boost_csv") or item.get("f_boost_single_csv") or item.get("path")
        if not csv_path:
            continue
        f_bo = pd.read_csv(Path(csv_path), index_col=0, parse_dates=True).abs()
        if line_id not in f_bo.columns or line_id not in base.columns:
            continue
        t_idx = common_t.intersection(f_bo.index)
        delta = (f_bo.loc[t_idx, line_id] - base.loc[t_idx, line_id]).clip(lower=0.0)
        full_delta = pd.Series(0.0, index=common_t)
        full_delta.loc[t_idx] = delta.values
        line_delta_cols[line_id] = full_delta
        better = full_delta > best_delta
        best_delta.loc[better] = full_delta.loc[better]
        best_line.loc[better] = line_id

    best_delta = best_delta.clip(upper=alpha * battery_mw)
    volume = (best_delta * dt_h).where(congested.any(axis=1), other=0.0)
    volume_optimal = pd.DataFrame(
        {
            "volume_mwh_optimal": volume.values,
            "assigned_line": best_line.values,
            "delta_f_mw_optimal": best_delta.values,
        },
        index=common_t,
    )
    assignment_log = pd.DataFrame(line_delta_cols, index=common_t)
    return volume_optimal, assignment_log


def compute_cost_from_perline_volumes(
    volume_optimal: pd.DataFrame,
    assignment_log: pd.DataFrame,
    monthly_costs: dict[int, float],
) -> dict:
    month_cost = pd.Series(
        [monthly_costs.get(ts.month, np.nan) for ts in volume_optimal.index],
        index=volume_optimal.index,
        dtype=float,
    )
    cost = volume_optimal["volume_mwh_optimal"] * month_cost
    assigned = volume_optimal["assigned_line"].dropna()
    primary_line = assigned.mode().iloc[0] if not assigned.empty else None
    total_volume = float(volume_optimal["volume_mwh_optimal"].sum())
    total_cost = float(cost.sum())
    return {
        "mode": "optimal",
        "total_timestamps": len(volume_optimal),
        "congested_timestamps": int((volume_optimal["volume_mwh_optimal"] > 0).sum()),
        "congestion_share_pct": round(100.0 * (volume_optimal["volume_mwh_optimal"] > 0).mean(), 2),
        "total_volume_mwh": round(total_volume, 1),
        "total_cost_eur": round(total_cost, 2),
        "primary_bottleneck_line": primary_line,
    }


def compute_congestion_volume_one_line(
    f_base: pd.DataFrame,
    f_boost_single: pd.DataFrame,
    mu_base: pd.DataFrame,
    target_line: str,
    dt_h: float = 1.0,
    dual_tol: float = DUAL_TOL,
    battery_mw: float = DEFAULT_BATTERY_MW,
    alpha: float = DEFAULT_ALPHA,
) -> pd.DataFrame:
    """One-line mode: battery assigned permanently to one fixed corridor line."""
    common_t = f_base.index.intersection(f_boost_single.index).intersection(mu_base.index)
    if target_line not in f_base.columns:
        raise ValueError(f"target_line '{target_line}' not in f_base columns.")
    if target_line not in f_boost_single.columns:
        raise ValueError(f"target_line '{target_line}' not in f_boost_single columns.")

    f_b = f_base.loc[common_t, target_line].abs()
    f_bo = f_boost_single.loc[common_t, target_line].abs()
    mu_line = (
        mu_base.loc[common_t, target_line].abs()
        if target_line in mu_base.columns
        else pd.Series(0.0, index=common_t)
    )
    congested = mu_line > dual_tol
    max_mw = alpha * battery_mw
    delta_f = (f_bo - f_b).clip(lower=0.0, upper=max_mw)
    volume_mwh = (delta_f * dt_h).where(congested, other=0.0)
    return pd.DataFrame(
        {
            "target_line": target_line,
            "congested": congested.values,
            "delta_f_mw": delta_f.values,
            "volume_avoided_mwh": volume_mwh.values,
        },
        index=common_t,
    )


def _select_target_line_by_mwh_relief(
    mu_wide: pd.DataFrame,
    f_base_wide: pd.DataFrame,
    boost_dir: Path,
    battery_mw: float,
    alpha: float,
    dual_tol: float,
    scenario_year: int,
) -> tuple[str, pd.Series]:
    """Select the corridor line that yields the greatest total MWh relief.

    For each corridor line with at least one congested hour, loads the
    pre-computed boost-flow CSV and computes the summed Δf over congested hours:

        delta_f_t = min(max(0, |f_boost_t| − |f_base_t|), α·P_bat)

    The line maximising Σ_t delta_f_t is returned.  Requires boost CSVs to be
    already present (populated by the optimal alleviation run); missing CSVs
    are skipped with a warning and scored as 0 MWh relief.

    All diagnostic output is written to stderr so the return value can be
    captured cleanly by a shell $() subshell.
    """
    max_mw = alpha * battery_mw
    relief: dict[str, float] = {}

    candidate_lines = [
        line for line in mu_wide.columns
        if line in f_base_wide.columns and (mu_wide[line].abs() > dual_tol).any()
    ]
    if not candidate_lines:
        raise RuntimeError(
            f"No corridor lines have congested hours at dual_tol={dual_tol:.1e}."
        )

    for line in candidate_lines:
        congested_mask = mu_wide[line].abs() > dual_tol
        safe_line = str(line).replace(" ", "_").replace("/", "-")
        boost_csv = (
            boost_dir
            / f"line_flow_abs_mw_{scenario_year}_boost_mw{int(battery_mw)}"
              f"_a{alpha:.2f}_line{safe_line}.csv"
        )

        if not boost_csv.exists():
            warnings.warn(
                f"[one_line selection] Boost CSV missing for line {line!r}: "
                f"{boost_csv.name}. Skipping — run optimal alleviation first.",
                UserWarning, stacklevel=2,
            )
            relief[line] = 0.0
            continue

        try:
            boost_df = pd.read_csv(boost_csv, index_col=0, parse_dates=True)
        except Exception as exc:
            warnings.warn(
                f"[one_line selection] Cannot read boost CSV for line {line!r}: {exc}",
                UserWarning, stacklevel=2,
            )
            relief[line] = 0.0
            continue

        if str(line) in boost_df.columns:
            f_boost = boost_df[str(line)].abs()
        elif boost_df.shape[1] == 1:
            f_boost = boost_df.iloc[:, 0].abs()
        else:
            warnings.warn(
                f"[one_line selection] Cannot locate column for line {line!r} "
                f"in {boost_csv.name} ({boost_df.shape[1]} columns). Skipping.",
                UserWarning, stacklevel=2,
            )
            relief[line] = 0.0
            continue

        f_base = f_base_wide[line].abs()
        f_base, f_boost = f_base.align(f_boost, join="inner")

        # The uprated line carries MORE flow in the boost solve; relief = Δf > 0.
        delta_f = (f_boost - f_base).clip(lower=0.0, upper=max_mw)
        mask = congested_mask.reindex(f_base.index, fill_value=False)
        relief[line] = float(delta_f[mask].sum())

    relief_series = pd.Series(relief, name="total_relief_mwh").sort_values(ascending=False)
    target_line = str(relief_series.index[0])

    print(f"[one_line] TARGET_LINE selected by max MWh relief: {target_line}", file=sys.stderr)
    print(f"  {'Line':<12}  {'Cong. h':>8}  {'Relief MWh':>12}", file=sys.stderr)
    for line, mwh in relief_series.head(5).items():
        cong_h = int((mu_wide[line].abs() > dual_tol).sum())
        print(f"  {str(line):<12}  {cong_h:>8}  {mwh:>12.1f}", file=sys.stderr)

    return target_line, relief_series


def run_one_line(
    mu_base_csv: Path,
    f_base_csv: Path,
    f_boost_single_csv: Path,
    target_line: str,
    s_nom_series: pd.Series,
    monthly_costs: dict[int, float],
    dt_h: float = 1.0,
    dual_tol: float = DUAL_TOL,
    battery_mw: float = DEFAULT_BATTERY_MW,
    alpha: float = DEFAULT_ALPHA,
) -> tuple[pd.DataFrame, dict]:
    """End-to-end one-line mode pipeline."""
    mu_b = pd.read_csv(mu_base_csv, index_col=0, parse_dates=True)
    f_b = pd.read_csv(f_base_csv, index_col=0, parse_dates=True)
    f_bo = pd.read_csv(f_boost_single_csv, index_col=0, parse_dates=True)
    dt_h = dt_h or _infer_dt(mu_b.index)
    hourly = compute_congestion_volume_one_line(
        f_b, f_bo, mu_b, target_line, dt_h, dual_tol, battery_mw=battery_mw, alpha=alpha
    )

    month_cost = pd.Series(
        [monthly_costs.get(ts.month, np.nan) for ts in hourly.index],
        index=hourly.index,
        dtype=float,
    )
    hourly["hourly_unit_cost_eur_mwh"] = month_cost.values
    hourly["cost_avoided_eur"] = hourly["volume_avoided_mwh"] * month_cost

    total_volume = float(hourly["volume_avoided_mwh"].sum())
    total_cost = float(hourly["cost_avoided_eur"].sum())
    _ = float(s_nom_series.get(target_line, np.nan))
    summary = {
        "mode": "one-line",
        "target_line": target_line,
        "total_timestamps": len(hourly),
        "congested_timestamps": int(hourly["congested"].sum()),
        "congestion_share_pct": round(100.0 * hourly["congested"].mean(), 2),
        "total_volume_mwh": round(total_volume, 1),
        "total_cost_eur": round(total_cost, 2),
        "note": (
            "Lower bound among flow-difference modes. Battery permanently "
            f"committed to '{target_line}'. Hours where other lines bind "
            "are not captured. Compare with 'optimal' mode to quantify "
            "the flexibility premium of dynamic commitment."
        ),
    }
    return hourly, summary


def compute_cost_alleviation_from_shadow(
    mu_base_csv: Path,
    f_base_csv: Path,
    f_boost_csv: Path,
    s_nom_series: pd.Series,
    monthly_costs: dict[int, float],
    dt_h: float = 1.0,
    dual_tol: float = DUAL_TOL,
    mu_boost_csv: Path | None = None,
) -> pd.DataFrame:
    """Compute avoided congestion cost using the shadow-price pipeline.

    PRIMARY OUTPUT
    ~~~~~~~~~~~~~~
    volume_avoided_mwh   — avoided MWh per line, summed over the year.
    cost_avoided_eur     — volume × historical monthly redispatch cost [EUR/MWh].

    CROSS-CHECK (model-internal, NOT used for paper results)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    rent_base_eur_model  — Σ_t μ_base × s_nom   (LP congestion rent, EUR)
    saving_eur_model     — rent_base − rent_boost (requires mu_boost_csv)

    The model-internal rents are reported for internal validation only.
    They should NOT be used to estimate real-world avoided redispatch costs
    because LP shadow prices reflect the model's marginal cost assumptions,
    not what German TSOs actually paid for redispatch in the historical data.

    Parameters
    ----------
    mu_base_csv : Path
        CSV of base-case shadow prices (timestamps × lines).
    f_base_csv : Path
        CSV of base-case absolute power flows |p0| [MW] (timestamps × lines).
    f_boost_csv : Path
        CSV of boost-case absolute power flows [MW] — s_nom expanded by α·P_bat.
    s_nom_series : pd.Series
        Thermal ratings indexed by line_id [MW].  Used for cross-check only.
    monthly_costs : dict[int, float]
        Historical monthly average redispatch cost {month_int: EUR/MWh}.
        Source: SMARD / BNetzA data.  The ONLY EUR source for the primary result.
    dt_h : float
        Time step in hours.
    dual_tol : float
        Shadow-price threshold for congestion detection.
    mu_boost_csv : Path | None
        Optional CSV of boost-case shadow prices.  Required only for the
        model-internal cross-check.

    Returns
    -------
    pd.DataFrame indexed by line_id with columns:
        volume_avoided_mwh      — primary result: avoided MWh over full period
        cost_avoided_eur        — primary result: volume × historical EUR/MWh
        congested_hours         — count of hours where |μ_base| > dual_tol
        rent_base_eur_model     — cross-check: LP congestion rent (base)
        saving_eur_model        — cross-check: LP rent saving (needs mu_boost_csv)
    """
    mu_b = pd.read_csv(mu_base_csv,  index_col=0, parse_dates=True)
    f_b  = pd.read_csv(f_base_csv,   index_col=0, parse_dates=True)
    f_bo = pd.read_csv(f_boost_csv,  index_col=0, parse_dates=True)

    # ── Volume (primary) ───────────────────────────────────────────────────
    volume_mwh = compute_congestion_volume_mwh(f_b, f_bo, mu_b, dt_h, dual_tol)

    # ── Map volume → EUR using HISTORICAL costs (not LP prices) ────────────
    month_cost_series = pd.Series(
        [monthly_costs.get(ts.month, np.nan) for ts in volume_mwh.index],
        index=volume_mwh.index,
        name="eur_per_mwh",
    )
    cost_eur_hourly: pd.DataFrame = volume_mwh.multiply(month_cost_series, axis=0)

    total_volume    = volume_mwh.sum(axis=0)
    total_cost      = cost_eur_hourly.sum(axis=0)
    congested_hours = (mu_b.abs() > dual_tol).sum(axis=0).reindex(total_volume.index, fill_value=0)

    out = pd.DataFrame({
        "volume_avoided_mwh": total_volume,
        "cost_avoided_eur":   total_cost,
        "congested_hours":    congested_hours,
    })

    # ── Cross-check: model-internal LP congestion rent ─────────────────────
    common = mu_b.columns.intersection(s_nom_series.index)
    s_nom  = s_nom_series.reindex(common).astype(float)
    rent_base = mu_b[common].abs().multiply(s_nom, axis=1).sum(axis=0)
    out.loc[common, "rent_base_eur_model"] = rent_base

    if mu_boost_csv is not None:
        mu_bo = pd.read_csv(mu_boost_csv, index_col=0, parse_dates=True)
        rent_boost = mu_bo[common].abs().multiply(s_nom, axis=1).sum(axis=0)
        out.loc[common, "saving_eur_model"] = rent_base - rent_boost
    else:
        out["saving_eur_model"] = np.nan

    return out.sort_values("cost_avoided_eur", ascending=False)


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY STATISTICS
# ══════════════════════════════════════════════════════════════════════════════

def summarise_results(
    df:          pd.DataFrame,
    battery_mw:  float,
    alpha:       float,
    threshold:   float,
) -> dict:
    """
    Compute summary statistics over the full simulation horizon.

    Returns a dict suitable for printing and for inclusion in the paper's
    results section.
    """
    # Deduplicate to corridor-level per timestamp.
    # battery_cap_expansion_mw is now per-line; summing across lines gives the
    # corridor expansion for that hour.  battery_cost_alleviation_eur likewise.
    ts_df = (
        df.groupby("timestamp").agg(
            total_corridor_overload_mw=("total_corridor_overload_mw", "first"),
            battery_cap_expansion_mw=("battery_cap_expansion_mw",    "sum"),  # sum across lines
            battery_cost_alleviation_eur=("battery_cost_alleviation_eur", "sum"),
            hourly_unit_cost_eur_mwh=("hourly_unit_cost_eur_mwh",   "first"),
        ).reset_index()
    )
    ts_df["month"] = ts_df["timestamp"].dt.month
    ts_df["year"]  = ts_df["timestamp"].dt.year

    # Infer dt
    ts_sorted = ts_df["timestamp"].sort_values()
    if len(ts_sorted) > 1:
        dt_h = ts_sorted.diff().dropna().dt.total_seconds().median() / 3600
    else:
        dt_h = 1.0

    congested_mask = ts_df["battery_cap_expansion_mw"] > 0

    total_hours           = len(ts_df)
    congested_hours       = int(congested_mask.sum())
    congestion_share_pct  = 100.0 * congested_hours / total_hours if total_hours > 0 else 0.0
    total_expansion_mwh   = float(ts_df["battery_cap_expansion_mw"].sum() * dt_h)
    total_alleviation_eur = float(ts_df["battery_cost_alleviation_eur"].sum())
    alleviation_per_mw_pa = (
        total_alleviation_eur / battery_mw
        if battery_mw > 0 else np.nan
    )

    # Peak hours (top-1% most valuable)
    n_peak = max(1, int(0.01 * congested_hours))
    peak_hours_eur = (
        ts_df.loc[congested_mask, "battery_cost_alleviation_eur"]
        .nlargest(n_peak)
        .sum()
    )

    # Monthly breakdown
    monthly = (
        ts_df.groupby(["year", "month"])
        .agg(
            congested_hours=("battery_cap_expansion_mw",
                             lambda x: (x > 0).sum()),
            expansion_mwh=("battery_cap_expansion_mw",
                           lambda x: (x * dt_h).sum()),
            cost_alleviation_eur=("battery_cost_alleviation_eur", "sum"),
            mean_unit_cost=("hourly_unit_cost_eur_mwh", "mean"),
        )
    )

    return {
        "battery_mw":              battery_mw,
        "alpha":                   alpha,
        "congestion_threshold":    threshold,
        "dt_h":                    dt_h,
        "total_timestamps":        total_hours,
        "congested_timestamps":    congested_hours,
        "congestion_share_pct":    round(congestion_share_pct, 2),
        "total_expansion_mwh":     round(total_expansion_mwh, 1),
        "total_alleviation_eur":   round(total_alleviation_eur, 2),
        "alleviation_per_mw_pa":   round(alleviation_per_mw_pa, 2),
        "peak_1pct_alleviation_eur": round(peak_hours_eur, 2),
        "monthly_breakdown":       monthly,
    }


def print_summary(s: dict) -> None:
    """Pretty-print the summary statistics."""
    print("\n" + "=" * 72)
    print("  GRIDBOOSTER CONGESTION COST ALLEVIATION — SUMMARY")
    print("=" * 72)
    print(f"  Battery capacity          : {s['battery_mw']:.0f} MW")
    print(f"  Virtual-transmission α    : {s['alpha']:.3f}")
    print(f"  Max expansion α·P_bat     : {s['alpha'] * s['battery_mw']:.0f} MW")
    print(f"  Congestion threshold      : {s['congestion_threshold']:.2f} × s_nom")
    print(f"  Time-step                 : {s['dt_h']:.2f} h")
    print()
    print(f"  Timestamps (total)        : {s['total_timestamps']:,}")
    print(f"  Congested timestamps      : {s['congested_timestamps']:,}  "
          f"({s['congestion_share_pct']:.1f}% of year)")
    print()
    print(f"  Total expansion volume    : {s['total_expansion_mwh']:,.0f} MWh")
    print(f"  Total cost alleviation    : EUR {s['total_alleviation_eur']:>14,.2f}")
    print(f"  Alleviation / MW / year   : EUR {s['alleviation_per_mw_pa']:>10,.2f}")
    print(f"  Top-1% peak hours contrib : EUR {s['peak_1pct_alleviation_eur']:>14,.2f}")
    print()
    print("  Monthly breakdown:")
    print(f"  {'Year':>4} {'Month':>5} {'Cong.h':>8} {'ExpMWh':>10} "
          f"{'Aleviation EUR':>16} {'AvgCost €/MWh':>14}")
    print("  " + "-" * 64)
    for (yr, mo), row in s["monthly_breakdown"].iterrows():
        print(
            f"  {yr:>4}  {mo:>4}  "
            f"{int(row['congested_hours']):>7}  "
            f"{row['expansion_mwh']:>10,.1f}  "
            f"{row['cost_alleviation_eur']:>16,.2f}  "
            f"{row['mean_unit_cost']:>13.2f}"
        )
    print("=" * 72)


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_output_path(
    input_csv:  Path,
    output_dir: Path | None,
    battery_mw: float,
    alpha:      float,
) -> Path:
    """
    Determine output path.  If output_dir is provided, use it; otherwise
    write alongside the input file in the same results sub-folder.
    """
    stem    = input_csv.stem
    suffix  = f"_battery{int(battery_mw)}mw_alpha{alpha:.2f}_alleviation"
    out_name = stem + suffix + ".csv"

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir / out_name
    else:
        default_dir = _default_congestion_alleviation_dir(input_csv)
        default_dir.mkdir(parents=True, exist_ok=True)
        return default_dir / out_name


def save_results(
    df:         pd.DataFrame,
    summary:    dict,
    out_csv:    Path,
) -> None:
    """Save the enriched hourly DataFrame and a companion summary CSV."""
    # ── Hourly detail ──────────────────────────────────────────────────────
    df.to_csv(out_csv, index=False, float_format="%.4f")
    print(f"\n  [saved] Hourly detail   : {out_csv}")

    # ── Monthly summary ────────────────────────────────────────────────────
    summary_csv = out_csv.with_name(out_csv.stem + "_monthly_summary.csv")
    summary["monthly_breakdown"].to_csv(summary_csv, float_format="%.2f")
    print(f"  [saved] Monthly summary : {summary_csv}")

    # ── Scalar KPIs ────────────────────────────────────────────────────────
    kpi_csv = out_csv.with_name(out_csv.stem + "_kpi.csv")
    kpi_rows = {k: v for k, v in summary.items() if k != "monthly_breakdown"}
    pd.DataFrame([kpi_rows]).to_csv(kpi_csv, index=False, float_format="%.4f")
    print(f"  [saved] KPI table       : {kpi_csv}")


# The shell script `job_congestion_alleviation.sh` runs ONE alleviation method
# per submission and writes outputs to:
#
#     results/kupferzell_{scenario}/congestion_alleviation/{method}/
#         <hourly alleviation CSV with one EUR column>
#
# After all three methods (simple, one_line, optimal_alleviation) have been
# run, this function scans those three sub-folders, aggregates each per-line
# hourly CSV down to a corridor-level hourly total, and joins them into one
# CSV with one row per hour and three relief-EUR columns:
#
#     Time_CET,
#     congestion_relief_simple_eur,
#     congestion_relief_one_line_eur,
#     congestion_relief_optimal_eur
#
# Idempotent: re-run after any new alleviation job to refresh the merged CSV.
# Methods not yet run get zero columns so downstream scripts can still load
# the file. This is invoked from job_congestion_alleviation.sh via a heredoc
# so no extra CLI argument needs to be added to the existing argparse block.

_METHOD_DIR_NAMES = ("simple", "one_line", "optimal_alleviation")
_METHOD_TO_OUTPUT_COL = {
    "simple":              "congestion_relief_simple_eur",
    "one_line":            "congestion_relief_one_line_eur",
    "optimal_alleviation": "congestion_relief_optimal_eur",
}


def _find_method_csv(method_dir: Path) -> Path | None:
    """Pick the most recent hourly alleviation CSV in a method sub-directory.

    Accepts any *.csv in the directory that is not a *_kpi.csv or
    *_monthly_summary.csv. If the producing run-mode emits multiple CSVs,
    the most recently modified one is used.
    """
    if not method_dir.is_dir():
        return None
    candidates = [p for p in method_dir.glob("*.csv")
                  if not p.name.endswith("_kpi.csv")
                  and not p.name.endswith("_monthly_summary.csv")]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _aggregate_hourly_to_total(csv_path: Path) -> pd.Series:
    """
    Read a per-method hourly alleviation CSV and return a Series indexed by
    timestamp giving the total corridor relief [EUR] per hour.

    The file may either be one row per (timestamp, line) or already
    aggregated to one row per timestamp. In either case we groupby and sum
    the EUR column. The function looks for any of the following columns:

        battery_cost_alleviation_eur     (original pro-rata code)
        avoided_cost_eur, alleviation_eur, congestion_relief_eur, saving_eur
                                         (likely names for the new run-modes)

    If the new simple/one_line/optimal run-modes use a column name not in
    this list, add it to the recognised set below or rename in the producing
    script — the function raises a ValueError listing the available columns.
    """
    df = pd.read_csv(csv_path)

    ts_col = next((c for c in df.columns
                   if str(c).strip().lower() in
                   ("time_cet", "timestamp", "time")),
                  None)
    if ts_col is None:
        raise ValueError(f"{csv_path} lacks a timestamp column.")

    eur_col = next((c for c in df.columns
                    if str(c).strip().lower() in (
                        "battery_cost_alleviation_eur",
                        "alleviation_eur",
                        "avoided_cost_eur",
                        "congestion_relief_eur",
                        "saving_eur",
                    )),
                   None)
    if eur_col is None:
        raise ValueError(
            f"{csv_path} contains no recognised EUR column. "
            f"Available: {list(df.columns)}\n"
            f"Add the column name to _aggregate_hourly_to_total or "
            f"rename in the producing script."
        )

    df[ts_col] = pd.to_datetime(df[ts_col])
    s = (df.groupby(ts_col)[eur_col]
         .sum(min_count=1)
         .astype(float)
         .sort_index())
    s.name = csv_path.parent.name  # method dir name
    return s


def merge_alleviation_revenues(
        scenario: str,
        year: int,
        results_root: Path | str | None = None,
) -> Path:
    """
    Build the merged 3-series hourly CSV for a given scenario.

    Parameters
    ----------
    scenario : "simple" | "full"
    year     : e.g. 2025
    results_root : path to repo's results/ directory. Defaults to
                   PROJECT_DIR / "results".

    Returns
    -------
    Path to the merged CSV. Methods that haven't yet been run are written
    as zero columns so downstream scripts can still load the file. Methods
    that have been run overwrite the corresponding column.
    """
    if results_root is None:
        results_root = PROJECT_DIR / "results"
    results_root = Path(results_root)

    base_dir = (results_root / f"kupferzell_{scenario}" /
                "congestion_alleviation")
    if not base_dir.is_dir():
        raise FileNotFoundError(f"Congestion-alleviation root not found: "
                                f"{base_dir}")

    series_per_method: dict[str, pd.Series] = {}
    methods_found: list[str] = []
    methods_missing: list[str] = []

    for m in _METHOD_DIR_NAMES:
        csv = _find_method_csv(base_dir / m)
        if csv is None:
            methods_missing.append(m)
            continue
        s = _aggregate_hourly_to_total(csv)
        s = s[s.index.year == year]
        series_per_method[m] = s
        methods_found.append(m)
        print(f"[merge] {m:<24s} ← {csv.relative_to(results_root)}  "
              f"({len(s):,} hours, total = EUR {s.sum():,.0f})")

    if not series_per_method:
        raise RuntimeError(
            f"No per-method alleviation CSVs found under {base_dir}. "
            f"Run job_congestion_alleviation.sh first."
        )

    # Union index across whatever methods are available
    idx = sorted(set().union(*[s.index for s in series_per_method.values()]))
    merged = pd.DataFrame(index=pd.DatetimeIndex(idx, name="Time_CET"))
    for m in _METHOD_DIR_NAMES:
        col = _METHOD_TO_OUTPUT_COL[m]
        if m in series_per_method:
            merged[col] = series_per_method[m].reindex(merged.index,
                                                       fill_value=0.0)
        else:
            # Method not yet run — fill with zero column so downstream
            # scripts can still open the file. Will be overwritten next run.
            merged[col] = 0.0

    out_csv = base_dir / f"alleviation_revenues_merged_{year}.csv"
    merged.reset_index().to_csv(out_csv, index=False, float_format="%.4f")

    print(f"\n[merge] Wrote merged CSV: {out_csv}")
    if methods_missing:
        print(f"[merge] Methods not yet run (zero columns): "
              f"{methods_missing}. Re-run the merge after running them.")
    return out_csv

# ══════════════════════════════════════════════════════════════════════════════
# PROGRAMMATIC API  (import-friendly)
# ══════════════════════════════════════════════════════════════════════════════

def run_legacy(
    csv_path:            Path | str,
    battery_mw:          float = DEFAULT_BATTERY_MW,
    alpha:               float = DEFAULT_ALPHA,
    congestion_threshold: float = DEFAULT_CONGESTION_THRESHOLD,
    monthly_costs:       dict[int, float] | None = None,
    cost_mode:           str   = "unit_cost",
    output_dir:          Path | str | None = None,
    verbose:             bool  = True,
    redispatch_cost_year: str = DEFAULT_REDISPATCH_COST_YEAR,
    network_path:        Path | str | None = None,
    minimum_voltage:     float = DEFAULT_MINIMUM_VOLTAGE,
) -> tuple[pd.DataFrame, dict]:
    """
    End-to-end pipeline.  Returns (enriched_df, summary_dict).

    Example
    -------
    >>> from congestion_cost_alleviation import run
    >>> df, s = run(
    ...     csv_path   = "results/kupferzell_2024_simple/kupferzell_line_proximity_hourly_2025.csv",
    ...     battery_mw = 250,
    ...     alpha      = 1.0,
    ...     monthly_costs = {
    ...         1: 82, 2: 76, 3: 70, 4: 62, 5: 55, 6: 50,
    ...         7: 53, 8: 58, 9: 66, 10: 74, 11: 80, 12: 88,
    ...     },
    ...     cost_mode  = "unit_cost",   # EUR / MWh
    ...     output_dir = "results/kupferzell_2024_simple/",
    ... )
    """
    csv_path   = Path(csv_path)
    output_dir = Path(output_dir) if output_dir is not None else None
    network_path = Path(network_path) if network_path is not None else None

    if not _is_raw_occurrence_csv(csv_path):
        raise ValueError(
            f"Input CSV must be a raw congestion-occurrence file, got: {csv_path.name}"
        )

    if monthly_costs is None:
        monthly_costs = load_monthly_redispatch_costs(redispatch_cost_year)

    print(f"\n{'='*72}")
    print(f"  GRIDBOOSTER CONGESTION COST ALLEVIATION")
    print(f"  Input : {csv_path}")
    print(f"  Battery: {battery_mw:.0f} MW  |  α = {alpha:.3f}  |  "
          f"α·P_bat = {alpha*battery_mw:.0f} MW")
    print(f"  Cost mode: {cost_mode}")
    print(f"  Redispatch cost year : {redispatch_cost_year}")
    print(f"{'='*72}\n")

    # ── Load ───────────────────────────────────────────────────────────────
    df = load_hourly_line_loading(csv_path)

    # ── Compute ────────────────────────────────────────────────────────────
    df = compute_cost_alleviation(
        df,
        battery_mw            = battery_mw,
        alpha                 = alpha,
        congestion_threshold  = congestion_threshold,
        monthly_costs         = monthly_costs,
        cost_mode             = cost_mode,
    )

    # ── Summarise ──────────────────────────────────────────────────────────
    summary = summarise_results(df, battery_mw, alpha, congestion_threshold)
    if verbose:
        print_summary(summary)

    # ── Save ──────────────────────────────────────────────────────────────
    out_csv = _resolve_output_path(csv_path, output_dir, battery_mw, alpha)
    save_results(df, summary, out_csv)

    resolved_network = _discover_network_path(csv_path=csv_path, network_path=network_path)
    map_path = _save_alleviation_map(
        df=df,
        out_csv=out_csv,
        network_path=resolved_network,
        minimum_voltage=minimum_voltage,
    )
    if map_path is not None:
        print(f"  [saved] Alleviation map  : {map_path}")

    return df, summary


def run(
    mode: str = "simple",
    mu_base_csv: Path | str | None = None,
    s_nom_csv: Path | str | None = None,
    f_base_csv: Path | str | None = None,
    boost_manifest: list[dict] | None = None,
    f_boost_single_csv: Path | str | None = None,
    target_line: str | None = None,
    battery_mw: float = DEFAULT_BATTERY_MW,
    alpha: float = DEFAULT_ALPHA,
    monthly_costs: dict[int, float] | None = None,
    redispatch_cost_year: str = DEFAULT_REDISPATCH_COST_YEAR,
    output_dir: Path | str | None = None,
    verbose: bool = True,
) -> tuple[pd.DataFrame, dict]:
    """Dispatcher for simple, optimal, and one-line congestion alleviation modes."""
    if monthly_costs is None:
        monthly_costs = load_monthly_redispatch_costs(redispatch_cost_year)

    output_dir = Path(output_dir) if output_dir is not None else None

    def _load_s_nom() -> pd.Series:
        if s_nom_csv is None:
            raise ValueError(f"mode='{mode}' requires s_nom_csv.")
        return pd.read_csv(Path(s_nom_csv), index_col=0).squeeze("columns").astype(float)

    if mode == "simple":
        if mu_base_csv is None:
            raise ValueError("mode='simple' requires mu_base_csv.")
        s_nom = _load_s_nom()
        mu_b = pd.read_csv(Path(mu_base_csv), index_col=0, parse_dates=True)
        dt_h = _infer_dt(mu_b.index)
        hourly, summary = run_shadow_simple(
            mu_base_csv=Path(mu_base_csv),
            s_nom_series=s_nom,
            monthly_costs=monthly_costs,
            battery_mw=battery_mw,
            alpha=alpha,
            dt_h=dt_h,
        )
    elif mode == "optimal":
        if any(x is None for x in [mu_base_csv, f_base_csv, boost_manifest, s_nom_csv]):
            raise ValueError(
                "mode='optimal' requires mu_base_csv, f_base_csv, "
                "boost_manifest, and s_nom_csv."
            )
        _ = _load_s_nom()
        mu_b = pd.read_csv(Path(mu_base_csv), index_col=0, parse_dates=True)
        f_b = pd.read_csv(Path(f_base_csv), index_col=0, parse_dates=True)
        dt_h = _infer_dt(mu_b.index)
        volume_optimal, assignment_log = compute_congestion_volume_mwh_perline(
            f_b, boost_manifest or [], mu_b, dt_h,
        )
        result = compute_cost_from_perline_volumes(volume_optimal, assignment_log, monthly_costs)
        hourly = volume_optimal.copy()
        hourly["cost_avoided_eur"] = (
            hourly["volume_mwh_optimal"]
            * pd.Series(
                [monthly_costs.get(ts.month, np.nan) for ts in hourly.index],
                index=hourly.index,
            )
        )
        summary = result
    elif mode == "one-line":
        if any(x is None for x in [mu_base_csv, f_base_csv, f_boost_single_csv, target_line]):
            raise ValueError(
                "mode='one-line' requires mu_base_csv, f_base_csv, "
                "f_boost_single_csv, and target_line."
            )
        s_nom = _load_s_nom()
        dt_h = _infer_dt(
            pd.read_csv(Path(mu_base_csv), index_col=0, parse_dates=True, nrows=5).index
        )
        hourly, summary = run_one_line(
            mu_base_csv=Path(mu_base_csv),
            f_base_csv=Path(f_base_csv),
            f_boost_single_csv=Path(f_boost_single_csv),
            target_line=target_line,
            s_nom_series=s_nom,
            monthly_costs=monthly_costs,
            dt_h=dt_h,
        )
    else:
        raise ValueError(
            f"mode must be 'simple', 'optimal', or 'one-line'. Got: {mode!r}"
        )

    if verbose:
        _print_mode_summary(summary)
    if output_dir is not None:
        _save_mode_results(hourly, summary, output_dir, mode, battery_mw, alpha)
    return hourly, summary


def _print_mode_summary(s: dict) -> None:
    mode = s.get("mode", "?")
    print(f"\n{'='*68}")
    print(f"  GRIDBOOSTER COST ALLEVIATION  [mode: {mode}]")
    print(f"{'='*68}")
    for key in (
        "total_volume_mwh",
        "total_cost_eur",
        "cost_per_mw_pa",
        "congested_timestamps",
        "congestion_share_pct",
        "primary_bottleneck_line",
        "target_line",
        "flexibility_premium_eur",
    ):
        if key in s:
            print(f"  {key:<30}: {s[key]}")
    if "note" in s:
        print(f"\n  NOTE: {s['note']}")
    print(f"{'='*68}")


def _save_mode_results(
    hourly: pd.DataFrame,
    summary: dict,
    output_dir: Path,
    mode: str,
    battery_mw: float,
    alpha: float,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    tag = f"battery{int(battery_mw)}mw_alpha{alpha:.2f}_{mode.replace('-', '_')}"
    hourly.to_csv(output_dir / f"alleviation_hourly_{tag}.csv", float_format="%.4f")
    kpi_rows = {k: v for k, v in summary.items() if k != "monthly_breakdown"}
    pd.DataFrame([kpi_rows]).to_csv(
        output_dir / f"alleviation_kpi_{tag}.csv", index=False
    )
    if "monthly_breakdown" in summary:
        summary["monthly_breakdown"].to_csv(
            output_dir / f"alleviation_monthly_{tag}.csv", float_format="%.2f"
        )
    print(f"\n  [saved] alleviation_hourly_{tag}.csv")
    print(f"  [saved] alleviation_kpi_{tag}.csv")


# ══════════════════════════════════════════════════════════════════════════════
# SENSITIVITY ANALYSIS  (battery size × alpha grid)
# ══════════════════════════════════════════════════════════════════════════════

def sensitivity_analysis(
    csv_path:            Path | str,
    battery_mw_values:   list[float],
    alpha_values:        list[float],
    congestion_threshold: float = DEFAULT_CONGESTION_THRESHOLD,
    monthly_costs:       dict[int, float] | None = None,
    cost_mode:           str   = "unit_cost",
    output_dir:          Path | str | None = None,
    redispatch_cost_year: str = DEFAULT_REDISPATCH_COST_YEAR,
) -> pd.DataFrame:
    """
    Run the alleviation calculation over a grid of (battery_mw, alpha) values
    and return a DataFrame with one row per (battery_mw, alpha) combination.

    Useful for NPV break-even analysis in the paper.

    Example
    -------
    >>> sens = sensitivity_analysis(
    ...     csv_path     = "results/.../kupferzell_line_proximity_hourly_2025.csv",
    ...     battery_mw_values = [100, 150, 200, 250, 300],
    ...     alpha_values      = [0.7, 0.8, 0.9, 1.0],
    ... )
    >>> print(sens[["battery_mw", "alpha", "total_alleviation_eur",
    ...             "alleviation_per_mw_pa"]])
    """
    csv_path   = Path(csv_path)
    output_dir = Path(output_dir) if output_dir is not None else None

    if not _is_raw_occurrence_csv(csv_path):
        raise ValueError(
            f"Input CSV must be a raw congestion-occurrence file, got: {csv_path.name}"
        )

    if monthly_costs is None:
        monthly_costs = load_monthly_redispatch_costs(redispatch_cost_year)

    # Load once, reuse across grid
    df_raw = load_hourly_line_loading(csv_path)

    rows = []
    for bat in battery_mw_values:
        for al in alpha_values:
            df_calc = compute_cost_alleviation(
                df_raw,
                battery_mw            = bat,
                alpha                 = al,
                congestion_threshold  = congestion_threshold,
                monthly_costs         = monthly_costs,
                cost_mode             = cost_mode,
            )
            s = summarise_results(df_calc, bat, al, congestion_threshold)
            rows.append({
                "battery_mw":              bat,
                "alpha":                   al,
                "max_expansion_mw":        al * bat,
                "congested_timestamps":    s["congested_timestamps"],
                "congestion_share_pct":    s["congestion_share_pct"],
                "total_expansion_mwh":     s["total_expansion_mwh"],
                "total_alleviation_eur":   s["total_alleviation_eur"],
                "alleviation_per_mw_pa":   s["alleviation_per_mw_pa"],
                "peak_1pct_alleviation_eur": s["peak_1pct_alleviation_eur"],
            })

    sens_df = pd.DataFrame(rows)

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        sens_path = output_dir / "sensitivity_battery_alpha.csv"
        sens_df.to_csv(sens_path, index=False, float_format="%.4f")
        print(f"\n  [saved] Sensitivity table: {sens_path}")
    else:
        default_dir = _default_congestion_alleviation_dir(csv_path)
        default_dir.mkdir(parents=True, exist_ok=True)
        sens_path = default_dir / "sensitivity_battery_alpha.csv"
        sens_df.to_csv(sens_path, index=False, float_format="%.4f")
        print(f"\n  [saved] Sensitivity table: {sens_path}")

    return sens_df


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def _build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Compute GridBooster congestion cost alleviation from PyPSA "
            "corridor line-loading CSV files."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--source",
        choices=("dual", "overload"),
        default="dual",
        help=(
            "'dual' = read Step-1 mu_upper CSVs and compute rent-difference "
            "savings (paper primary). 'overload' = legacy loading-fraction "
            "Taylor approximation (appendix robustness check)."
        ),
    )
    p.add_argument(
        "--mu-base-csv",
        type=Path,
        default=None,
        help="Base-scenario Step-1 mu_upper csv (required if --source dual).",
    )
    p.add_argument(
        "--mu-boost-csv",
        type=Path,
        default=None,
        help="Boost-scenario mu_upper csv (optional cross-check if --source dual).",
    )
    p.add_argument(
        "--f-base-csv",
        type=Path,
        default=None,
        help="Base-scenario absolute line flows |p0| [MW] (required if --source dual).",
    )
    p.add_argument(
        "--f-boost-csv",
        type=Path,
        default=None,
        help="Boost-scenario absolute line flows |p0| [MW] (required if --source dual).",
    )
    p.add_argument(
        "--input-csv",
        type=Path,
        default=None,
        help=(
            "Path to kupferzell_line_proximity_hourly_*.csv or "
            "kupferzell_hourly_utilisation.csv.  If omitted, the script "
            "searches under --results-dir."
        ),
    )
    p.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help=(
            "Root of the results/ folder.  Used to auto-discover input CSV "
            "files when --input-csv is not specified, and as the default "
            "output location."
        ),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Directory to write output CSVs.  Defaults to the same folder "
            "as the input CSV (i.e., the relevant run-config subfolder)."
        ),
    )
    p.add_argument(
        "--battery-mw",
        type=float,
        default=DEFAULT_BATTERY_MW,
        help="GridBooster rated power capacity [MW].",
    )
    p.add_argument(
        "--alpha",
        type=float,
        default=DEFAULT_ALPHA,
        help=(
            "Virtual-transmission multiplier α ∈ (0,1].  "
            "Fraction of battery MW that translates to expanded line capacity."
        ),
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_CONGESTION_THRESHOLD,
        help="Congestion threshold (fraction of s_nom).  Match PyPSA solve value.",
    )
    p.add_argument(
        "--cost-mode",
        choices=["unit_cost", "total_monthly"],
        default="unit_cost",
        help=(
            "unit_cost: monthly_costs values are EUR/MWh.  "
            "total_monthly: values are total EUR for that month."
        ),
    )
    p.add_argument(
        "--redispatch-cost-year",
        choices=list(REDISPATCH_COST_YEAR_CHOICES),
        default=DEFAULT_REDISPATCH_COST_YEAR,
        help=(
            "Monthly redispatch-cost source to load from the CSV dataset. "
            "Choose 2022, 2023, 2024, 2025, or mean."
        ),
    )
    p.add_argument(
        "--network",
        type=Path,
        default=None,
        help=(
            "Solved PyPSA network used for map plotting. If omitted, the script "
            "auto-discovers a matching base_s_*_elec_.nc under pypsa-eur/results or resources."
        ),
    )
    p.add_argument(
        "--minimum-voltage",
        type=float,
        default=DEFAULT_MINIMUM_VOLTAGE,
        help=(
            "Minimum line voltage in kV included in the alleviation map. "
            "Set to 0 to disable voltage filtering."
        ),
    )
    p.add_argument(
        "--sensitivity",
        action="store_true",
        help=(
            "Run a (battery_mw × alpha) sensitivity grid instead of a "
            "single calculation."
        ),
    )
    p.add_argument(
        "--run-mode",
        choices=("simple", "one-line", "optimal"),
        default=None,
        help=(
            "Shadow-price alleviation mode (takes precedence over --source). "
            "'simple': full battery deployed every congested hour, no LOPF re-solve. "
            "'one-line': battery committed to --target-line; one dedicated boost LOPF. "
            "'optimal': one boost LOPF per corridor line; best Δf assigned per hour."
        ),
    )
    p.add_argument(
        "--s-nom-csv",
        type=Path,
        default=None,
        help=(
            "CSV of thermal ratings indexed by line_id "
            "(corridor_s_nom_<year>.csv from the occurrence step). "
            "Alternative to --network for loading s_nom."
        ),
    )
    p.add_argument(
        "--target-line",
        type=str,
        default=None,
        help="Line id to commit the battery to (required for --run-mode one-line).",
    )
    p.add_argument(
        "--boost-manifest-json",
        type=Path,
        default=None,
        help=(
            "JSON manifest listing per-line boost CSVs "
            "(required for --run-mode optimal). "
            "Format: [{\"line_id\": \"...\", \"f_boost_single_csv\": \"...\"}]"
        ),
    )
    return p


def _load_s_nom(args) -> "pd.Series":
    """Load thermal ratings from --s-nom-csv or --network."""
    if args.s_nom_csv is not None:
        return pd.read_csv(args.s_nom_csv, index_col=0).iloc[:, 0]
    if args.network is not None:
        return pypsa.Network(args.network).lines["s_nom"]
    raise SystemExit("--run-mode requires either --s-nom-csv or --network to load s_nom.")


def main(argv: list[str] | None = None) -> None:
    parser = _build_cli()
    args = parser.parse_args(argv)

    # ── --run-mode dispatch (takes precedence over --source) ──────────────────
    if args.run_mode is not None:
        import json as _json

        s_nom_series  = _load_s_nom(args)
        monthly_costs = load_monthly_redispatch_costs(args.redispatch_cost_year)
        out_dir       = args.output_dir
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
        mode_slug = args.run_mode.replace("-", "_")
        stem = f"battery{int(args.battery_mw)}mw_alpha{args.alpha:.2f}_{mode_slug}"

        def _out(prefix: str, ext: str = "csv") -> Path:
            name = f"alleviation_{prefix}_{stem}.{ext}"
            return (out_dir / name) if out_dir else Path(name)

        if args.run_mode == "simple":
            if args.mu_base_csv is None:
                raise SystemExit("--run-mode simple requires --mu-base-csv.")
            hourly, summary = run_shadow_simple(
                mu_base_csv=args.mu_base_csv,
                s_nom_series=s_nom_series,
                monthly_costs=monthly_costs,
                battery_mw=args.battery_mw,
                alpha=args.alpha,
            )
            hourly_csv = _out("hourly")
            kpi_csv    = _out("kpi")
            hourly.to_csv(hourly_csv, index=True, float_format="%.4f")
            pd.DataFrame([{k: v for k, v in summary.items()}]).to_csv(kpi_csv, index=False)
            if args.network is not None:
                n_map = pypsa.Network(args.network)
                cost_per_line = pd.Series(
                    {summary["target_line"]: float(hourly["cost_avoided_eur"].sum())}
                )
                map_path = _out("cost_map", "png")
                plot_congestion_alleviation_map(
                    line_saved_cost_eur=cost_per_line,
                    buses=n_map.buses,
                    lines=n_map.lines,
                    output_path=str(map_path),
                )
                print(f"[saved] cost map   : {map_path}")

        elif args.run_mode == "one-line":
            for name, val in [("--mu-base-csv", args.mu_base_csv),
                               ("--f-base-csv",  args.f_base_csv),
                               ("--f-boost-csv", args.f_boost_csv),
                               ("--target-line", args.target_line)]:
                if val is None:
                    raise SystemExit(f"--run-mode one-line requires {name}.")
            hourly, summary = run_one_line(
                mu_base_csv=args.mu_base_csv,
                f_base_csv=args.f_base_csv,
                f_boost_single_csv=args.f_boost_csv,
                target_line=args.target_line,
                s_nom_series=s_nom_series,
                monthly_costs=monthly_costs,
                battery_mw=args.battery_mw,
                alpha=args.alpha,
            )
            hourly_csv = _out("hourly")
            kpi_csv    = _out("kpi")
            hourly.to_csv(hourly_csv, index=True, float_format="%.4f")
            pd.DataFrame([{k: v for k, v in summary.items()}]).to_csv(kpi_csv, index=False)
            if args.network is not None:
                n_map = pypsa.Network(args.network)
                cost_per_line = pd.Series(
                    {args.target_line: float(hourly["cost_avoided_eur"].sum())}
                )
                map_path = _out("cost_map", "png")
                plot_congestion_alleviation_map(
                    line_saved_cost_eur=cost_per_line,
                    buses=n_map.buses,
                    lines=n_map.lines,
                    output_path=str(map_path),
                )
                print(f"[saved] cost map   : {map_path}")

        elif args.run_mode == "optimal":
            for name, val in [("--mu-base-csv",        args.mu_base_csv),
                               ("--f-base-csv",          args.f_base_csv),
                               ("--boost-manifest-json", args.boost_manifest_json)]:
                if val is None:
                    raise SystemExit(f"--run-mode optimal requires {name}.")
            manifest = _json.loads(args.boost_manifest_json.read_text())
            mu_b = pd.read_csv(args.mu_base_csv, index_col=0, parse_dates=True)
            f_b  = pd.read_csv(args.f_base_csv,  index_col=0, parse_dates=True)
            volume_optimal, assignment_log = compute_congestion_volume_mwh_perline(
                f_base=f_b, boost_manifest=manifest, mu_base=mu_b,
                battery_mw=args.battery_mw, alpha=args.alpha)
            summary = compute_cost_from_perline_volumes(volume_optimal, assignment_log, monthly_costs)
            hourly_csv = _out("hourly")
            assign_csv = _out("assignment")
            kpi_csv    = _out("kpi")
            volume_optimal.to_csv(hourly_csv, float_format="%.4f")
            assignment_log.to_csv(assign_csv, float_format="%.4f")
            pd.DataFrame([{k: v for k, v in summary.items()}]).to_csv(kpi_csv, index=False)
            print(f"[saved] assignment : {assign_csv}")
            if args.network is not None:
                n_map = pypsa.Network(args.network)
                month_cost_s = pd.Series(
                    [monthly_costs.get(ts.month, np.nan) for ts in volume_optimal.index],
                    index=volume_optimal.index,
                    dtype=float,
                )
                hourly_cost = volume_optimal["volume_mwh_optimal"] * month_cost_s
                cost_per_line = (
                    hourly_cost.groupby(volume_optimal["assigned_line"]).sum().astype(float)
                )
                map_path = _out("cost_map", "png")
                plot_congestion_alleviation_map(
                    line_saved_cost_eur=cost_per_line,
                    buses=n_map.buses,
                    lines=n_map.lines,
                    output_path=str(map_path),
                )
                print(f"[saved] cost map   : {map_path}")

        print(f"[saved] hourly     : {hourly_csv}")
        print(f"[saved] KPI        : {kpi_csv}")
        print(f"Total avoided cost : {summary['total_cost_eur'] / 1e6:.3f} M EUR")
        print(f"Congested hours    : {summary['congested_timestamps']} / {summary['total_timestamps']}")
        return

    if args.source == "dual":
        missing = [
            name for name, val in [
                ("--mu-base-csv", args.mu_base_csv),
                ("--f-base-csv",  args.f_base_csv),
                ("--f-boost-csv", args.f_boost_csv),
            ] if val is None
        ]
        if missing:
            raise SystemExit(f"--source dual requires: {', '.join(missing)}")
        if pypsa is None:
            raise SystemExit("pypsa is required for --source dual.")
        if args.network is None:
            raise SystemExit("--source dual requires --network so s_nom can be loaded.")
        n = pypsa.Network(args.network)
        monthly_costs = load_monthly_redispatch_costs(args.redispatch_cost_year)
        df_sav = compute_cost_alleviation_from_shadow(
            mu_base_csv=args.mu_base_csv,
            f_base_csv=args.f_base_csv,
            f_boost_csv=args.f_boost_csv,
            s_nom_series=n.lines["s_nom"],
            monthly_costs=monthly_costs,
            mu_boost_csv=args.mu_boost_csv,
        )
        out_dir = args.output_dir if args.output_dir else None
        out_csv = _resolve_output_path(
            args.mu_base_csv,
            out_dir,
            args.battery_mw,
            args.alpha,
        )
        df_sav.to_csv(out_csv, float_format="%.2f")
        print(f"Saved shadow-price alleviation: {out_csv}")
        print(f"Total avoided cost (primary)  : {df_sav['cost_avoided_eur'].sum() / 1e6:.2f} M EUR")
        if not df_sav["saving_eur_model"].isna().all():
            print(f"Total LP rent saving (x-check): {df_sav['saving_eur_model'].sum() / 1e6:.2f} M EUR")
        return

    # ── Resolve input CSV(s) ───────────────────────────────────────────────
    if args.input_csv is not None:
        csv_files = [args.input_csv]
    else:
        results_root = _detect_results_dir(args.results_dir)
        csv_files    = _find_hourly_csv(results_root)
        if not csv_files:
            parser.error(
                f"No Kupferzell hourly CSV found under {results_root}.\n"
                f"Specify --input-csv explicitly, or ensure the postprocessing "
                f"scripts have been run first."
            )

    print(f"Found {len(csv_files)} hourly line-loading file(s):")
    for f in csv_files:
        print(f"  {f}")

    if args.sensitivity:
        # Run sensitivity grid for each discovered file
        bat_range   = [50, 100, 150, 200, 250, 300, 400]
        alpha_range = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        for csv_path in csv_files:
            if not _is_raw_occurrence_csv(csv_path):
                print(f"  Skipping derived file: {csv_path.name}")
                continue
            out_dir = args.output_dir if args.output_dir else None
            print(f"\n  Sensitivity analysis for {csv_path.name} …")
            sens = sensitivity_analysis(
                csv_path              = csv_path,
                battery_mw_values     = bat_range,
                alpha_values          = alpha_range,
                congestion_threshold  = args.threshold,
                cost_mode             = args.cost_mode,
                output_dir            = out_dir,
                redispatch_cost_year  = args.redispatch_cost_year,
            )
            print(sens[["battery_mw", "alpha", "total_alleviation_eur",
                         "alleviation_per_mw_pa"]].to_string(index=False))
    else:
        for csv_path in csv_files:
            if not _is_raw_occurrence_csv(csv_path):
                print(f"  Skipping derived file: {csv_path.name}")
                continue
            out_dir = args.output_dir if args.output_dir else None
            run_legacy(
                csv_path              = csv_path,
                battery_mw            = args.battery_mw,
                alpha                 = args.alpha,
                congestion_threshold  = args.threshold,
                cost_mode             = args.cost_mode,
                output_dir            = out_dir,
                verbose               = True,
                redispatch_cost_year  = args.redispatch_cost_year,
                network_path          = args.network,
                minimum_voltage       = args.minimum_voltage,
            )


if __name__ == "__main__":
    main()
