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
DEFAULT_SOLVED_NETWORK = PYPSA_EUR_DIR / "results" / "kupferzell_2024_simple" / "networks" / "base_s_256_elec_.nc"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "outputs" / "postprocess_simple"
DEFAULT_POWERPLANTS_CSV = PYPSA_EUR_DIR / "resources" / "kupferzell_2024_simple" / "powerplants_s_256.csv"


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
        choices=("corridor", "kupferzell", "all"),
        default="corridor",
        help="Which lines receive the α·P_bat uprate during the solve.",
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


def _optimize_with_shadow_prices(n: pypsa.Network) -> tuple[str, str]:
    """Run Gurobi with barrier shadow prices, allowing the documented crossover fallback."""
    try:
        return n.optimize(
            solver_name=SOLVER_NAME,
            solver_options=SOLVER_OPTIONS,
            keep_shadowprices=True,
        )
    except Exception as exc:
        fallback_options = dict(SOLVER_OPTIONS)
        fallback_options["Crossover"] = 1
        warnings.warn(
            "Gurobi rejected Method=2/Crossover=0 on this host; falling back to "
            "Method=2/Crossover=1 to preserve shadow prices. "
            f"Original error: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
        return n.optimize(
            solver_name=SOLVER_NAME,
            solver_options=fallback_options,
            keep_shadowprices=True,
        )


def run_step10_solve(
    input_network: Path,
    out_dir: Path,
    battery_mw: float = 0.0,
    alpha: float = DEFAULT_ALPHA,
    target_area: str = "corridor",
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    n = pypsa.Network(input_network)

    monitored, scope, _ = select_target_lines(
        n,
        target_area=target_area,
        requested_lines=[],
        minimum_voltage=220.0,
    )
    uprate = apply_booster_uprate(n, monitored, battery_mw, alpha)
    print(
        f"Booster uprate: {battery_mw:.0f} MW x alpha={alpha:.2f} "
        f"split over {len(monitored)} monitored line(s) in scope '{scope}'"
    )

    status, termination = _optimize_with_shadow_prices(n)
    print(f"Solve status: {status}, termination: {termination}")
    if getattr(n.lines_t, "mu_upper", None) is None or len(n.lines_t.mu_upper) == 0:
        raise RuntimeError(
            "mu_upper empty after solve — Gurobi did not return shadow prices. "
            "Check solver options and keep_shadowprices=True."
        )

    tag = "base" if battery_mw == 0 else f"boost_mw{int(battery_mw)}_a{alpha:.2f}"
    solved = out_dir / f"network_{SIM_YEAR}_{tag}.nc"
    loading_file = out_dir / f"line_loading_hourly_{SIM_YEAR}_{tag}.csv"
    uprate_file = out_dir / f"line_uprate_mw_{SIM_YEAR}_{tag}.csv"

    n.export_to_netcdf(solved)
    loading = n.lines_t.p0.abs().div(n.lines.s_nom, axis=1)
    loading.to_csv(loading_file)
    uprate.to_csv(uprate_file, header=True)

    print(f"Saved: {solved}")
    print(f"Saved: {loading_file}")
    print(f"Saved: {uprate_file}")
    return solved, loading_file


def run_step11_12_postprocess(
    out_dir: Path,
    solved: Path,
    powerplants_csv: Path,
    threshold: float,
    target_area: str = "corridor",
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
        run_step10_solve(
            args.input_network,
            out,
            battery_mw=args.battery_mw,
            alpha=args.alpha,
            target_area=args.target_area,
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
