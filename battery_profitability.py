#!/usr/bin/env python3
"""
battery_profitability.py
========================
Calculates the Net Present Value (NPV) and overall project feasibility for the
Kupferzell GridBooster project by aggregating:
  1. Ancillary Services (Fixed)
  2. Merchant Revenues (Dynamic)
  3. TSO Congestion Alleviation (Dynamic)
"""

import argparse
import sys
from pathlib import Path
import pandas as pd
from plotting import generate_comparison_plot, plot_alpha_assessment

# ─── Economic and Technical Parameters ───────────────────────────────────────
# Project NPV parameters
CAPEX_PER_MWH: float = 300_000.0
OPEX_PER_YEAR: float = 25_000.0
DISCOUNT_RATE: float = 0.07
NPV_LIFETIME_YEARS: int = 20

# Internal consistency parameters (from merchant_revenues.py / allocation model)
CAP_MW: float = 250.0
VOLUME_MWH: float = 250.0
ETA: float = 0.85
SELF_DISCHARGE_PER_H: float = 0.0
OC_EUR_PER_MWH: float = 2.0
MERCHANT_LIFETIME_YEARS: int = 25  # Reference value from merchant script
CAPEX_PER_MW_REF: float = 120_000.0 # Reference value from merchant script

# Ancillary Services specific to Consentec (2022) / German Market estimates
REACTIVE_POWER_EUR_PER_MVA: float = 5000.0
NETWORK_RESTORATION_EUR_PER_MW: float = 1000.0

# ─── Transmission Line Baseline Parameters ───────────────────────────────────
# Sources:
#   - ENTSO-E TYNDP 2024 CBA Methodology, Annex 3: Investment Cost Reference Values
#     (380kV AC OHL, Central-Western Europe / Germany: €1.5–2.5 M/km; mid: €2.0 M/km)
#   - BNetzA Netzentwicklungsplan 2023–2037, Kostenkennwerte für Leitungsneubau
#     (confirms €1.8–3.0 M/km for 380kV OHL in Germany incl. planning & permitting)
#   - TransnetBW/Amprion infrastructure cost disclosures (BNetzA approval filings)
#
# Reference project: new 380kV AC overhead line, ~50 km segment in Southern Germany.
# This is the smallest discrete transmission expansion a TSO would plan to alleviate
# a 380kV bottleneck at Kupferzell. A single-circuit 380kV OHL carries ~1,500 MW
# (thermal rating), so the project delivers considerably more capacity than 250 MW.
# Total project cost is used (not capacity-scaled) as the TSO cannot build a
# fractional line; this is conservative for the battery — the TL baseline appears
# more expensive in absolute terms even though it provides more capacity.
TL_COST_PER_KM_EUR: float = 2_000_000.0   # €2 M/km, 380kV AC OHL, Germany
TL_LENGTH_KM: float = 50.0                 # Assumed bottleneck segment length
TL_SUBSTATION_EUR: float = 20_000_000.0   # Two substation bay reinforcements (~€10 M each)
TL_CAPEX_EUR: float = (TL_COST_PER_KM_EUR * TL_LENGTH_KM) + TL_SUBSTATION_EUR  # €120 M
TL_OPEX_PER_YEAR: float = TL_CAPEX_EUR * 0.008  # 0.8% of CAPEX p.a. (ENTSO-E OHL standard)

# ─── Alpha Sensitivity Assessment ────────────────────────────────────────────
ALPHA_ASSESSMENT_VALUES: list[float] = [round(i * 0.1, 1) for i in range(1, 11)]  # [0.1 … 1.0]


def calculate_npv(capex: float, annual_cash_flow: float, discount_rate: float, lifetime: int) -> float:
    """
    Computes the Net Present Value over the project's lifetime using standard
    discounted cash flow methodology.
    """
    npv = -capex
    for t in range(1, lifetime + 1):
        npv += annual_cash_flow / ((1 + discount_rate) ** t)
    return npv

def get_transmission_baseline(allocation_dir: Path, args, multiplier: int) -> dict:
    """
    Estimates profitability of a 380kV AC overhead line expansion as a baseline
    comparison for the 250MW GridBooster.

    Revenue: the dynamic one-line congestion relief stream — a transmission line
    eliminates the bottleneck and earns equivalent congestion cost savings with no
    merchant or ancillary service income.

    Cost parameters: ENTSO-E TYNDP 2024 + BNetzA NEP 2023 (see module constants).
    """
    baseline_kpi_file = allocation_dir / f"allocation_{args.allocation_method}_dynamic_one_line_{args.year}_kpi.csv"

    if baseline_kpi_file.exists():
        baseline_df = pd.read_csv(baseline_kpi_file)
        annual_tl_revenue = (
            baseline_df
            .get("annual_congestion_relief_eur", baseline_df.get("tso_revenue_eur", pd.Series([0.0])))
            .iloc[0]
        ) * multiplier
    else:
        print(f"  [Warning] TL baseline file {baseline_kpi_file.name} not found — using current TSO revenue as fallback.")
        annual_tl_revenue = None  # Handled in main()

    return {
        "annual_revenue": annual_tl_revenue,
        "annual_opex": TL_OPEX_PER_YEAR,
        "capex": TL_CAPEX_EUR,
    }


def _recompute_alpha_revenues(daily: pd.DataFrame, alpha: float) -> tuple[float, float]:
    """
    Re-applies the optimal-revenue day-level allocation for a scaled alpha and returns
    (tso_revenue, merchant_revenue) summed over all days in the daily frame.

    For each day originally assigned `tso_priority`:
      • Keep TSO if  (daily_tso × alpha + daily_constrained_merchant) ≥ daily_unconstrained_merchant
      • Flip to merchant-only otherwise — earning the unconstrained merchant value instead.
    Days originally `merchant_only` cannot flip to TSO (alpha ≤ 1.0 ≤ base alpha).
    """
    tso_mask = daily["day_choice"] == "tso_priority"
    tso_day_value = daily["tso_relief"] * alpha + daily["merch_constrained"]

    remains_tso = tso_mask & (tso_day_value >= daily["merch_unconstrained"])
    flips_merchant = tso_mask & ~remains_tso
    orig_merchant = ~tso_mask

    tso_revenue = (daily.loc[remains_tso, "tso_relief"] * alpha).sum()
    merch_revenue = (
        daily.loc[remains_tso, "merch_constrained"].sum()
        + daily.loc[flips_merchant | orig_merchant, "merch_unconstrained"].sum()
    )
    return float(tso_revenue), float(merch_revenue)


def run_alpha_profitability_assessment(
    allocation_dir: Path,
    results_root: Path,
    scratch_dir: Path,
    args,
    multiplier: int,
) -> tuple[pd.DataFrame, Path, Path]:
    """
    Sweeps alpha ∈ {0.1, 0.2, …, 1.0} for the congestion-alleviation scaling
    factor and evaluates GridBooster profitability at each level.

    Revenue model
    ─────────────
    For each alpha, the optimal-revenue allocation logic is re-applied at daily
    resolution using the existing hourly allocation CSV:
      – A TSO-priority day retains its TSO assignment only if
        (daily_tso × alpha + daily_constrained_merchant) ≥ daily_unconstrained_merchant.
      – Otherwise the day flips to merchant-only, earning the unconstrained merchant value.
    This captures the coupled effect of alpha on both TSO and realized merchant revenues.
    Ancillary revenues are alpha-invariant (fixed contract).

    If the hourly allocation file is absent (e.g. non-optimal-revenue method), the
    function falls back to linear TSO scaling with fixed merchant revenues.

    NPV metric: Equivalent Annual Annuity  EAA = NPV × CRF
    (Brealey, Myers & Allen, Principles of Corporate Finance, 13th ed., ch. 6;
    also CIMA/ICAEW: Annual Equivalent Value / AEV).  EAA converts lifetime NPV to
    a uniform annual equivalent, allowing profitability to be read directly in M€/yr.

    Intermediate per-alpha CSVs are written to scratch_dir (HPC work3 scratch, not
    the project /zhome tree).  Only the consolidated CSV and plot reach results/.

    Returns (results_df, csv_path, plot_path).
    """
    scratch_dir.mkdir(parents=True, exist_ok=True)

    # ── Load hourly allocation file for proper re-allocation ─────────────────
    hourly_file = (
        allocation_dir
        / f"allocation_{args.allocation_method}_{args.merchant_method_tag}_{args.year}.csv"
    )
    use_hourly = hourly_file.exists()

    if use_hourly:
        print(f"  [Alpha] Loading hourly allocation: {hourly_file.name}")
        hourly_df = pd.read_csv(hourly_file, parse_dates=["Time_CET"])
        hourly_df["_date"] = hourly_df["Time_CET"].dt.date
        daily = (
            hourly_df.groupby("_date")
            .agg(
                day_choice=("day_choice", "first"),
                tso_relief=("congestion_relief_eur", "sum"),
                merch_constrained=("merchant_constrained_eur", "sum"),
                merch_unconstrained=("merchant_unconstrained_eur", "sum"),
            )
            .reset_index()
        )
        # Validate against KPI totals at alpha=1.0
        base_tso_raw, base_merch_raw = _recompute_alpha_revenues(daily, 1.0)
        base_tso = base_tso_raw * multiplier
        base_merch = base_merch_raw * multiplier
    else:
        print(f"  [Alpha] Hourly file not found ({hourly_file.name}) — using KPI fallback.")
        kpi_file = (
            allocation_dir
            / f"allocation_{args.allocation_method}_{args.merchant_method_tag}_{args.year}_kpi.csv"
        )
        if not kpi_file.exists():
            print(f"  [Error] Neither hourly nor KPI file found. Aborting alpha assessment.")
            return pd.DataFrame(), Path(), Path()
        kpi_df = pd.read_csv(kpi_file)
        base_tso = (
            kpi_df.get("annual_congestion_relief_eur", kpi_df.get("tso_revenue_eur", pd.Series([0.0])))
            .iloc[0]
        ) * multiplier
        base_merch = (
            kpi_df.get("annual_merchant_revenue_eur", kpi_df.get("merchant_revenue_eur", pd.Series([0.0])))
            .iloc[0]
        ) * multiplier
        daily = None

    ancillary_revenue = (CAP_MW * REACTIVE_POWER_EUR_PER_MVA) + (CAP_MW * NETWORK_RESTORATION_EUR_PER_MW)
    capex = VOLUME_MWH * CAPEX_PER_MWH
    crf = (DISCOUNT_RATE * (1 + DISCOUNT_RATE) ** NPV_LIFETIME_YEARS) / (
        (1 + DISCOUNT_RATE) ** NPV_LIFETIME_YEARS - 1
    )

    print(f"  [Alpha] Base TSO revenue   (α=1.0) : €{base_tso:>18,.0f}")
    print(f"  [Alpha] Base merchant      (α=1.0) : €{base_merch:>18,.0f}")
    print(f"  [Alpha] Ancillary (fixed)          : €{ancillary_revenue:>18,.0f}")
    print(f"  [Alpha] CAPEX                      : €{capex:>18,.0f}")
    print(f"  [Alpha] CRF ({NPV_LIFETIME_YEARS} yr, {DISCOUNT_RATE:.0%})         : {crf:.6f}\n")

    rows = []
    for alpha in ALPHA_ASSESSMENT_VALUES:
        if use_hourly and daily is not None:
            tso_rev_raw, merch_rev_raw = _recompute_alpha_revenues(daily, alpha)
            tso_rev = tso_rev_raw * multiplier
            merch_rev = merch_rev_raw * multiplier
        else:
            tso_rev = base_tso * alpha
            merch_rev = base_merch  # fixed in fallback mode

        total_rev = tso_rev + merch_rev + ancillary_revenue
        net_cf = total_rev - OPEX_PER_YEAR
        npv = calculate_npv(capex, net_cf, DISCOUNT_RATE, NPV_LIFETIME_YEARS)
        eaa = npv * crf

        row = {
            "alpha": alpha,
            "annual_tso_revenue_eur": tso_rev,
            "annual_merchant_revenue_eur": merch_rev,
            "annual_ancillary_revenue_eur": ancillary_revenue,
            "total_annual_revenue_eur": total_rev,
            "annual_opex_eur": OPEX_PER_YEAR,
            "net_annual_cash_flow_eur": net_cf,
            "total_capex_eur": capex,
            "npv_eur": npv,
            "eaa_eur": eaa,
            "discount_rate": DISCOUNT_RATE,
            "npv_lifetime_years": NPV_LIFETIME_YEARS,
            "crf": crf,
        }
        rows.append(row)

        # Intermediate result to scratch (not project tree)
        scratch_file = (
            scratch_dir
            / f"alpha_{alpha:.2f}_{args.allocation_method}_{args.merchant_method_tag}_{args.year}.csv"
        )
        pd.DataFrame([row]).to_csv(scratch_file, index=False)

        print(
            f"  α={alpha:.1f}: TSO={tso_rev/1e6:>7.2f} M€  "
            f"Merch={merch_rev/1e6:>6.2f} M€  "
            f"NPV={npv/1e6:>8.1f} M€  "
            f"EAA={eaa/1e6:>7.2f} M€/yr"
        )

    results_df = pd.DataFrame(rows)

    # ── Persist consolidated CSV to results tree ─────────────────────────────
    out_dir = results_root / args.scenario / "npv_calculation"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{args.allocation_method}_{args.merchant_method_tag}_{args.year}"
    csv_path = out_dir / f"alpha_assessment_{tag}.csv"
    results_df.to_csv(csv_path, index=False)

    plot_path = out_dir / f"alpha_assessment_{tag}.png"

    print(f"\n  [saved] {csv_path}")
    return results_df, csv_path, plot_path


def print_validation_parameters():
    """Prints all parameters to terminal to ensure internal consistency across scripts."""
    print("════════════════════════════════════════════════════════════════════════════")
    print("  INTERNAL CONSISTENCY VALIDATION PARAMETERS")
    print("════════════════════════════════════════════════════════════════════════════")
    print(f"  cap_mw: float = {CAP_MW}")
    print(f"  volume_mwh: float = {VOLUME_MWH}")
    print(f"  eta: float = {ETA}")
    print(f"  self_discharge_per_h: float = {SELF_DISCHARGE_PER_H}")
    print(f"  OC_eur_per_mwh: float = {OC_EUR_PER_MWH}")
    print(f"  lifetime_years: int = {MERCHANT_LIFETIME_YEARS} (NPV calculated over {NPV_LIFETIME_YEARS})")
    print(f"  discount_rate: float = {DISCOUNT_RATE}")
    print(f"  capex_per_mw: float = {CAPEX_PER_MW_REF}")
    print(f"  capex_per_mwh: float = {CAPEX_PER_MWH}")
    print(f"  opex_per_year: float = {OPEX_PER_YEAR}")
    print("════════════════════════════════════════════════════════════════════════════\n")
def main():
    parser = argparse.ArgumentParser(description="GridBooster NPV Calculation")
    parser.add_argument("--scenario", required=True, choices=["kupferzell_simple", "kupferzell_full"])
    parser.add_argument("--allocation-method", required=True)
    parser.add_argument("--merchant-method-tag", required=True)
    parser.add_argument("--year", required=True)
    parser.add_argument("--results-root", required=True, type=Path)
    parser.add_argument(
        "--mode",
        choices=["profitability", "alpha_assessment"],
        default="profitability",
        help=(
            "'profitability': standard single-point NPV + TL baseline comparison (default). "
            "'alpha_assessment': sweep alpha ∈ [0.1, 1.0] and compute NPV/EAA at each level."
        ),
    )
    parser.add_argument(
        "--scratch-root",
        type=Path,
        default=None,
        help=(
            "Root directory for intermediate HPC scratch files (alpha_assessment only). "
            "Defaults to a '.scratch' subfolder inside --results-root. "
            "On DTU HPC, pass /work3/<uid>/... to keep scratch off /zhome."
        ),
    )

    args = parser.parse_args()
    print_validation_parameters()

    allocation_dir = args.results_root / args.scenario / "final_allocation"
    multiplier = 12 if args.scenario == "kupferzell_simple" else 1

    # ── Alpha assessment dispatch ─────────────────────────────────────────────
    if args.mode == "alpha_assessment":
        scratch_dir = (
            args.scratch_root / "battery_alpha_assessment"
            if args.scratch_root
            else args.results_root / ".scratch" / "alpha_assessment"
        )
        results_df, csv_path, plot_path = run_alpha_profitability_assessment(
            allocation_dir, args.results_root, scratch_dir, args, multiplier
        )
        if results_df.empty:
            sys.exit(1)
        plot_alpha_assessment(results_df, plot_path)
        print(f"  [saved] {plot_path}")
        return

    # ── Standard profitability mode ───────────────────────────────────────────
    kpi_file = allocation_dir / f"allocation_{args.allocation_method}_{args.merchant_method_tag}_{args.year}_kpi.csv"
    allocation_csv = allocation_dir / f"allocation_{args.allocation_method}_{args.merchant_method_tag}_{args.year}.csv"

    if not kpi_file.exists():
        print(f"ERROR: KPI allocation file not found: {kpi_file}")
        sys.exit(1)

    kpi_df = pd.read_csv(kpi_file)
    annual_tso_revenue = (
        kpi_df.get("annual_congestion_relief_eur", kpi_df.get("tso_revenue_eur", 0.0))
        .iloc[0]
    )
    annual_merchant_revenue = (
        kpi_df.get("annual_merchant_revenue_eur", kpi_df.get("merchant_revenue_eur", 0.0))
        .iloc[0]
    )

    # Variable O&M: throughput-based cost using hourly charge/discharge if available.
    if allocation_csv.exists():
        allocation_df = pd.read_csv(allocation_csv)
        p_ch = pd.to_numeric(allocation_df.get("p_ch_mw", 0.0), errors="coerce").fillna(0.0)
        p_dis = pd.to_numeric(allocation_df.get("p_dis_mw", 0.0), errors="coerce").fillna(0.0)
        annual_throughput_mwh = float((p_ch + p_dis).sum())
    else:
        annual_throughput_mwh = 0.0
        print(f"WARNING: Allocation CSV not found for throughput O&M: {allocation_csv}")
    annual_variable_om = annual_throughput_mwh * OC_EUR_PER_MWH
    annual_merchant_revenue_gross = annual_merchant_revenue + annual_variable_om

    # 3. Extrapolate if simple scenario (multiplier already computed above)
    annual_tso_revenue *= multiplier
    annual_merchant_revenue *= multiplier
    annual_merchant_revenue_gross *= multiplier
    annual_variable_om *= multiplier
    annual_ancillary_revenue = (CAP_MW * REACTIVE_POWER_EUR_PER_MVA) + (CAP_MW * NETWORK_RESTORATION_EUR_PER_MW)
    total_annual_revenue = annual_tso_revenue + annual_merchant_revenue_gross + annual_ancillary_revenue
    annual_opex = OPEX_PER_YEAR + annual_variable_om
    annual_cash_flow = total_annual_revenue - annual_opex
    capex = VOLUME_MWH * CAPEX_PER_MWH

    gb_npv = calculate_npv(capex, annual_cash_flow, DISCOUNT_RATE, NPV_LIFETIME_YEARS)

    # Transmission Baseline Execution
    tl_baseline = get_transmission_baseline(allocation_dir, args, multiplier)
    if tl_baseline["annual_revenue"] is None:
        tl_baseline["annual_revenue"] = annual_tso_revenue

    tl_annual_cash_flow = tl_baseline["annual_revenue"] - tl_baseline["annual_opex"]
    tl_npv = calculate_npv(tl_baseline["capex"], tl_annual_cash_flow, DISCOUNT_RATE, NPV_LIFETIME_YEARS)
    tl_baseline["tl_npv_eur"] = tl_npv

    # Save CSV
    out_dir = args.results_root / args.scenario / "npv_calculation"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"npv_results_{args.allocation_method}_{args.merchant_method_tag}_{args.year}.csv"

    results_df = pd.DataFrame([{
        "scenario": args.scenario,
        "allocation_method": args.allocation_method,
        "merchant_method_tag": args.merchant_method_tag,
        "year": args.year,
        "extrapolation_multiplier": multiplier,
        "annual_tso_revenue_eur": annual_tso_revenue,
        "annual_merchant_revenue_eur": annual_merchant_revenue,
        "annual_merchant_revenue_gross_eur": annual_merchant_revenue_gross,
        "annual_ancillary_revenue_eur": annual_ancillary_revenue,
        "annual_variable_om_eur": annual_variable_om,
        "annual_fixed_opex_eur": OPEX_PER_YEAR,
        "annual_opex_eur": annual_opex,
        "total_annual_revenue_eur": total_annual_revenue,
        "annual_net_cash_flow_eur": annual_cash_flow,
        "total_capex_eur": capex,
        "gb_npv_eur": gb_npv,
        "npv_eur": gb_npv,
        "discount_rate": DISCOUNT_RATE,
        "npv_lifetime_years": NPV_LIFETIME_YEARS
    }])
    results_df.to_csv(out_file, index=False)

    # Plot Generation
    plot_file = out_dir / f"npv_comparison_{args.allocation_method}_{args.merchant_method_tag}_{args.year}.png"

    gb_data = {
        "annual_tso_revenue_eur": annual_tso_revenue,
        "annual_merchant_revenue_eur": annual_merchant_revenue,
        "annual_ancillary_revenue_eur": annual_ancillary_revenue,
        "annual_opex_eur": annual_opex,
        "total_capex_eur": capex,
        "npv_eur": gb_npv,
        "gb_npv_eur": gb_npv,
    }
    generate_comparison_plot(gb_data, tl_baseline, DISCOUNT_RATE, NPV_LIFETIME_YEARS, plot_file)

    print(f"  Financial Summary ({args.scenario}):")
    print(f"  - Extrapolation multiplier : {multiplier}x")
    print(f"  - Annual TSO Congestion    : €{annual_tso_revenue:,.2f}")
    print(f"  - Annual Merchant (net)    : €{annual_merchant_revenue:,.2f}")
    print(f"  - Annual Merchant (gross)  : €{annual_merchant_revenue_gross:,.2f}")
    print(f"  - Annual Ancillary         : €{annual_ancillary_revenue:,.2f}")
    print(f"  - Annual Variable O&M      : €{annual_variable_om:,.2f}")
    print(f"  - Annual Fixed OPEX        : €{OPEX_PER_YEAR:,.2f}")
    print(f"  - Total Annual Revenue     : €{total_annual_revenue:,.2f}")
    print(f"  - Annual Net Cash Flow     : €{annual_cash_flow:,.2f}")
    print(f"  - Total CAPEX              : €{capex:,.2f}")
    print(f"  - Net Present Value (NPV)  : €{gb_npv:,.2f}\n")
    print(f"  Transmission Line Baseline (380kV AC OHL, ~50km):")
    print(f"  - Annual TL Revenue        : €{tl_baseline['annual_revenue']:,.2f}")
    print(f"  - Annual TL OPEX           : €{tl_baseline['annual_opex']:,.2f}")
    print(f"  - TL CAPEX                 : €{tl_baseline['capex']:,.2f}")
    print(f"  - TL Net Present Value     : €{tl_npv:,.2f}\n")
    print(f"  [saved] {out_file}")
    print(f"  [saved] {plot_file}")

if __name__ == "__main__":
    main()