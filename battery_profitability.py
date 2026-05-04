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
from plotting import generate_comparison_plot

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
# Source: ENTSO-E TYNDP standard cost assumptions for 380kV AC overhead lines.
TL_REF_CAPACITY_MW: float = 2400.0      # Typical 380kV double circuit capacity
TL_COST_PER_KM_EUR: float = 2_000_000.0 # ~2M EUR/km
TL_LENGTH_KM: float = 50.0              # Assumed congestion bottleneck length
TL_FULL_CAPEX: float = TL_COST_PER_KM_EUR * TL_LENGTH_KM
TL_CAPEX_SCALED: float = TL_FULL_CAPEX * (CAP_MW / TL_REF_CAPACITY_MW) # Scaled to 250MW
TL_OPEX_PER_YEAR: float = TL_CAPEX_SCALED * 0.01  # Standard 1% of CAPEX


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
    """Estimates profitability of a scaled 250MW transmission line expansion."""
    baseline_kpi_file = allocation_dir / f"allocation_{args.allocation_method}_dynamic_one_line_{args.year}_kpi.csv"

    if baseline_kpi_file.exists():
        baseline_df = pd.read_csv(baseline_kpi_file)
        annual_tl_revenue = (
                                baseline_df.get("annual_congestion_relief_eur", baseline_df.get("tso_revenue_eur", 0.0))
                                .iloc[0]
                            ) * multiplier
    else:
        print(f"  [Warning] Baseline {baseline_kpi_file.name} not found. Fallback to current TSO revenue.")
        annual_tl_revenue = None # Will be handled in main via fallback

    return {
        "annual_revenue": annual_tl_revenue,
        "annual_opex": TL_OPEX_PER_YEAR,
        "capex": TL_CAPEX_SCALED
    }

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

    args = parser.parse_args()
    print_validation_parameters()

    allocation_dir = args.results_root / args.scenario / "final_allocation"
    kpi_file = allocation_dir / f"allocation_{args.allocation_method}_{args.merchant_method_tag}_{args.year}_kpi.csv"

    if not kpi_file.exists():
        print(f"ERROR: KPI allocation file not found: {kpi_file}")
        sys.exit(1)

    kpi_df = pd.read_csv(kpi_file)
    annual_tso_revenue = kpi_df.get("annual_congestion_relief_eur", kpi_df.get("tso_revenue_eur", 0.0)).iloc[0]
    annual_merchant_revenue = kpi_df.get("annual_merchant_revenue_eur", kpi_df.get("merchant_revenue_eur", 0.0)).iloc[0]

    multiplier = 12 if args.scenario == "kupferzell_simple" else 1
    annual_tso_revenue *= multiplier
    annual_merchant_revenue *= multiplier

    annual_ancillary_revenue = (CAP_MW * REACTIVE_POWER_EUR_PER_MVA) + (CAP_MW * NETWORK_RESTORATION_EUR_PER_MW)
    total_annual_revenue = annual_tso_revenue + annual_merchant_revenue + annual_ancillary_revenue
    annual_cash_flow = total_annual_revenue - OPEX_PER_YEAR
    capex = VOLUME_MWH * CAPEX_PER_MWH

    gb_npv = calculate_npv(capex, annual_cash_flow, DISCOUNT_RATE, NPV_LIFETIME_YEARS)

    # Transmission Baseline Execution
    tl_baseline = get_transmission_baseline(allocation_dir, args, multiplier)
    if tl_baseline["annual_revenue"] is None:
        tl_baseline["annual_revenue"] = annual_tso_revenue

    tl_annual_cash_flow = tl_baseline["annual_revenue"] - tl_baseline["annual_opex"]
    tl_npv = calculate_npv(tl_baseline["capex"], tl_annual_cash_flow, DISCOUNT_RATE, NPV_LIFETIME_YEARS)
    tl_baseline["npv"] = tl_npv

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
        "annual_ancillary_revenue_eur": annual_ancillary_revenue,
        "total_annual_revenue_eur": total_annual_revenue,
        "annual_opex_eur": OPEX_PER_YEAR,
        "annual_net_cash_flow_eur": annual_cash_flow,
        "total_capex_eur": capex,
        "gb_npv_eur": gb_npv,
        "discount_rate": DISCOUNT_RATE,
        "npv_lifetime_years": NPV_LIFETIME_YEARS
    }])
    results_df.to_csv(out_file, index=False)

    # Plot Generation
    plot_file = out_dir / f"npv_comparison_{args.allocation_method}_{args.merchant_method_tag}_{args.year}.png"

    gb_data = {
        "annual_tso_revenue": annual_tso_revenue,
        "annual_merchant_revenue": annual_merchant_revenue,
        "annual_ancillary_revenue": annual_ancillary_revenue,
        "annual_opex": OPEX_PER_YEAR,
        "capex": capex,
        "gb_npv_eur": gb_npv
    }
    generate_comparison_plot(gb_data, tl_baseline, DISCOUNT_RATE, NPV_LIFETIME_YEARS, plot_file)


    results_df.to_csv(out_file, index=False)

    print(f"  Financial Summary ({args.scenario}):")
    print(f"  - Extrapolation multiplier : {multiplier}x")
    print(f"  - Annual TSO Congestion    : €{annual_tso_revenue:,.2f}")
    print(f"  - Annual Merchant          : €{annual_merchant_revenue:,.2f}")
    print(f"  - Annual Ancillary         : €{annual_ancillary_revenue:,.2f}")
    print(f"  - Total Annual Revenue     : €{total_annual_revenue:,.2f}")
    print(f"  - Annual Net Cash Flow     : €{annual_cash_flow:,.2f}")
    print(f"  - Total CAPEX              : €{capex:,.2f}")
    print(f"  - Net Present Value (NPV)  : €{gb_npv:,.2f}\n")
    print(f"  [saved] {out_file}")

if __name__ == "__main__":
    main()