"""Compatibility orchestrator for Step 10-12 workflow.

New scripts:
- run_validation_pypsa.py
- congestion_occurence_pypsa.py
- plotting.py
"""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import pandas as pd
import pypsa

from congestion_occurence_pypsa import run_congestion_postprocess, select_target_lines
from run_validation_pypsa import run_validation
# ─── BATTERY PARAMETERS ──────────────────────────────────────────────────────
#
# INSERT THIS BLOCK near the top of research_workflow.py
# (after the imports, before the existing main script logic).
#
# Source-of-truth dataclass for the Kupferzell GridBooster. Imported by
# merchant_revenues.py and gridbooster_allocation_model.py via:
#     from research_workflow import BatteryParams
#
# Triangulation of the 250 MW / 250 MWh sizing:
#   * Consentec/Fluence (2025) "Assessment of Grid Booster Monetary
#     Valuation" models grid-booster batteries with a 1:1 power-energy
#     ratio (their reference fleet: 600 MW / 600 MWh).
#   * The Kupferzell project's publicly reported total cost (~€100 M)
#     matches 250 MW · 120 k€/MW + 250 MWh · 300 k€/MWh = €105 M.
#     A 500 MWh sizing would imply ~€180 M, inconsistent with public reports.
#
# Round-trip efficiency 0.85 is the NREL-ATB-2024 mid-range for utility-
# scale Li-ion at AC-AC level. 25 yr / 7 % WACC are the BNetzA reference
# assumptions (Festlegung BK4-21-055) for German TSO grid infrastructure.
#
# ACTION ITEM: also update the docstring header in
# congestion_cost_alleviation.py (the line "(250 MW / 500 MWh, TransnetBW
# / Fluence 2025)") to "(250 MW / 250 MWh, 1-h duration; Fluence /
# TransnetBW 2025)" so the codebase has a single consistent specification.
from dataclasses import dataclass


@dataclass(frozen=True)
class BatteryParams:
    """Default Kupferzell GridBooster parameters (250 MW / 250 MWh, 1-h duration)."""
    cap_mw: float = 250.0           # P_nom — AC-side rated power [MW]
    volume_mwh: float = 250.0       # E_nom — usable energy capacity [MWh]
    eta: float = 0.85               # round-trip efficiency, applied on discharge side
    self_discharge_per_h: float = 0.0   # disabled for now (variable kept for future use)
    OC_eur_per_mwh: float = 2.0     # variable O&M per MWh of throughput [EUR/MWh]
    lifetime_years: int = 25
    discount_rate: float = 0.07
    capex_per_mw: float = 120_000.0     # PCS, BoP, civils                   [EUR/MW]
    capex_per_mwh: float = 300_000.0    # cells, BMS                         [EUR/MWh]


SIM_YEAR = 2025
SOLVER_NAME = "gurobi"
SOLVER_OPTIONS = {
    "Method": 2,
    "Crossover": 0,
    "BarConvTol": 1e-6,
    "FeasibilityTol": 1e-5,
    "OptimalityTol": 1e-5,
    "Threads": 8,
}
S_MAX_PU_PREVENTIVE = 0.7
DEFAULT_BATTERY_MW = 250.0
DEFAULT_ALPHA = 1.0
PROJECT_DIR = Path(__file__).resolve().parent
PYPSA_EUR_DIR = PROJECT_DIR.parent / "pypsa-eur"
DEFAULT_INPUT_NETWORK = PYPSA_EUR_DIR / "resources" / "kupferzell_2024_full" / "networks" / "base_s_256_elec_.nc"
DEFAULT_SOLVED_NETWORK = PYPSA_EUR_DIR / "results" / "kupferzell_2024_full" / "networks" / "base_s_256_elec_.nc"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "outputs" / "postprocess_full"
DEFAULT_POWERPLANTS_CSV = PYPSA_EUR_DIR / "resources" / "kupferzell_2024_full" / "powerplants_s_256.csv"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run step 10-12 workflow")
    p.add_argument(
        "--mode",
        choices=["solve", "postprocess", "all"],
        default="postprocess",
        help="solve=step10 only, postprocess=step11-12 only, all=step10-12",
    )
    p.add_argument(
        "--input-network",
        type=Path,
        default=DEFAULT_INPUT_NETWORK,
        help="Input network (typically pre-solve network from completed setup)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory for solved model and post-processing artefacts",
    )
    p.add_argument(
        "--solved-network",
        type=Path,
        default=None,
        help="Optional explicit solved network path for postprocess mode",
    )
    p.add_argument(
        "--powerplants-csv",
        type=Path,
        default=DEFAULT_POWERPLANTS_CSV,
        help="Reference powerplants file for capacity pypsa-validation",
    )
    p.add_argument(
        "--congestion-threshold",
        type=float,
        default=0.98,
        help="Congestion threshold in pu for post-processing outputs",
    )
    p.add_argument(
        "--battery-mw",
        type=float,
        default=0.0,
        help="Battery power capacity [MW]. 0 = baseline (no uprate).",
    )
    p.add_argument(
        "--alpha",
        type=float,
        default=DEFAULT_ALPHA,
        help="Virtual-transmission multiplier α ∈ (0,1].",
    )
    p.add_argument(
        "--target-area",
        choices=("kupferzell_node", "kupferzell_corridor",
                 "kupferzell_brochure_line_selection", "all","custom_lines"),
        default="kupferzell_brochure_line_selection",
        help="Which lines receive the α·P_bat uprate during the solve.",
    )
    p.add_argument(
        "--custom-lines",
        type=str,
        default="",
        help=(
            "Custom comma-separated line IDs to analyze (overrides --target-area). "
            "E.g. 'Line 5234, Line 5235' or '111,222,333'"
        ),
    )
    p.add_argument(
        "--boost-lines",
        type=str,
        default=None,
        help=(
            "Comma-separated line id(s) to restrict the boost uprate to. "
            "Overrides --target-area line selection. "
            "Required for one-line and optimal alleviation modes."
        ),
    )
    return p.parse_args()


def solved_network_path(out: Path) -> Path:
    return out / f"network_{SIM_YEAR}_solved.nc"


def line_loading_path(out: Path) -> Path:
    return out / f"line_loading_hourly_{SIM_YEAR}.csv"


def apply_booster_uprate(
    n: pypsa.Network,
    monitored: pd.Index,
    battery_mw: float,
    alpha: float,
    preventive_s_max_pu: float = S_MAX_PU_PREVENTIVE,
) -> pd.Series:
    """Raise s_max_pu on monitored lines by α·P_bat MW, split equally and capped at 1.0."""
    n.lines["s_max_pu"] = preventive_s_max_pu
    if len(monitored) == 0 or battery_mw <= 0 or alpha <= 0:
        return pd.Series(0.0, index=n.lines.index, name="uprate_mw")

    s_nom = n.lines.loc[monitored, "s_nom"].astype(float)
    per_line_mw = (alpha * battery_mw) / len(monitored)
    new_pu = (preventive_s_max_pu * s_nom + per_line_mw) / s_nom
    new_pu = new_pu.clip(upper=1.0)
    n.lines.loc[monitored, "s_max_pu"] = new_pu
    uprate_mw = (new_pu - preventive_s_max_pu) * s_nom
    return uprate_mw.reindex(n.lines.index, fill_value=0.0).rename("uprate_mw")


def _optimize_with_shadow_prices(n: pypsa.Network, need_duals: bool = True) -> tuple[str, str]:
    """Run Gurobi with barrier; assign all duals if need_duals=True."""
    base_options = dict(SOLVER_OPTIONS)
    try:
        return n.optimize(
            solver_name=SOLVER_NAME,
            solver_options=base_options,
            assign_all_duals=need_duals,
        )
    except Exception as exc:
        fallback_options = dict(base_options)
        fallback_options["Crossover"] = 1
        warnings.warn(
            "Gurobi rejected Crossover=0 on this host; falling back to Crossover=1. "
            f"Original error: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
        return n.optimize(
            solver_name=SOLVER_NAME,
            solver_options=fallback_options,
            assign_all_duals=need_duals,
        )


def run_step10_solve(
    input_network: Path,
    out_dir: Path,
    battery_mw: float = 0.0,
    alpha: float = DEFAULT_ALPHA,
    target_area: str = "custom_lines",
    boost_lines: list[str] | None = None,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    n = pypsa.Network(input_network)

    requested = boost_lines if boost_lines else []
    monitored, scope, _ = select_target_lines(
        n,
        target_area=target_area,
        requested_lines=requested,
        minimum_voltage=220.0,
    )
    uprate = apply_booster_uprate(n, monitored, battery_mw, alpha)
    print(
        f"Booster uprate: {battery_mw:.0f} MW x alpha={alpha:.2f} "
        f"split over {len(monitored)} monitored line(s) in scope '{scope}'"
    )

    # Boost re-solves only need p0 flows, not duals.
    is_boost = bool(boost_lines) or battery_mw > 0
    status, termination = _optimize_with_shadow_prices(n, need_duals=not is_boost)
    print(f"Solve status: {status}, termination: {termination}")
    if not is_boost:
        if getattr(n.lines_t, "mu_upper", None) is None or len(n.lines_t.mu_upper) == 0:
            raise RuntimeError(
                "mu_upper empty after base solve — shadow prices were not assigned. "
                "Check that assign_all_duals=True is reaching the solver."
            )

    if battery_mw == 0:
        tag = "base"
    elif boost_lines and len(boost_lines) == 1:
        safe_id = boost_lines[0].replace(" ", "_").replace("/", "-")
        tag = f"boost_mw{int(battery_mw)}_a{alpha:.2f}_line{safe_id}"
    else:
        tag = f"boost_mw{int(battery_mw)}_a{alpha:.2f}"

    solved = out_dir / f"network_{SIM_YEAR}_{tag}.nc"
    loading_file = out_dir / f"line_loading_hourly_{SIM_YEAR}_{tag}.csv"
    flow_abs_file = out_dir / f"line_flow_abs_mw_{SIM_YEAR}_{tag}.csv"
    uprate_file = out_dir / f"line_uprate_mw_{SIM_YEAR}_{tag}.csv"

    n.export_to_netcdf(solved)
    abs_flows = n.lines_t.p0.abs()
    abs_flows.to_csv(flow_abs_file)
    loading = abs_flows.div(n.lines.s_nom, axis=1)
    loading.to_csv(loading_file)
    uprate.to_csv(uprate_file, header=True)

    print(f"Saved: {solved}")
    print(f"Saved: {flow_abs_file}")
    print(f"Saved: {loading_file}")
    print(f"Saved: {uprate_file}")
    return solved, loading_file, flow_abs_file


def run_step11_12_postprocess(
    out_dir: Path,
    solved: Path,
    powerplants_csv: Path,
    threshold: float,
    target_area: str = "custom_lines",
) -> None:
    run_validation(
        network=solved,
        output_dir=out_dir,
        powerplants_csv=powerplants_csv,
    )
    run_congestion_postprocess(
        network=solved,
        output_dir=out_dir,
        threshold=threshold,
        target_area=target_area,
    )


def main() -> None:
    args = parse_args()
    out = args.output_dir

    if args.mode in {"solve", "all"}:
        boost_lines = (
            [ln.strip() for ln in args.boost_lines.split(",") if ln.strip()]
            if args.boost_lines else None
        )
        run_step10_solve(
            args.input_network,
            out,
            battery_mw=args.battery_mw,
            alpha=args.alpha,
            target_area=args.target_area,
            boost_lines=boost_lines,
        )

    if args.mode in {"postprocess", "all"}:
        solved = args.solved_network if args.solved_network is not None else DEFAULT_SOLVED_NETWORK
        run_step11_12_postprocess(
            out_dir=out,
            solved=solved,
            powerplants_csv=args.powerplants_csv,
            threshold=args.congestion_threshold,
            target_area=args.target_area,
        )


if __name__ == "__main__":
    main()
