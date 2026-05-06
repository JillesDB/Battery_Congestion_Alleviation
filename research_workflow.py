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
    "Method": 2,             # barrier
    "Crossover": 0,          # ENABLED — converts interior optimum to a vertex; cleans residuals
    "BarConvTol": 1e-4,
    "FeasibilityTol": 1e-3,  # tighter, paired with crossover
    "OptimalityTol": 1e-3,
    "Threads": 8,
    "NumericFocus": 1,       # gentle — was 3; aggressive setting hurt without helping
    "ScaleFlag": 2,          # keep — geometric mean scaling, pre-factorisation
    # BarHomogeneous removed — only useful for genuinely infeasible problems
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
    p.add_argument(
        "--hard-rerun",
        action="store_true",
        help=(
            "Force re-solve even if the boost flow CSV already exists. "
            "Default: skip the solve when line_flow_abs_mw_*.csv is present."
        ),
    )
    p.add_argument(
        "--keep-netcdf",
        action="store_true",
        help=(
            "Keep the solved network .nc file after extracting flows. "
            "Default: delete it to save disk (CSVs contain everything downstream needs). "
            "Always kept for the BASE solve (battery_mw==0) since duals are needed there."
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


def _optimize_with_shadow_prices(
    n: pypsa.Network,
    need_duals: bool = True,
    lp_dump_path: Path | None = None,
) -> tuple[str, str]:
    """Run Gurobi with barrier; assign all duals if need_duals=True.

    If *lp_dump_path* is provided and the solve ends non-optimally (or the
    fallback also throws), the LP is rebuilt from the network state and written
    there so the infeasibility/numerics can be inspected offline with Gurobi or
    any LP reader.  The dump is best-effort: a failure to write is warned but
    never raises.
    """
    base_options = dict(SOLVER_OPTIONS)

    def _dump_lp(reason: str) -> None:
        if lp_dump_path is None:
            return
        try:
            lp_dump_path.parent.mkdir(parents=True, exist_ok=True)
            m = n.optimize.create_model()
            m.to_file(str(lp_dump_path))
            warnings.warn(
                f"LP written for post-mortem ({reason}): {lp_dump_path}",
                RuntimeWarning,
                stacklevel=3,
            )
        except Exception as dump_exc:
            warnings.warn(
                f"LP dump requested but failed: {dump_exc}",
                RuntimeWarning,
                stacklevel=3,
            )

    try:
        status, termination = n.optimize(
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
        try:
            status, termination = n.optimize(
                solver_name=SOLVER_NAME,
                solver_options=fallback_options,
                assign_all_duals=need_duals,
            )
        except Exception as exc2:
            _dump_lp(f"exception on both attempts: {exc2}")
            raise

    if status != "ok" or termination != "optimal":
        _dump_lp(f"status={status} termination={termination}")

    return status, termination



def run_step10_solve(
        input_network: Path,
        out_dir: Path,
        battery_mw: float = 0.0,
        alpha: float = DEFAULT_ALPHA,
        target_area: str = "custom_lines",
        boost_lines: list[str] | None = None,
        hard_rerun: bool = False,
        keep_netcdf: bool = False,
) -> tuple[Path | None, Path, Path]:
    """Solve a base or boost LOPF and export per-line flow / loading CSVs.

    Caching policy
    --------------
    The boost flow CSV (``line_flow_abs_mw_<year>_<tag>.csv``) is the canonical
    cache marker. If it exists and ``hard_rerun`` is False, the entire solve
    is skipped. The companion network ``.nc`` and loading / uprate CSVs are
    treated as derivable from the flow CSV (or unnecessary downstream) and are
    NOT required to be present.

    Disk policy
    -----------
    For boost solves the ``.nc`` is deleted after the CSVs are written, unless
    ``keep_netcdf=True``. The base solve (battery_mw==0) always retains its
    ``.nc`` because shadow prices live there.
    """
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

    # Build the tag and target paths BEFORE loading anything heavy.
    if battery_mw == 0:
        tag = "base"
    elif boost_lines and len(boost_lines) == 1:
        safe_id = boost_lines[0].replace(" ", "_").replace("/", "-")
        tag = f"boost_mw{int(battery_mw)}_a{alpha:.2f}_line{safe_id}"
    else:
        tag = f"boost_mw{int(battery_mw)}_a{alpha:.2f}"

    solved        = out_dir / f"network_{SIM_YEAR}_{tag}.nc"
    flow_abs_file = out_dir / f"line_flow_abs_mw_{SIM_YEAR}_{tag}.csv"
    loading_file  = out_dir / f"line_loading_hourly_{SIM_YEAR}_{tag}.csv"
    uprate_file   = out_dir / f"line_uprate_mw_{SIM_YEAR}_{tag}.csv"

    # Cache short-circuit: presence of the flow CSV is sufficient.
    is_boost = bool(boost_lines) or battery_mw > 0
    if is_boost and flow_abs_file.exists() and not hard_rerun:
        print(f"[cache] Skipping solve — flow CSV already present: {flow_abs_file.name}")
        # Return None for the .nc path: it may have been deleted on a prior run.
        return (solved if solved.exists() else None), loading_file, flow_abs_file

    # ── Solve from scratch ────────────────────────────────────────────────
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

    lp_dump_path = out_dir / f"debug_failed_lp_{SIM_YEAR}_{tag}.lp"
    status, termination = _optimize_with_shadow_prices(
        n, need_duals=not is_boost, lp_dump_path=lp_dump_path
    )
    print(f"Solve status: {status}, termination: {termination}")

    # Acceptance criteria: base needs clean duals; boost only needs feasible flows.
    ok_terminations = {"optimal", "suboptimal"} if is_boost else {"optimal"}
    if status != "ok" or termination not in ok_terminations:
        target_id = (boost_lines[0] if boost_lines and len(boost_lines) == 1
                     else str(boost_lines))
        raise RuntimeError(
            f"LOPF for tag={tag}, line={target_id} failed: "
            f"status={status}, termination={termination}, objective={n.objective}. "
            f"LP written to {lp_dump_path} for post-mortem inspection."
        )
    if not is_boost:
        if getattr(n.lines_t, "mu_upper", None) is None or len(n.lines_t.mu_upper) == 0:
            raise RuntimeError(
                "mu_upper empty after base solve — shadow prices were not assigned."
            )

    # Extract every CSV the downstream pipeline consumes BEFORE touching the .nc.
    abs_flows = n.lines_t.p0.abs()
    abs_flows.to_csv(flow_abs_file)
    abs_flows.div(n.lines.s_nom, axis=1).to_csv(loading_file)
    uprate.to_csv(uprate_file, header=True)
    print(f"Saved: {flow_abs_file}")
    print(f"Saved: {loading_file}")
    print(f"Saved: {uprate_file}")

    # Disk policy: drop the .nc for boost solves unless explicitly retained.
    nc_kept = (not is_boost) or keep_netcdf
    if nc_kept:
        n.export_to_netcdf(solved)
        print(f"Saved: {solved}")
    else:
        # Pre-emptively remove any stale .nc from a prior run that DID retain it.
        if solved.exists():
            try:
                solved.unlink()
                print(f"Removed stale: {solved.name}")
            except OSError as exc:
                warnings.warn(f"Could not remove stale {solved}: {exc}",
                              RuntimeWarning, stacklevel=2)
        print(f"[disk] Skipping .nc export for boost solve (use --keep-netcdf to retain).")

    return (solved if nc_kept else None), loading_file, flow_abs_file


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
            hard_rerun=args.hard_rerun,
            keep_netcdf=args.keep_netcdf,
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
