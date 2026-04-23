"""Compatibility orchestrator for Step 10-12 workflow.

New scripts:
- run_validation_pypsa.py
- congestion_occurence_pypsa.py
- plotting.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pypsa

from congestion_occurence_pypsa import run_congestion_postprocess
from run_validation_pypsa import run_validation

SIM_YEAR = 2025
SOLVER_NAME = "highs"
SOLVER_OPTIONS = {"presolve": "on", "parallel": "on"}
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
    return p.parse_args()


def solved_network_path(out: Path) -> Path:
    return out / f"network_{SIM_YEAR}_solved.nc"


def line_loading_path(out: Path) -> Path:
    return out / f"line_loading_hourly_{SIM_YEAR}.csv"


def run_step10_solve(input_network: Path, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    n = pypsa.Network(input_network)
    status, termination = n.optimize(
        solver_name=SOLVER_NAME,
        solver_options=SOLVER_OPTIONS,
    )
    print(f"Solve status: {status}, termination: {termination}")

    solved = solved_network_path(out_dir)
    loading_file = line_loading_path(out_dir)

    n.export_to_netcdf(solved)
    loading = n.lines_t.p0.abs().div(n.lines.s_nom, axis=1)
    loading.to_csv(loading_file)

    print(f"Saved: {solved}")
    print(f"Saved: {loading_file}")
    return solved, loading_file


def run_step11_12_postprocess(
    out_dir: Path,
    solved: Path,
    powerplants_csv: Path,
    threshold: float,
) -> None:
    run_validation(
        network=solved,
        output_dir=out_dir,
        powerplants_csv=powerplants_csv,
    )
    run_congestion_postprocess(network=solved, output_dir=out_dir, threshold=threshold)


def main() -> None:
    args = parse_args()
    out = args.output_dir

    if args.mode in {"solve", "all"}:
        run_step10_solve(args.input_network, out)

    if args.mode in {"postprocess", "all"}:
        solved = args.solved_network if args.solved_network is not None else DEFAULT_SOLVED_NETWORK
        run_step11_12_postprocess(
            out_dir=out,
            solved=solved,
            powerplants_csv=args.powerplants_csv,
            threshold=args.congestion_threshold,
        )


if __name__ == "__main__":
    main()
