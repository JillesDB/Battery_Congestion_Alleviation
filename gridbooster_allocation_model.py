"""
gridbooster_allocation_model.py
================================

Hourly allocation model for the Kupferzell GridBooster: decides whether
the asset operates in TSO (congestion-relief) mode or merchant (spot
market) mode in each hour of the year, under three allocation methods.

Inputs
------
1. Merged congestion-alleviation hourly CSV
   results/kupferzell_{scenario}/congestion_alleviation/
       alleviation_revenues_merged_{year}.csv
   Columns: Time_CET, congestion_relief_simple_eur,
                      congestion_relief_one_line_eur,
                      congestion_relief_optimal_eur

2. Merchant revenue hourly CSVs (produced by merchant_revenues.py)
   results/merchant_revenues/kupferzell_{scenario}/
       dam_merchant_revenues_unconstrained_{year}.csv
       dam_merchant_revenues_tso_constrained_{method}_{year}.csv

The merchant CSV that is loaded depends on the allocation_method:
    temporal         → unconstrained only (TSO months take 0 merchant)
    tso_priority     → tso_constrained_{alleviation_method}
    optimal_revenue  → BOTH (compares unconstrained vs constrained daily)

Allocation methods
------------------
* "temporal"        — pre-defined per-month assignment via --temporal-allocation
                      (a JSON dict like {"tso":["jan","feb","mar"],
                                         "merchant":["apr","may",...]}).
                      In TSO months, the hourly value is the chosen
                      alleviation_method's congestion_relief_eur.
                      In merchant months, the hourly value is the
                      unconstrained merchant hourly_revenue_eur.

* "tso_priority"    — every hour with congestion_relief > 0 is TSO; all
                      remaining hours take the constrained merchant
                      hourly_revenue_eur.

* "optimal_revenue" — daily comparison:
                          A = sum of unconstrained merchant for the day
                          B = sum of (congestion_relief + constrained merchant)
                              for the day
                      Per day, pick max(A, B). Sum over the year =
                      absolute upper bound on annual revenue.

Output
------
results/final_allocation/kupferzell_{scenario}/
    allocation_{allocation_method}_{alleviation_method}_{year}.csv
    allocation_{allocation_method}_{alleviation_method}_{year}_kpi.csv

Hourly CSV columns:
    Time_CET, mode, congestion_relief_eur, merchant_revenue_eur,
    total_revenue_eur, soc_mwh, p_ch_mw, p_dis_mw

mode ∈ {"tso", "merchant"}.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_ROOT = PROJECT_DIR / "results"
DEFAULT_YEAR = 2025

MONTH_ALIASES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

ALLEVIATION_METHOD_ALIASES = {
    "simple":              "simple",
    "one_line":            "one_line",
    "optimal":             "optimal",
    "optimal_alleviation": "optimal",
}

ALLEVIATION_COLUMN_BY_METHOD = {
    "simple":   "congestion_relief_simple_eur",
    "one_line": "congestion_relief_one_line_eur",
    "optimal":  "congestion_relief_optimal_eur",
}


# ══════════════════════════════════════════════════════════════════════════════
# I/O
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_alleviation_csv(scenario: str, year: int,
                             results_root: Path) -> Path:
    return (results_root / f"kupferzell_{scenario}" /
            "congestion_alleviation" /
            f"alleviation_revenues_merged_{year}.csv")


def _resolve_merchant_csv(scenario: str, mode: str,
                          alleviation_method: str | None,
                          year: int, results_root: Path) -> Path:
    base = results_root / "merchant_revenues" / f"kupferzell_{scenario}"
    if mode == "unconstrained":
        return base / f"dam_merchant_revenues_unconstrained_{year}.csv"
    elif mode == "tso_constrained":
        method = ALLEVIATION_METHOD_ALIASES[alleviation_method]
        return base / f"dam_merchant_revenues_tso_constrained_{method}_{year}.csv"
    raise ValueError(f"Unknown merchant mode {mode!r}.")


def _resolve_output_paths(scenario: str, allocation_method: str,
                          alleviation_method: str, year: int,
                          results_root: Path) -> tuple[Path, Path]:
    out_dir = results_root / "final_allocation" / f"kupferzell_{scenario}"
    out_dir.mkdir(parents=True, exist_ok=True)
    method = ALLEVIATION_METHOD_ALIASES[alleviation_method]
    stem = f"allocation_{allocation_method}_{method}_{year}"
    return out_dir / f"{stem}.csv", out_dir / f"{stem}_kpi.csv"


def _load_alleviation(alleviation_csv: Path, alleviation_method: str,
                      year: int) -> pd.Series:
    df = pd.read_csv(alleviation_csv)
    ts_col = next((c for c in df.columns
                   if str(c).strip().lower() in ("time_cet", "timestamp", "time")),
                  None)
    if ts_col is None:
        raise ValueError(f"Alleviation CSV {alleviation_csv} lacks a "
                         f"timestamp column.")
    df[ts_col] = pd.to_datetime(df[ts_col])
    df = df.set_index(ts_col).sort_index()
    df = df.loc[df.index.year == year]
    method = ALLEVIATION_METHOD_ALIASES[alleviation_method]
    col = ALLEVIATION_COLUMN_BY_METHOD[method]
    if col not in df.columns:
        raise ValueError(f"Column {col!r} not in {list(df.columns)}.")
    s = df[col].astype(float).rename("congestion_relief_eur")
    return s


def _load_merchant(merchant_csv: Path, year: int) -> pd.DataFrame:
    df = pd.read_csv(merchant_csv)
    ts_col = next((c for c in df.columns
                   if str(c).strip().lower() in ("time_cet", "timestamp", "time")),
                  None)
    df[ts_col] = pd.to_datetime(df[ts_col])
    df = df.set_index(ts_col).sort_index()
    df = df.loc[df.index.year == year]
    keep = ["price_eur_mwh", "p_ch_mw", "p_dis_mw", "soc_mwh",
            "hourly_revenue_eur", "hourly_oc_cost_eur"]
    keep = [c for c in keep if c in df.columns]
    return df[keep]


# ══════════════════════════════════════════════════════════════════════════════
# ALLOCATION ROUTINES
# ══════════════════════════════════════════════════════════════════════════════

def allocate_temporal(alleviation: pd.Series,
                      merchant_unconstrained: pd.DataFrame,
                      temporal_allocation: dict,
                      ) -> pd.DataFrame:
    """
    Pre-defined monthly assignment. TSO months use congestion_relief_eur;
    merchant months use unconstrained merchant hourly_revenue_eur (net of OC).
    """
    tso_months = {MONTH_ALIASES[m.strip().lower()]
                  for m in temporal_allocation.get("tso", [])}
    merch_months = {MONTH_ALIASES[m.strip().lower()]
                    for m in temporal_allocation.get("merchant", [])}
    overlap = tso_months & merch_months
    if overlap:
        raise ValueError(f"Months assigned to both tso and merchant: {overlap}")
    all_assigned = tso_months | merch_months
    missing = set(range(1, 13)) - all_assigned
    if missing:
        raise ValueError(f"Months unassigned in temporal_allocation: {missing}")

    idx = alleviation.index.union(merchant_unconstrained.index).sort_values()
    df = pd.DataFrame(index=idx)
    df["congestion_relief_eur"] = alleviation.reindex(idx, fill_value=0.0)
    merch_cols = merchant_unconstrained.reindex(idx).fillna(0.0)
    df["merchant_revenue_eur"] = (merch_cols.get("hourly_revenue_eur", 0.0)
                                  - merch_cols.get("hourly_oc_cost_eur", 0.0))
    df["soc_mwh"] = merch_cols.get("soc_mwh", np.nan)
    df["p_ch_mw"] = merch_cols.get("p_ch_mw", np.nan)
    df["p_dis_mw"] = merch_cols.get("p_dis_mw", np.nan)

    month = df.index.month
    is_tso = pd.Series(np.isin(month, list(tso_months)), index=df.index)
    df["mode"] = np.where(is_tso, "tso", "merchant")

    # In tso months, zero out merchant; in merchant months, zero out tso
    df.loc[is_tso, "merchant_revenue_eur"] = 0.0
    df.loc[~is_tso, "congestion_relief_eur"] = 0.0
    df["total_revenue_eur"] = (df["congestion_relief_eur"]
                               + df["merchant_revenue_eur"])
    return df.reset_index().rename(columns={"index": "Time_CET"})


def allocate_tso_priority(alleviation: pd.Series,
                          merchant_constrained: pd.DataFrame,
                          ) -> pd.DataFrame:
    """
    Every hour with positive congestion_relief is TSO. In remaining hours,
    take the TSO-constrained merchant revenue (already net-of-OC if both
    present).
    """
    idx = alleviation.index.union(merchant_constrained.index).sort_values()
    df = pd.DataFrame(index=idx)
    df["congestion_relief_eur"] = alleviation.reindex(idx, fill_value=0.0)
    merch = merchant_constrained.reindex(idx).fillna(0.0)
    df["merchant_revenue_eur"] = (merch.get("hourly_revenue_eur", 0.0)
                                  - merch.get("hourly_oc_cost_eur", 0.0))
    df["soc_mwh"] = merch.get("soc_mwh", np.nan)
    df["p_ch_mw"] = merch.get("p_ch_mw", np.nan)
    df["p_dis_mw"] = merch.get("p_dis_mw", np.nan)

    is_tso = df["congestion_relief_eur"] > 0.0
    df["mode"] = np.where(is_tso, "tso", "merchant")
    # In TSO hours, the merchant LP already enforces p_ch=p_dis=0,
    # so merchant_revenue_eur should already be 0 there. Sanity-zero anyway.
    df.loc[is_tso, "merchant_revenue_eur"] = 0.0
    df["total_revenue_eur"] = (df["congestion_relief_eur"]
                               + df["merchant_revenue_eur"])
    return df.reset_index().rename(columns={"index": "Time_CET"})


def allocate_optimal_revenue(alleviation: pd.Series,
                             merchant_unconstrained: pd.DataFrame,
                             merchant_constrained: pd.DataFrame,
                             ) -> pd.DataFrame:
    """
    Daily comparison:
      A_d = sum_d unconstrained merchant net revenue
      B_d = sum_d (congestion_relief + constrained merchant net revenue)
    Pick the larger per day. Annual sum = absolute upper bound on revenue.
    """
    idx = (alleviation.index
           .union(merchant_unconstrained.index)
           .union(merchant_constrained.index)
           .sort_values())

    df = pd.DataFrame(index=idx)
    df["congestion_relief_eur"] = alleviation.reindex(idx, fill_value=0.0)

    merch_u = merchant_unconstrained.reindex(idx).fillna(0.0)
    merch_c = merchant_constrained.reindex(idx).fillna(0.0)

    df["merchant_unconstrained_eur"] = (
            merch_u.get("hourly_revenue_eur", 0.0)
            - merch_u.get("hourly_oc_cost_eur", 0.0))
    df["merchant_constrained_eur"] = (
            merch_c.get("hourly_revenue_eur", 0.0)
            - merch_c.get("hourly_oc_cost_eur", 0.0))

    # Daily totals
    day_key = df.index.normalize()
    daily = pd.DataFrame({
        "A_unconstrained": df.groupby(day_key)["merchant_unconstrained_eur"].sum(),
        "B_relief":        df.groupby(day_key)["congestion_relief_eur"].sum(),
        "B_constrained_m": df.groupby(day_key)["merchant_constrained_eur"].sum(),
    })
    daily["B_tso_priority"] = daily["B_relief"] + daily["B_constrained_m"]
    daily["choice"] = np.where(daily["A_unconstrained"] >= daily["B_tso_priority"],
                               "merchant_only", "tso_priority")

    choice_per_hour = pd.Series(daily["choice"].reindex(day_key).values,
                                index=df.index)

    # Hourly assignment
    is_merch_only = (choice_per_hour == "merchant_only")
    df["congestion_relief_eur"] = np.where(is_merch_only, 0.0,
                                           df["congestion_relief_eur"])
    df["merchant_revenue_eur"] = np.where(is_merch_only,
                                          df["merchant_unconstrained_eur"],
                                          df["merchant_constrained_eur"])
    df["mode"] = np.where(
        is_merch_only, "merchant",
        np.where(df["congestion_relief_eur"] > 0.0, "tso", "merchant"))
    df["total_revenue_eur"] = (df["congestion_relief_eur"]
                               + df["merchant_revenue_eur"])

    # Optional diagnostic: which days were tso_priority days?
    df["day_choice"] = choice_per_hour

    # SoC / p_ch / p_dis: take from whichever LP solution is in force
    for col in ("soc_mwh", "p_ch_mw", "p_dis_mw"):
        u = merch_u.get(col, pd.Series(np.nan, index=idx))
        c = merch_c.get(col, pd.Series(np.nan, index=idx))
        df[col] = np.where(is_merch_only, u, c)

    return df.reset_index().rename(columns={"index": "Time_CET"})


# ══════════════════════════════════════════════════════════════════════════════
# KPI SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def _build_kpi_table(hourly: pd.DataFrame,
                     scenario: str,
                     allocation_method: str,
                     alleviation_method: str,
                     year: int) -> pd.DataFrame:
    total_relief = float(hourly["congestion_relief_eur"].sum())
    total_merchant = float(hourly["merchant_revenue_eur"].sum())
    total = float(hourly["total_revenue_eur"].sum())
    n_tso_hours = int((hourly["mode"] == "tso").sum())
    n_merch_hours = int((hourly["mode"] == "merchant").sum())
    n_total = len(hourly)

    return pd.DataFrame([{
        "scenario": scenario,
        "allocation_method": allocation_method,
        "alleviation_method": ALLEVIATION_METHOD_ALIASES[alleviation_method],
        "year": year,
        "n_hours_total": n_total,
        "n_hours_tso_mode": n_tso_hours,
        "n_hours_merchant_mode": n_merch_hours,
        "tso_mode_share_pct": round(100.0 * n_tso_hours / max(n_total, 1), 2),
        "annual_congestion_relief_eur": round(total_relief, 2),
        "annual_merchant_revenue_eur": round(total_merchant, 2),
        "annual_total_revenue_eur": round(total, 2),
    }])


# ══════════════════════════════════════════════════════════════════════════════
# DRIVER
# ══════════════════════════════════════════════════════════════════════════════

def run(scenario: str,
        allocation_method: str,
        alleviation_method: str,
        year: int = DEFAULT_YEAR,
        results_root: Path | str = DEFAULT_RESULTS_ROOT,
        temporal_allocation: dict | None = None,
        ) -> tuple[pd.DataFrame, pd.DataFrame]:
    results_root = Path(results_root)

    if alleviation_method not in ALLEVIATION_METHOD_ALIASES:
        raise ValueError(f"alleviation_method must be one of "
                         f"{list(ALLEVIATION_METHOD_ALIASES)}, got "
                         f"{alleviation_method!r}.")
    if allocation_method not in ("temporal", "tso_priority", "optimal_revenue"):
        raise ValueError(f"allocation_method must be one of "
                         f"'temporal', 'tso_priority', 'optimal_revenue', got "
                         f"{allocation_method!r}.")

    alleviation_csv = _resolve_alleviation_csv(scenario, year, results_root)
    if not alleviation_csv.exists():
        raise FileNotFoundError(f"Merged alleviation CSV not found: "
                                f"{alleviation_csv}\nRun "
                                f"job_congestion_alleviation.sh for all "
                                f"three methods first.")

    alleviation = _load_alleviation(alleviation_csv, alleviation_method, year)

    if allocation_method == "temporal":
        if temporal_allocation is None:
            raise ValueError("--temporal-allocation is required for "
                             "allocation_method='temporal'.")
        merch_u_csv = _resolve_merchant_csv(scenario, "unconstrained",
                                            None, year, results_root)
        merchant_u = _load_merchant(merch_u_csv, year)
        hourly = allocate_temporal(alleviation, merchant_u, temporal_allocation)

    elif allocation_method == "tso_priority":
        merch_c_csv = _resolve_merchant_csv(scenario, "tso_constrained",
                                            alleviation_method, year,
                                            results_root)
        merchant_c = _load_merchant(merch_c_csv, year)
        hourly = allocate_tso_priority(alleviation, merchant_c)

    else:  # optimal_revenue
        merch_u_csv = _resolve_merchant_csv(scenario, "unconstrained",
                                            None, year, results_root)
        merch_c_csv = _resolve_merchant_csv(scenario, "tso_constrained",
                                            alleviation_method, year,
                                            results_root)
        merchant_u = _load_merchant(merch_u_csv, year)
        merchant_c = _load_merchant(merch_c_csv, year)
        hourly = allocate_optimal_revenue(alleviation, merchant_u, merchant_c)

    kpi = _build_kpi_table(hourly, scenario, allocation_method,
                           alleviation_method, year)

    out_csv, kpi_csv = _resolve_output_paths(scenario, allocation_method,
                                             alleviation_method, year,
                                             results_root)
    hourly.to_csv(out_csv, index=False, float_format="%.4f")
    kpi.to_csv(kpi_csv, index=False, float_format="%.4f")

    print("=" * 72)
    print(f"  GRIDBOOSTER ALLOCATION — {allocation_method} × "
          f"{ALLEVIATION_METHOD_ALIASES[alleviation_method]}")
    print("=" * 72)
    for k, v in kpi.iloc[0].items():
        print(f"  {k:<32s}: {v}")
    print(f"\n  Saved hourly: {out_csv}")
    print(f"  Saved KPI   : {kpi_csv}")
    return hourly, kpi


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenario", required=True, choices=["simple", "full"])
    p.add_argument("--allocation-method", required=True,
                   choices=["temporal", "tso_priority", "optimal_revenue"])
    p.add_argument("--alleviation-method", required=True,
                   choices=["simple", "one_line", "optimal", "optimal_alleviation"])
    p.add_argument("--year", type=int, default=DEFAULT_YEAR)
    p.add_argument("--results-root", default=str(DEFAULT_RESULTS_ROOT))
    p.add_argument("--temporal-allocation", default=None,
                   help='JSON dict, e.g. \'{"tso":["jan","feb","mar"],'
                        '"merchant":["apr","may","jun","jul","aug","sep","oct","nov","dec"]}\'')
    args = p.parse_args()

    temporal_allocation = (json.loads(args.temporal_allocation)
                           if args.temporal_allocation else None)

    try:
        run(args.scenario, args.allocation_method, args.alleviation_method,
            args.year, Path(args.results_root), temporal_allocation)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()