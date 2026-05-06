"""
merchant_revenues.py
=====================

Day-ahead spot-market revenue estimation for the Kupferzell GridBooster
under two operational regimes:

  * "unconstrained"   — battery free to optimise revenue subject only to
                        physical and SoC dynamics. 365 independent 24h LPs,
                        perfect foresight within each day, price-taker,
                        daily cyclic boundary SoC_0 = SoC_24 = E_nom.

  * "tso_constrained" — TSO-priority hours (read from a TSO-mask derived
                        from the merged alleviation-revenues CSV) impose
                        SoC_t = E_nom and p_ch = p_dis = 0 in every TSO
                        hour. Merchant LP plays in the remaining hours
                        under the same daily cyclic boundary conditions.

LP formulation (per day, 24 variables of each kind):

    max  sum_t  price_t * (p_dis_t - p_ch_t) * dt          [EUR]
              - sum_t  OC * (p_ch_t + p_dis_t) * dt        [variable O&M]

    s.t.
         soc_t = soc_{t-1} + eta * p_ch_t * dt - p_dis_t * dt   (efficiency on charge)
         soc_{-1} = soc_{23} = E_nom                                (daily cyclic, full)
         0 <= p_ch_t  <= P_nom
         0 <= p_dis_t <= P_nom
         0 <= soc_t   <= E_nom
         (TSO-constrained only:) soc_t = E_nom, p_ch_t = p_dis_t = 0  for t in TSO_mask

Solver: scipy.optimize.linprog with method="highs" — open-source, license-
free, handles negative prices cleanly. Each daily LP has ~96 variables and
solves in ~ms; full year takes a few seconds.

Outputs (one CSV per regime):

    results/kupferzell_{simple|full}/4_merchant_revenues/
        dam_merchant_revenues_unconstrained_2025.csv
        dam_merchant_revenues_tso_constrained_{alleviation_method}_2025.csv

Each CSV has hourly rows with columns:
    Time_CET, price_eur_mwh, p_ch_mw, p_dis_mw, soc_mwh,
    self_discharge_mw, hourly_revenue_eur, hourly_oc_cost_eur, day_status

day_status ∈ {"ok", "dst_substituted", "infeasible"} — diagnostic flag.

CLI
---
    python3 merchant_revenues.py --mode unconstrained --scenario simple
    python3 merchant_revenues.py --mode tso_constrained \\
                                 --scenario simple \\
                                 --alleviation-method one_line
"""
from __future__ import annotations

import argparse
import sys
import warnings
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import linprog

from plotting import plot_merchant_hour_bars, plot_merchant_revenue_bars


# ─── Paths and defaults ───────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_SPOT_CSV = PROJECT_DIR / "data" / "germany_hourly_spot_prices.csv"
DEFAULT_RESULTS_ROOT = PROJECT_DIR / "results"
DEFAULT_YEAR = 2025
DEFAULT_FORESIGHT = "perfect"        # placeholder for future variants
DEFAULT_MARKET_POWER = "price_taker" # placeholder for future variants
DEFAULT_BIDDING_HORIZON_H = 24       # daily LP

SCENARIO_ALIASES = {
    "simple": "kupferzell_simple",
    "full": "kupferzell_full",
    "kupferzell_simple": "kupferzell_simple",
    "kupferzell_full": "kupferzell_full",
}

LEGACY_SCENARIO_DIRS = {
    "kupferzell_simple": "kupferzell_simple",
    "kupferzell_full": "kupferzell_full",
}

ALLEVIATION_METHOD_ALIASES = {
    "flat_one_line": "flat_one_line",
    "simple": "flat_one_line",
    "dynamic_one_line": "dynamic_one_line",
    "one_line": "dynamic_one_line",
    "dynamic_multiple_lines": "dynamic_multiple_lines",
    "optimal": "dynamic_multiple_lines",
    "optimal_alleviation": "dynamic_multiple_lines",
}

ALLEVIATION_COLUMN_CANDIDATES = {
    "flat_one_line": [
        "congestion_relief_flat_one_line_eur",
        "congestion_relief_simple_eur",
    ],
    "dynamic_one_line": [
        "congestion_relief_dynamic_one_line_eur",
        "congestion_relief_one_line_eur",
    ],
    "dynamic_multiple_lines": [
        "congestion_relief_dynamic_multiple_lines_eur",
        "congestion_relief_optimal_eur",
    ],
}


def _canonical_scenario(scenario: str) -> str:
    return SCENARIO_ALIASES.get(scenario, scenario)


def _canonical_alleviation_method(method: str) -> str:
    return ALLEVIATION_METHOD_ALIASES.get(method, method)


def _scenario_dir(results_root: Path, scenario: str, prefer_existing: bool = False) -> Path:
    canonical = _canonical_scenario(scenario)
    base = results_root / canonical
    if prefer_existing and not base.exists():
        legacy = results_root / LEGACY_SCENARIO_DIRS.get(canonical, "")
        if legacy.exists():
            return legacy
    return base


# ─── Battery params (mirrored from research_workflow.BatteryParams) ───────────
# Kept here as a fallback so this module is importable on its own. The
# preferred source of truth is research_workflow.BatteryParams; if it is
# importable we use that.
@dataclass(frozen=True)
class BatteryParams:
    cap_mw: float = 250.0
    volume_mwh: float = 250.0
    eta: float = 0.85
    self_discharge_per_h: float = 0.0
    OC_eur_per_mwh: float = 2.0
    lifetime_years: int = 25
    discount_rate: float = 0.07
    capex_per_mw: float = 120_000.0
    capex_per_mwh: float = 300_000.0


def _load_battery_params() -> BatteryParams:
    """Prefer research_workflow.BatteryParams if available, else local fallback."""
    try:
        from research_workflow import BatteryParams as _RW_Params  # type: ignore
        # Coerce to local frozen dataclass for consistency
        return BatteryParams(**{f: getattr(_RW_Params(), f)
                                for f in BatteryParams.__dataclass_fields__})
    except Exception:
        return BatteryParams()


# ══════════════════════════════════════════════════════════════════════════════
# I/O
# ══════════════════════════════════════════════════════════════════════════════

def load_spot_prices(csv_path: Path | str = DEFAULT_SPOT_CSV) -> pd.DataFrame:
    """
    Load germany_hourly_spot_prices.csv.

    The file has a 'Time_CET' timestamp column (Central European Time, with
    DST switches) and a 'price' column [EUR/MWh]. Returns a DataFrame indexed
    by the CET timestamp with a single 'price' column.
    """
    csv_path = Path(csv_path)
    df = pd.read_csv(csv_path)

    # Locate timestamp column
    ts_col = next((c for c in df.columns
                   if str(c).strip().lower() in ("time_cet", "time", "timestamp")),
                  None)
    if ts_col is None:
        raise ValueError(f"No timestamp column found in {csv_path}. "
                         f"Expected 'Time_CET'. Columns: {list(df.columns)}")

    price_col = next((c for c in df.columns
                      if str(c).strip().lower() in ("price", "price_eur_mwh",
                                                    "spot_price", "dam_price")),
                     None)
    if price_col is None:
        raise ValueError(f"No price column found in {csv_path}. "
                         f"Columns: {list(df.columns)}")

    df = df[[ts_col, price_col]].rename(columns={ts_col: "Time_CET",
                                                 price_col: "price"})
    df["Time_CET"] = pd.to_datetime(df["Time_CET"])
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna(subset=["Time_CET", "price"])
    df = df.set_index("Time_CET").sort_index()
    return df


def filter_year(prices: pd.DataFrame, year: int) -> pd.DataFrame:
    return prices.loc[prices.index.year == year].copy()


def prepare_daily_panels(prices: pd.DataFrame
                         ) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """
    Group hourly prices into calendar days. DST transition days (23h or 25h)
    are replaced with a copy of the *previous* day's 24h profile and flagged.

    Returns
    -------
    price_matrix : np.ndarray, shape (n_days, 24)
        Hourly prices [EUR/MWh] aligned to a 24-slot day index.
    timestamps   : np.ndarray, shape (n_days, 24)
        The CET datetime stamps. For substituted DST days, the original
        date is preserved on the date axis but the prices are copied from
        the previous day.
    day_flags    : list[str], length n_days
        "ok" or "dst_substituted".
    """
    prices = prices.sort_index()
    days = sorted(prices.index.normalize().unique())
    if not days:
        raise ValueError("No price data after filtering.")

    n_days = len(days)
    pm = np.full((n_days, 24), np.nan, dtype=float)
    ts = np.empty((n_days, 24), dtype="datetime64[ns]")
    flags = ["ok"] * n_days

    # First pass: load native data per day
    for i, day in enumerate(days):
        day_data = prices.loc[prices.index.normalize() == day, "price"]
        # Construct expected 24h grid for this date (naive, no DST)
        expected = pd.date_range(start=day, periods=24, freq="h")
        ts[i, :] = expected.values
        if len(day_data) == 24:
            pm[i, :] = day_data.values
        else:
            flags[i] = "dst_substituted"
            # Leave NaN for now; second pass fills from previous day

    # Second pass: substitute DST days with previous day's prices
    for i in range(n_days):
        if flags[i] == "dst_substituted":
            if i == 0:
                # Fall back to next day if DST happens to be day 0
                src = next((j for j in range(1, n_days) if flags[j] == "ok"), None)
            else:
                src = i - 1
                while src >= 0 and flags[src] != "ok":
                    src -= 1
            if src is None or src < 0:
                raise RuntimeError(f"Could not find a valid neighbour day to "
                                   f"substitute for DST day {days[i]}.")
            pm[i, :] = pm[src, :]
            warnings.warn(f"DST day {days[i].date()} substituted with prices "
                          f"from {days[src].date()}.", stacklevel=2)

    if np.isnan(pm).any():
        bad = [str(days[i].date()) for i in range(n_days)
               if np.isnan(pm[i, :]).any()]
        raise RuntimeError(f"Unfilled price slots remain on days: {bad}")

    return pm, ts, flags


# ══════════════════════════════════════════════════════════════════════════════
# LP CORE
# ══════════════════════════════════════════════════════════════════════════════
#
# Variable layout (per day, T=24 hours):
#
#   x = [ p_ch_0, ..., p_ch_{T-1},
#         p_dis_0, ..., p_dis_{T-1},
#         soc_0, ..., soc_{T-1} ]                      shape (3T,)
#
# Index helpers (constants, computed once):
#   PCH = 0..T-1
#   PDIS = T..2T-1
#   SOC = 2T..3T-1
#
# scipy.linprog minimises c^T x, so we negate the objective to maximise.

def _solve_daily_lp(prices_24: np.ndarray,
                    params: BatteryParams,
                    tso_mask: np.ndarray | None = None,
                    dt_h: float = 1.0) -> dict:
    """
    Solve a single 24h spot-market LP.

    Parameters
    ----------
    prices_24 : array shape (24,)
        Hourly EPEX day-ahead prices [EUR/MWh].
    params : BatteryParams
        Battery sizing and economic parameters.
    tso_mask : array shape (24,) of bool, optional
        If provided, every True hour locks SoC = E_nom and p_ch = p_dis = 0.
    dt_h : float
        Time-step length in hours. Default 1.0.

    Returns
    -------
    dict with keys:
        p_ch, p_dis, soc, self_discharge   — np.ndarray shape (24,)
        revenue, oc_cost, net_revenue       — floats [EUR]
        status                              — "ok" | "infeasible"
    """
    T = 24
    P = params.cap_mw
    E = params.volume_mwh
    eta = params.eta
    OC = params.OC_eur_per_mwh

    PCH = slice(0, T)
    PDIS = slice(T, 2 * T)
    SOC = slice(2 * T, 3 * T)
    n_var = 3 * T

    # ── Objective: maximise revenue minus variable O&M ───────────────────────
    # Revenue = Σ price_t * (p_dis - p_ch) * dt
    # OC cost = Σ OC * (p_ch + p_dis) * dt   (charged for throughput on either side)
    c = np.zeros(n_var)
    c[PCH] = (+prices_24 + OC) * dt_h    # minimise:  +price*p_ch + OC*p_ch
    c[PDIS] = (-prices_24 + OC) * dt_h   # minimise: -price*p_dis + OC*p_dis
    # Note: SoC has no direct objective coefficient.

    # ── Equality constraints ─────────────────────────────────────────────────
    # SoC dynamics:
    #   t = 0:   soc_0 - eta*p_ch_0*dt + p_dis_0*dt = E_nom    (since soc_{-1}=E)
    #   t > 0:   soc_t - soc_{t-1} - eta*p_ch_t*dt + p_dis_t*dt = 0
    A_eq_rows: list[np.ndarray] = []
    b_eq: list[float] = []

    for t in range(T):
        row = np.zeros(n_var)
        row[2 * T + t] = 1.0                 # +soc_t
        if t > 0:
            row[2 * T + t - 1] = -1.0        # -soc_{t-1}
        row[t] = -eta * dt_h                 # -eta*dt * p_ch_t
        row[T + t] = 1.0 * dt_h              # +dt * p_dis_t
        A_eq_rows.append(row)
        b_eq.append(E if t == 0 else 0.0)

    # Final SoC = E_nom (daily cyclic, full charge)
    row = np.zeros(n_var)
    row[2 * T + (T - 1)] = 1.0
    A_eq_rows.append(row)
    b_eq.append(E)

    # TSO-locked hours: SoC_t = E_nom AND p_ch_t = p_dis_t = 0
    if tso_mask is not None:
        for t in range(T):
            if tso_mask[t]:
                # SoC_t = E_nom
                row = np.zeros(n_var); row[2 * T + t] = 1.0
                A_eq_rows.append(row); b_eq.append(E)
                # p_ch_t = 0
                row = np.zeros(n_var); row[t] = 1.0
                A_eq_rows.append(row); b_eq.append(0.0)
                # p_dis_t = 0
                row = np.zeros(n_var); row[T + t] = 1.0
                A_eq_rows.append(row); b_eq.append(0.0)

    A_eq = np.vstack(A_eq_rows) if A_eq_rows else None
    b_eq_arr = np.array(b_eq) if b_eq else None

    # ── Variable bounds ──────────────────────────────────────────────────────
    bounds = (
            [(0.0, P)] * T +       # p_ch
            [(0.0, P)] * T +       # p_dis
            [(0.0, E)] * T         # soc
    )

    # ── Solve ────────────────────────────────────────────────────────────────
    res = linprog(
        c=c,
        A_eq=A_eq, b_eq=b_eq_arr,
        bounds=bounds,
        method="highs",
    )

    if not res.success:
        return {
            "p_ch": np.zeros(T),
            "p_dis": np.zeros(T),
            "soc": np.full(T, E),  # benign default: full battery
            "self_discharge": np.zeros(T),
            "revenue": 0.0,
            "oc_cost": 0.0,
            "net_revenue": 0.0,
            "status": "infeasible",
        }

    x = res.x
    p_ch = x[PCH]
    p_dis = x[PDIS]
    soc = x[SOC]

    revenue = float(np.sum(prices_24 * (p_dis - p_ch) * dt_h))
    oc_cost = float(np.sum(OC * (p_ch + p_dis) * dt_h))
    net = revenue - oc_cost

    return {
        "p_ch": p_ch,
        "p_dis": p_dis,
        "soc": soc,
        "self_discharge": np.zeros(T),     # placeholder — disabled for now
        "revenue": revenue,
        "oc_cost": oc_cost,
        "net_revenue": net,
        "status": "ok",
    }


# ══════════════════════════════════════════════════════════════════════════════
# YEAR-LONG DRIVERS
# ══════════════════════════════════════════════════════════════════════════════

def run_unconstrained_year(prices_csv: Path | str = DEFAULT_SPOT_CSV,
                           year: int = DEFAULT_YEAR,
                           params: BatteryParams | None = None,
                           ) -> pd.DataFrame:
    """
    Solve 365 independent unconstrained 24h LPs and assemble an hourly
    DataFrame. Returns the DataFrame; caller handles I/O.
    """
    if params is None:
        params = _load_battery_params()

    prices_all = load_spot_prices(prices_csv)
    prices = filter_year(prices_all, year)
    if prices.empty:
        raise ValueError(f"No price data for year {year} in {prices_csv}.")

    pm, ts, day_flags = prepare_daily_panels(prices)
    n_days, T = pm.shape
    assert T == 24

    rows = []
    print(f"[merchant_revenues] Solving {n_days} unconstrained daily LPs "
          f"for year {year} ...")
    for d in range(n_days):
        sol = _solve_daily_lp(pm[d], params, tso_mask=None)
        for h in range(T):
            rows.append({
                "Time_CET": pd.Timestamp(ts[d, h]),
                "price_eur_mwh": float(pm[d, h]),
                "p_ch_mw": float(sol["p_ch"][h]),
                "p_dis_mw": float(sol["p_dis"][h]),
                "soc_mwh": float(sol["soc"][h]),
                "self_discharge_mw": float(sol["self_discharge"][h]),
                "hourly_revenue_eur": float(
                    pm[d, h] * (sol["p_dis"][h] - sol["p_ch"][h])),
                "hourly_oc_cost_eur": float(
                    params.OC_eur_per_mwh
                    * (sol["p_ch"][h] + sol["p_dis"][h])),
                "day_status": (sol["status"]
                               if sol["status"] != "ok" else day_flags[d]),
            })

    df = pd.DataFrame(rows).sort_values("Time_CET").reset_index(drop=True)
    total_rev = df["hourly_revenue_eur"].sum()
    total_oc = df["hourly_oc_cost_eur"].sum()
    print(f"[merchant_revenues] Unconstrained annual gross revenue : "
          f"EUR {total_rev:>14,.2f}")
    print(f"[merchant_revenues] Unconstrained annual O&M cost      : "
          f"EUR {total_oc:>14,.2f}")
    print(f"[merchant_revenues] Unconstrained annual NET revenue   : "
          f"EUR {total_rev - total_oc:>14,.2f}")
    return df


def _build_tso_mask_from_alleviation_csv(alleviation_csv: Path,
                                         method: str,
                                         year: int,
                                         ) -> pd.Series:
    """
    Read the merged alleviation-revenues CSV and return a boolean Series
    indexed by hourly Time_CET, True wherever congestion_relief_<method>_eur
    is strictly positive.

    Expected merged CSV columns:
        Time_CET, congestion_relief_simple_eur,
                  congestion_relief_one_line_eur,
                  congestion_relief_optimal_eur
    """
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

    # Map method-name aliases to the expected column
    canonical_method = _canonical_alleviation_method(method)
    col = next(
        (candidate for candidate in ALLEVIATION_COLUMN_CANDIDATES.get(canonical_method, []) if candidate in df.columns),
        None,
    )
    if col is None or col not in df.columns:
        raise ValueError(f"Alleviation method {method!r} maps to column "
                         f"{col!r} which is not in {list(df.columns)}.")

    mask = df[col].astype(float) > 0.0
    mask.name = "tso_hour"
    return mask


def run_tso_constrained_year(prices_csv: Path | str,
                             alleviation_merged_csv: Path | str,
                             alleviation_method: str,
                             year: int = DEFAULT_YEAR,
                             params: BatteryParams | None = None,
                             ) -> pd.DataFrame:
    """
    Solve 365 daily LPs where TSO hours (those with positive congestion
    relief under the chosen alleviation_method) lock SoC = E_nom and
    p_ch = p_dis = 0.
    """
    if params is None:
        params = _load_battery_params()

    prices_all = load_spot_prices(prices_csv)
    prices = filter_year(prices_all, year)

    tso_series = _build_tso_mask_from_alleviation_csv(
        Path(alleviation_merged_csv), alleviation_method, year)

    # Align TSO mask onto the same hourly grid as prices. Any missing slots
    # default to False (= merchant-eligible).
    tso_aligned = tso_series.reindex(prices.index, fill_value=False).astype(bool)

    pm, ts, day_flags = prepare_daily_panels(prices)
    # Build the day×24 mask matrix in the same order as pm:
    n_days, T = pm.shape
    mask_mat = np.zeros((n_days, T), dtype=bool)
    for d in range(n_days):
        for h in range(T):
            tstamp = pd.Timestamp(ts[d, h])
            if tstamp in tso_aligned.index:
                mask_mat[d, h] = bool(tso_aligned.loc[tstamp])
            # Substituted DST days carry the previous day's TSO mask too —
            # acceptable approximation since TSO hours are sparse.

    rows = []
    print(f"[merchant_revenues] Solving {n_days} TSO-constrained daily LPs "
          f"for year {year}, alleviation={alleviation_method} ...")
    n_tso_hours = int(mask_mat.sum())
    print(f"[merchant_revenues]   TSO-locked hours in mask: {n_tso_hours}")

    for d in range(n_days):
        sol = _solve_daily_lp(pm[d], params, tso_mask=mask_mat[d])
        for h in range(T):
            rows.append({
                "Time_CET": pd.Timestamp(ts[d, h]),
                "price_eur_mwh": float(pm[d, h]),
                "p_ch_mw": float(sol["p_ch"][h]),
                "p_dis_mw": float(sol["p_dis"][h]),
                "soc_mwh": float(sol["soc"][h]),
                "self_discharge_mw": float(sol["self_discharge"][h]),
                "hourly_revenue_eur": float(
                    pm[d, h] * (sol["p_dis"][h] - sol["p_ch"][h])),
                "hourly_oc_cost_eur": float(
                    params.OC_eur_per_mwh
                    * (sol["p_ch"][h] + sol["p_dis"][h])),
                "tso_locked": bool(mask_mat[d, h]),
                "day_status": (sol["status"]
                               if sol["status"] != "ok" else day_flags[d]),
            })

    df = pd.DataFrame(rows).sort_values("Time_CET").reset_index(drop=True)
    total_rev = df["hourly_revenue_eur"].sum()
    total_oc = df["hourly_oc_cost_eur"].sum()
    print(f"[merchant_revenues] Constrained annual gross revenue : "
          f"EUR {total_rev:>14,.2f}")
    print(f"[merchant_revenues] Constrained annual O&M cost      : "
          f"EUR {total_oc:>14,.2f}")
    print(f"[merchant_revenues] Constrained annual NET revenue   : "
          f"EUR {total_rev - total_oc:>14,.2f}")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT PATHS & SAVE
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_output_csv(scenario: str,
                        mode: str,
                        alleviation_method: str | None,
                        year: int,
                        results_root: Path = DEFAULT_RESULTS_ROOT,
                        ) -> Path:
    """
    Output path convention:

      results/{kupferzell_simple|kupferzell_full}/4_merchant_revenues/
          dam_merchant_revenues_unconstrained_{year}.csv
          dam_merchant_revenues_tso_constrained_{alleviation_method}_{year}.csv
    """
    base = _scenario_dir(Path(results_root), scenario) / "4_merchant_revenues"
    base.mkdir(parents=True, exist_ok=True)
    if mode == "unconstrained":
        return base / f"dam_merchant_revenues_unconstrained_{year}.csv"
    elif mode == "tso_constrained":
        if alleviation_method is None:
            raise ValueError("alleviation_method required for tso_constrained mode.")
        method = _canonical_alleviation_method(alleviation_method)
        return base / (f"dam_merchant_revenues_tso_constrained_"
                       f"{method}_{year}.csv")
    raise ValueError(f"Unknown mode {mode!r}.")


def _resolve_alleviation_csv(scenario: str, year: int,
                             results_root: Path = DEFAULT_RESULTS_ROOT) -> Path:
    """The merged alleviation-revenues CSV produced by congestion_cost_alleviation.merge_alleviation_revenues."""
    results_root = Path(results_root)
    canonical = _canonical_scenario(scenario)
    path = (
        results_root
        / canonical
        / "3_congestion_alleviation"
        / f"alleviation_revenues_merged_{year}.csv"
    )
    if path.exists():
        return path
    legacy = (
        results_root
        / LEGACY_SCENARIO_DIRS.get(canonical, "")
        / "3_congestion_alleviation"
        / f"alleviation_revenues_merged_{year}.csv"
    )
    return legacy if legacy.exists() else path


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", required=True,
                   choices=["unconstrained", "tso_constrained"])
    p.add_argument("--scenario", required=True,
                   choices=["kupferzell_simple", "kupferzell_full", "simple", "full"])
    p.add_argument("--alleviation-method",
                   choices=[
                       "flat_one_line",
                       "dynamic_one_line",
                       "dynamic_multiple_lines",
                       "simple",
                       "one_line",
                       "optimal",
                       "optimal_alleviation",
                   ],
                   default=None,
                   help="Required for --mode tso_constrained.")
    p.add_argument("--year", type=int, default=DEFAULT_YEAR)
    p.add_argument("--prices-csv", default=str(DEFAULT_SPOT_CSV))
    p.add_argument("--results-root", default=str(DEFAULT_RESULTS_ROOT))
    p.add_argument("--alleviation-csv", default=None,
                   help="Override path to merged alleviation CSV. "
                        "Defaults to results/{scenario}/"
                        "congestion_alleviation/alleviation_revenues_merged_{year}.csv")
    p.add_argument("--foresight", default=DEFAULT_FORESIGHT,
                   help="Currently only 'perfect' is implemented.")
    p.add_argument("--market-power", default=DEFAULT_MARKET_POWER,
                   help="Currently only 'price_taker' is implemented.")
    args = p.parse_args()

    if args.foresight != "perfect":
        sys.exit(f"foresight={args.foresight!r} not implemented yet.")
    if args.market_power != "price_taker":
        sys.exit(f"market_power={args.market_power!r} not implemented yet.")

    params = _load_battery_params()
    print(f"[merchant_revenues] BatteryParams: {asdict(params)}")

    out_csv = _resolve_output_csv(args.scenario, args.mode,
                                  args.alleviation_method, args.year,
                                  Path(args.results_root))

    if args.mode == "unconstrained":
        df = run_unconstrained_year(args.prices_csv, args.year, params)
    else:
        if args.alleviation_method is None:
            sys.exit("--alleviation-method is required for tso_constrained mode.")
        all_csv = (Path(args.alleviation_csv) if args.alleviation_csv
                   else _resolve_alleviation_csv(args.scenario, args.year,
                                                 Path(args.results_root)))
        if not all_csv.exists():
            sys.exit(f"Alleviation CSV not found: {all_csv}\n"
                     f"Run job_congestion_alleviation.sh for all three "
                     f"alleviation methods first; the merge step produces "
                     f"this file.")
        df = run_tso_constrained_year(args.prices_csv, all_csv,
                                      args.alleviation_method, args.year,
                                      params)

    df.to_csv(out_csv, index=False, float_format="%.4f")
    print(f"[merchant_revenues] Saved: {out_csv}")
    try:
        annual_plot, monthly_plot = plot_merchant_revenue_bars(
            out_csv.parent,
            args.year,
        )
        annual_hours_plot, monthly_hours_plot = plot_merchant_hour_bars(
            out_csv.parent,
            args.year,
        )
        print(f"[merchant_revenues] Saved annual plot : {annual_plot}")
        print(f"[merchant_revenues] Saved monthly plot: {monthly_plot}")
        print(f"[merchant_revenues] Saved annual hours plot : {annual_hours_plot}")
        print(f"[merchant_revenues] Saved monthly hours plot: {monthly_hours_plot}")
    except Exception as exc:
        print(f"[merchant_revenues] WARNING: Could not update plots: {exc}")


if __name__ == "__main__":
    main()
