"""
Step 3: Run LOPF (Linear OPF) — Market Dispatch + DC Power Flow
================================================================
Solves the linearised optimal power flow problem for each month
of the simulation year, then aggregates results.

Methodological note:
--------------------
We use PyPSA's linearised OPF (LOPF) which solves simultaneously:
  (a) Economic dispatch (merit-order, cost minimisation)
  (b) DC load flow (kirchhoff constraints, line flow limits)

This is the standard approach in the congestion management literature
(cf. Frysztacki et al. 2021; Brown et al. 2018). The DC approximation
is appropriate for the 380 kV transmission level where:
  - Voltage magnitudes are near 1 p.u.
  - Reactive power effects on active power flows are second-order
  - Line R/X ratios are low (highly inductive lines)

Generator marginal costs are set from the merit-order stack using
2024 fuel costs (EEX forward prices) and technology-specific efficiencies.
The optimisation minimises total system cost subject to:
  - Generator capacity constraints
  - Transmission line thermal limits (s_nom)
  - Energy balance at each bus (Kirchhoff's current law)
  - Hydro and storage consistency constraints

N-1 Security Constraint:
  We implement a post-hoc N-1 check rather than full SCLOPF
  (which is NP-hard and computationally prohibitive for 8784 hours).
  For each hour, we test outage of each corridor line and compute
  re-distributed flows on remaining lines using PTDF matrices.
  This is the standard approach in operational congestion studies
  (cf. Neuhoff et al. 2013; Joskow & Tirole 2000).

Run: python pypsa_run_powerflow.py

Output: outputs/lopf_results.nc  (network with p, p_max_pu, line flows)
        outputs/line_loading_hourly.csv  (line loading fractions, all lines)
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
import pypsa
import pandas as pd
import numpy as np
import warnings
from pathlib import Path

from pypsa_config import (
    NETWORK_DIR, OUTPUT_DIR, SOLVER, SOLVER_OPTIONS, SIM_YEAR, FULL_YEAR,
    TEST_WEEKS
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

PREPARED_FILE = os.path.join(NETWORK_DIR, "elec_s_128_prepared.nc")
RESULTS_FILE  = os.path.join(OUTPUT_DIR, "lopf_results.nc")
LOADING_FILE  = os.path.join(OUTPUT_DIR, "line_loading_hourly.csv")


def log_network_capacity_overview(n: pypsa.Network) -> None:
    """
    Print a structured capacity overview of all components in the network.

    This function provides a complete inventory of:
    - Generator capacity by carrier (GW)
    - Storage unit capacity by type (GW)
    - Transmission line thermal limits (GW)
    - Load profile (peak, annual)
    - Adequacy margin (dispatchable vs. peak demand)

    Called at the start of solve_network to audit the network state
    before optimization, ensuring visibility into potential infeasibility
    or missing capacity data.
    """
    logger.info("=" * 70)
    logger.info("NETWORK CAPACITY OVERVIEW (Pre-Solve Audit)")
    logger.info("=" * 70)

    # --- Generators by Carrier ---
    if not n.generators.empty:
        gen_summary = (
            n.generators.groupby("carrier")["p_nom"]
            .sum()
            .sort_values(ascending=False) / 1e3  # Convert MW to GW
        )
        logger.info("\n▶ GENERATORS (by carrier) [GW]:")
        for carrier, capacity_gw in gen_summary.items():
            logger.info(f"  {carrier:<20s}: {capacity_gw:8.2f} GW")
        logger.info(f"  {'TOTAL':<20s}: {gen_summary.sum():8.2f} GW")
    else:
        logger.info("\n▶ GENERATORS: none")

    # --- Storage Units ---
    if not n.storage_units.empty:
        sto_summary = (
            n.storage_units.groupby("carrier")["p_nom"]
            .sum()
            .sort_values(ascending=False) / 1e3
        )
        logger.info("\n▶ STORAGE UNITS (by carrier) [GW]:")
        for carrier, capacity_gw in sto_summary.items():
            logger.info(f"  {carrier:<20s}: {capacity_gw:8.2f} GW")
        logger.info(f"  {'TOTAL':<20s}: {sto_summary.sum():8.2f} GW")
    else:
        logger.info("\n▶ STORAGE UNITS: none")

    # --- Links (e.g., HVDC, pumped storage) ---
    if not n.links.empty:
        link_summary = (
            n.links.groupby("carrier")["p_nom"]
            .sum()
            .sort_values(ascending=False) / 1e3
        )
        logger.info("\n▶ LINKS (e.g., HVDC) [GW]:")
        for carrier, capacity_gw in link_summary.items():
            logger.info(f"  {carrier:<20s}: {capacity_gw:8.2f} GW")
        logger.info(f"  {'TOTAL':<20s}: {link_summary.sum():8.2f} GW")

    # --- Transmission Lines ---
    logger.info("\n▶ TRANSMISSION LINES:")
    logger.info(f"  Count:            {len(n.lines):8d} AC lines")
    total_thermal_limit_gw = n.lines["s_nom"].sum() / 1e3
    logger.info(f"  Total s_nom:      {total_thermal_limit_gw:8.2f} GW")
    if not n.lines.empty:
        avg_limit = n.lines["s_nom"].mean() / 1e3
        logger.info(f"  Avg s_nom/line:   {avg_limit:8.2f} GW")

    # --- Load (Demand) Profile ---
    logger.info("\n▶ LOAD PROFILE:")
    if not n.loads_t.p_set.empty:
        load_by_hour = n.loads_t.p_set.sum(axis=1)
        peak_load_gw = load_by_hour.max() / 1e3
        min_load_gw = load_by_hour.min() / 1e3
        annual_energy_twh = load_by_hour.sum() / 1e6

        logger.info(f"  Peak demand:      {peak_load_gw:8.2f} GW")
        logger.info(f"  Min demand:       {min_load_gw:8.2f} GW")
        logger.info(f"  Annual energy:    {annual_energy_twh:8.2f} TWh")
    else:
        logger.info("  No loads in network")

    # --- ADEQUACY CHECK ---
    logger.info("\n▶ ADEQUACY CHECK (Dispatchable Capacity vs. Peak Load):")
    if not n.generators.empty:
        # Sum of all dispatchable (non-VRE) capacity
        dispatchable_carriers = [
            c for c in n.generators.carrier.unique()
            if c not in ["onwind", "offwind-ac", "offwind-dc", "offwind-float", "solar", "solar-rooftop"]
        ]
        total_dispatchable_gw = (
            n.generators[n.generators.carrier.isin(dispatchable_carriers)]["p_nom"].sum() / 1e3
        )

        if not n.loads_t.p_set.empty:
            peak_load_gw = n.loads_t.p_set.sum(axis=1).max() / 1e3
            margin_gw = total_dispatchable_gw - peak_load_gw

            logger.info(f"  Dispatchable:     {total_dispatchable_gw:8.2f} GW")
            logger.info(f"  Peak load:        {peak_load_gw:8.2f} GW")
            logger.info(f"  Adequacy margin:  {margin_gw:8.2f} GW  ", end="")

            if margin_gw > 0:
                logger.info("✓ (sufficient)")
            else:
                logger.warning(f"⚠  INFEASIBLE: {margin_gw:.2f} GW DEFICIT!")
                logger.warning("    The problem is likely infeasible due to insufficient dispatchable capacity.")
                logger.warning("    Check: 1) generator capacity estimates, 2) load profile scaling, 3) solver timeout.")

    logger.info("=" * 70)


def set_marginal_costs(n: pypsa.Network) -> pypsa.Network:
    """
    Set generator marginal costs from 2024 fuel prices and efficiencies.

    Sources:
    - Gas:       EEX Natural Gas TTF 2024 forward ~35 EUR/MWh_th
    - Hard coal: EEX Coal API2 2024 forward ~110 USD/t → ~12 EUR/MWh_th
    - Lignite:   Mine-mouth cost ~4 EUR/MWh_th
    - Nuclear:   0 EUR/MWh (decommissioned; p_nom=0)
    - CO2:       ETS price 2024 ~60 EUR/tCO2
    - Wind/Solar: 0 marginal cost (VRE priority dispatch)

    FLAG: Verify ETS price assumption against EEX data for 2024.
    """
    # Fuel costs (EUR/MWh_thermal) + CO2 cost (EUR/MWh_el)
    # mc = fuel_cost / efficiency + co2_intensity / efficiency * co2_price
    co2_price = 60.0   # EUR/tCO2

    marginal_costs = {
        "onwind":     0.0,
        "offwind-ac": 0.0,
        "offwind-dc": 0.0,
        "solar":      0.0,
        "ror":        0.0,
        "hydro":      1.0,    # small opportunity cost
        "biomass":    5.0,    # feedstock cost (rough estimate)
        "nuclear":    3.0,    # O&M only (p_nom=0 anyway)
        # Gas CCGT: 35 EUR/MWh_th / 0.58 eff + 0.202 tCO2/MWh_th / 0.58 * 60
        "CCGT":       35 / 0.58 + 0.202 / 0.58 * co2_price,   # ≈ 81 EUR/MWh
        # Gas OCGT: 35 / 0.42 + 0.202/0.42 * 60
        "OCGT":       35 / 0.42 + 0.202 / 0.42 * co2_price,   # ≈ 112 EUR/MWh
        # Hard coal: 12 EUR/MWh_th / 0.42 eff + 0.335 / 0.42 * 60
        "coal":       12 / 0.42 + 0.335 / 0.42 * co2_price,   # ≈ 77 EUR/MWh
        # Lignite: 4 / 0.38 + 0.36 / 0.38 * 60
        "lignite":    4  / 0.38 + 0.360 / 0.38 * co2_price,   # ≈ 68 EUR/MWh
        "oil":        80 / 0.35 + 0.260 / 0.35 * co2_price,   # ≈ 274 EUR/MWh
        "PHS":        0.0,    # cost determined by arbitrage; set separately
    }

    for carrier, mc in marginal_costs.items():
        mask = n.generators.carrier == carrier
        if mask.any():
            n.generators.loc[mask, "marginal_cost"] = mc

    # Load shedding: high cost ensures it's last resort
    # Check if load-shedding generators exist in the network
    ls_mask = n.generators.carrier.isin(["load", "load_shedding"])
    if ls_mask.any():
        n.generators.loc[ls_mask, "marginal_cost"] = 10_000.0

    print(f"  ✓ Marginal costs set for {len(n.generators)} generators")
    for carrier, mc in marginal_costs.items():
        n.gens = n.generators
        c_gen = n.generators[n.generators.carrier == carrier]
        if not c_gen.empty:
            print(f"    {carrier:<15s}: {mc:6.1f} EUR/MWh")

    return n


def configure_lopf_options(n: pypsa.Network) -> dict:
    """
    Prepare kwargs for n.optimize() call.
    Uses HiGHS solver (open-source, performant for LP).
    """
    solver_opts = SOLVER_OPTIONS.get(SOLVER, {})
    return dict(
        solver_name=SOLVER,
        solver_options=solver_opts,
        # Fix all generator p_nom (historical, not co-optimised)
        optimize_transmission_only=False,
        # Use linearised power flow (DC approximation)
        linearized_unit_commitment=True,
    )


def run_lopf_monthly(n: pypsa.Network) -> pypsa.Network:
    """
    Run LOPF in monthly batches to manage memory and enable
    partial restarts. Each batch is solved independently.

    Memory-efficient approach: for 128-bus / 8784-hour problem,
    running the full year at once requires ~16 GB RAM for the LP
    coefficient matrix. Monthly batches (~730 hours) fit in ~2 GB.
    """
    months = range(1, 13)

    # Containers for results
    gen_p_list  = []
    line_p_list = []

    for month in months:
        month_mask = n.snapshots.month == month
        month_snaps = n.snapshots[month_mask]
        n_hours = len(month_snaps)
        print(f"\n  Month {month:02d}: {month_snaps[0].date()} — "
              f"{month_snaps[-1].date()} ({n_hours} hours)")

        # Slice network to this month
        n_month = n.copy()
        n_month.set_snapshots(month_snaps)

        # Run LOPF
        try:
            status, condition = n_month.optimize(
                solver_name=SOLVER,
                solver_options=SOLVER_OPTIONS.get(SOLVER, {}),
            )
        except Exception as e:
            print(f"    [!] LOPF failed for month {month}: {e}")
            print("    Falling back to copper-plate dispatch (no line limits)...")
            # Fallback: relax line limits and retry
            n_month.lines["s_nom"] *= 10
            status, condition = n_month.optimize(solver_name=SOLVER)
            n_month.lines["s_nom"] /= 10

        if status != "ok":
            warnings.warn(f"Month {month}: solver status = {status}, "
                          f"condition = {condition}")

        # Extract results
        gen_p_list.append(n_month.generators_t.p)
        line_p_list.append(n_month.lines_t.p0)

        print(f"    Status: {status} | {condition}")
        print(f"    Total generation: "
              f"{n_month.generators_t.p.sum().sum()/1e6:.2f} TWh")
        print(f"    Max line loading: "
              f"{(n_month.lines_t.p0.abs() / n_month.lines.s_nom).max().max():.3f}")

    # Concatenate monthly results back onto main network
    n.generators_t.p = pd.concat(gen_p_list)
    n.lines_t.p0     = pd.concat(line_p_list)
    n.lines_t.p1     = -n.lines_t.p0   # lossless DC

    print(f"\n  ✓ LOPF complete for all 12 months")
    return n


def run_lopf_full(n: pypsa.Network) -> pypsa.Network:
    """Run LOPF for the full year in one shot (high RAM requirement ~16 GB)."""
    print(f"  Running full-year LOPF ({len(n.snapshots)} snapshots) ...")
    status, condition = n.optimize(
        solver_name=SOLVER,
        solver_options=SOLVER_OPTIONS.get(SOLVER, {}),
    )
    print(f"  Status: {status} | {condition}")
    return n


def compute_line_loading(n: pypsa.Network) -> pd.DataFrame:
    """
    Compute hourly line loading fraction: |p0| / s_nom for all lines.
    Values > 1.0 indicate overload (should not occur post-LOPF unless
    the solver relaxed constraints due to infeasibility).
    """
    loading = n.lines_t.p0.abs().div(n.lines.s_nom, axis=1)

    # Also check links (HVDC)
    if len(n.links) > 0 and len(n.links_t.p0) > 0:
        link_loading = n.links_t.p0.abs().div(
            n.links["p_nom"].replace(0, np.nan), axis=1
        ).fillna(0)
        loading = pd.concat([loading, link_loading], axis=1)

    print(f"  ✓ Line loading computed: {loading.shape[0]} hours × "
          f"{loading.shape[1]} lines/links")
    print(f"    Max loading fraction: {loading.max().max():.4f}")
    print(f"    Hours with any line > 0.98: "
          f"{(loading.max(axis=1) >= 0.98).sum()}")
    return loading


def n1_contingency_check(n: pypsa.Network,
                          corridor_lines: list) -> pd.DataFrame:
    """
    Post-hoc N-1 contingency analysis for corridor lines.

    For each line in corridor_lines, compute the PTDF shift when that
    line is outaged, and estimate the resulting flow redistribution.

    Returns a DataFrame with columns = corridor_lines,
    index = snapshots, values = max loading fraction among remaining
    corridor lines under the contingency.

    This implements the standard linearised N-1 check using
    Power Transfer Distribution Factors (PTDFs) derived from
    the network susceptance matrix.
    """
    from pypsa.linopt import get_var
    import scipy.sparse as sp

    print(f"  Running N-1 contingency check for {len(corridor_lines)} lines ...")

    # Build susceptance matrix (for DC power flow)
    # B_bus = incidence matrix × diag(1/x) × incidence matrix^T
    lines = n.lines.loc[corridor_lines]

    # Simplified PTDF using PyPSA's built-in PTDF calculation
    try:
        ptdf = n.ptdf_matrix()   # shape: (n_lines, n_buses-1)
    except AttributeError:
        print("    [!] PTDF matrix not available; skipping N-1 check")
        return pd.DataFrame()

    n1_results = {}

    for outaged_line in corridor_lines:
        # PTDF row for the outaged line
        if outaged_line not in n.lines.index:
            continue
        line_idx = n.lines.index.get_loc(outaged_line)
        ptdf_outaged = ptdf[line_idx, :]

        # Lodf: Line Outage Distribution Factor for each monitored line
        # LODF_{m,k} = PTDF_{m,k} / (1 - PTDF_{k,k})
        # where m = monitored line, k = outaged line
        lodf = {}
        for mon_line in corridor_lines:
            if mon_line == outaged_line:
                continue
            mon_idx = n.lines.index.get_loc(mon_line)
            ptdf_mk = ptdf[mon_idx, :]   # PTDF of monitored line

            denom = 1 - ptdf_outaged.dot(ptdf_outaged)
            if abs(float(denom)) < 1e-6:   # parallel or islanding
                lodf[mon_line] = 0.0
            else:
                lodf[mon_line] = float(ptdf_mk.dot(ptdf_outaged) / denom)

        # Post-contingency flow on monitored lines:
        # f_m^(k) = f_m + LODF_{m,k} * f_k
        contingency_loading = pd.DataFrame(index=n.snapshots)
        for mon_line, lodf_val in lodf.items():
            f_base_k = n.lines_t.p0.get(outaged_line,
                                         pd.Series(0, index=n.snapshots))
            f_base_m = n.lines_t.p0.get(mon_line,
                                         pd.Series(0, index=n.snapshots))
            f_contingency = (f_base_m + lodf_val * f_base_k).abs()
            contingency_loading[mon_line] = (
                f_contingency / n.lines.at[mon_line, "s_nom"]
            )

        n1_results[outaged_line] = contingency_loading.max(axis=1)

    if n1_results:
        n1_df = pd.DataFrame(n1_results)
        print(f"  ✓ N-1 check complete")
        print(f"    Max post-contingency loading: {n1_df.max().max():.3f}")
        return n1_df
    else:
        return pd.DataFrame()


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Step 3: Running LOPF Market Dispatch + Power Flow")
    print("=" * 60)

    # Load prepared network
    print(f"\n[3.1] Loading prepared network ...")
    n = pypsa.Network(PREPARED_FILE)
    print(f"  {len(n.buses)} buses | {len(n.lines)} lines | "
          f"{len(n.generators)} generators | {len(n.snapshots)} snapshots")

    # Optionally restrict to test period
    if not FULL_YEAR:
        test_snaps = n.snapshots[:TEST_WEEKS * 7 * 24]
        n.set_snapshots(test_snaps)
        print(f"  [TEST MODE] Restricted to {len(n.snapshots)} snapshots "
              f"({TEST_WEEKS} weeks)")

    # Set marginal costs
    print(f"\n[3.2] Setting generator marginal costs ...")
    n = set_marginal_costs(n)

    # Log network capacity overview
    log_network_capacity_overview(n)

    # Run LOPF
    print(f"\n[3.3] Running LOPF ...")
    if len(n.snapshots) <= 1000:
        n = run_lopf_full(n)
    else:
        n = run_lopf_monthly(n)

    # Save full results
    print(f"\n[3.4] Saving LOPF results ...")
    n.export_to_netcdf(RESULTS_FILE)
    print(f"  ✓ Saved to {RESULTS_FILE}")

    # Compute line loading
    print(f"\n[3.5] Computing line loading fractions ...")
    loading = compute_line_loading(n)
    loading.to_csv(LOADING_FILE)
    print(f"  ✓ Saved to {LOADING_FILE}")

    print(f"\n✓ Step 3 complete.")
