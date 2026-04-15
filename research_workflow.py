"""Minimal workflow for Step 10-12 of the Kupferzell congestion project.

Step 10: Solve full-year market dispatch / power flow.
Step 11: Validate solved model behaviour.
Step 12: Export congestion statistics and Kupferzell-focused outputs.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import pypsa

matplotlib.use("Agg")
import matplotlib.pyplot as plt

SIM_YEAR = 2025
CONGESTION_THRESHOLD = 0.98
KUPFERZELL_LAT = 49.2333
KUPFERZELL_LON = 9.6833
KUPFERZELL_RADIUS_DEG = 0.8
SOLVER_NAME = "highs"
SOLVER_OPTIONS = {"presolve": "on", "parallel": "on"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run step 10-12 workflow")
    p.add_argument(
        "--mode",
        choices=["solve", "postprocess", "all"],
        default="all",
        help="solve=step10 only, postprocess=step11-12 only, all=step10-12",
    )
    p.add_argument(
        "--input-network",
        type=Path,
        default=Path("data/networks/base_s_256_elec_.nc"),
        help="Input network (typically pre-solve network from completed setup)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/step10_12"),
        help="Output directory for solved model and post-processing artefacts",
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


def validate_model(n: pypsa.Network, loading: pd.DataFrame, out_dir: Path) -> pd.DataFrame:
    checks: list[tuple[str, str, str]] = []
    checks.append(("snapshots_present", "pass" if len(n.snapshots) > 0 else "fail", str(len(n.snapshots))))
    checks.append(("line_flows_present", "pass" if not n.lines_t.p0.empty else "fail", str(n.lines_t.p0.shape)))

    if n.loads_t.p_set.empty:
        checks.append(("load_timeseries_present", "fail", "No load timeseries"))
    else:
        total_load = n.loads_t.p_set.sum(axis=1)
        checks.append(("load_nonnegative", "pass" if (total_load >= 0).all() else "fail", f"min={total_load.min():.2f} MW"))

    violations = int((loading > 1.001).sum().sum())
    checks.append(("line_limit_violations", "pass" if violations == 0 else "warning", f"cells={violations}"))

    if np.isfinite(getattr(n, "objective", np.nan)):
        checks.append(("objective_finite", "pass", f"{float(n.objective):.2f}"))
    else:
        checks.append(("objective_finite", "warning", "objective not stored in netcdf"))

    out = pd.DataFrame(checks, columns=["check", "status", "detail"])
    out.to_csv(out_dir / "model_validation_summary.csv", index=False)
    return out


def find_kupferzell_lines(n: pypsa.Network) -> pd.Index:
    buses = n.buses.copy()
    buses["dist_deg"] = np.sqrt((buses["y"] - KUPFERZELL_LAT) ** 2 + (buses["x"] - KUPFERZELL_LON) ** 2)
    near = buses[buses["dist_deg"] <= KUPFERZELL_RADIUS_DEG].index
    lines = n.lines[n.lines.bus0.isin(near) | n.lines.bus1.isin(near)]
    return lines.index


def export_congestion(n: pypsa.Network, loading: pd.DataFrame, out_dir: Path) -> None:
    flags = (loading >= CONGESTION_THRESHOLD).astype(int)
    flags.to_csv(out_dir / f"congestion_hourly_flags_{SIM_YEAR}.csv")

    summary = pd.DataFrame(index=loading.columns)
    summary["bus0"] = n.lines.reindex(summary.index)["bus0"]
    summary["bus1"] = n.lines.reindex(summary.index)["bus1"]
    summary["s_nom_mw"] = n.lines.reindex(summary.index)["s_nom"]
    summary["mean_loading"] = loading.mean()
    summary["p95_loading"] = loading.quantile(0.95)
    summary["max_loading"] = loading.max()
    summary["congested_hours"] = flags.sum()
    summary["congested_share_pct"] = 100.0 * summary["congested_hours"] / len(loading)
    summary = summary.sort_values("congested_hours", ascending=False)
    summary.to_csv(out_dir / f"congestion_by_line_{SIM_YEAR}.csv")

    kup_lines = find_kupferzell_lines(n)
    rows = []
    for line in kup_lines:
        for ts, value in loading[line].items():
            rows.append(
                {
                    "timestamp": ts,
                    "line": line,
                    "bus0": n.lines.at[line, "bus0"],
                    "bus1": n.lines.at[line, "bus1"],
                    "s_nom_mw": float(n.lines.at[line, "s_nom"]),
                    "loading_fraction": float(value),
                    "distance_to_capacity_fraction": float(1 - value),
                    "at_or_above_capacity_threshold": int(value >= CONGESTION_THRESHOLD),
                }
            )
    pd.DataFrame(rows).to_csv(out_dir / f"kupferzell_line_proximity_hourly_{SIM_YEAR}.csv", index=False)

    top = summary.head(30).iloc[::-1]
    plt.figure(figsize=(10, 8))
    plt.barh(top.index.astype(str), top["congested_hours"], color="#3366cc")
    plt.xlabel("Congested hours in year")
    plt.ylabel("Transmission line")
    plt.title("Congestion occurrence per line (top 30)")
    plt.tight_layout()
    plt.savefig(out_dir / f"figure_congestion_occurrence_per_line_{SIM_YEAR}.png", dpi=150)
    plt.close()

    plt.figure(figsize=(11, 4))
    for line, group in pd.DataFrame(rows).groupby("line"):
        s = group.sort_values("timestamp").head(336)
        plt.plot(pd.to_datetime(s["timestamp"]), s["loading_fraction"], label=line, linewidth=1)
    plt.axhline(CONGESTION_THRESHOLD, linestyle="--", color="red", label=f"threshold {CONGESTION_THRESHOLD}")
    plt.ylabel("Loading fraction")
    plt.title("Kupferzell-area line loading (first 2 weeks)")
    if rows:
        plt.legend(fontsize=7, loc="upper right")
    plt.tight_layout()
    plt.savefig(out_dir / f"figure_kupferzell_line_loading_{SIM_YEAR}.png", dpi=150)
    plt.close()


def run_step11_12_postprocess(out_dir: Path) -> None:
    n = pypsa.Network(solved_network_path(out_dir))
    loading = pd.read_csv(line_loading_path(out_dir), index_col=0, parse_dates=True)

    validation = validate_model(n, loading, out_dir)
    print(validation)
    export_congestion(n, loading, out_dir)


def main() -> None:
    args = parse_args()
    out = args.output_dir

    if args.mode in {"solve", "all"}:
        run_step10_solve(args.input_network, out)

    if args.mode in {"postprocess", "all"}:
        run_step11_12_postprocess(out)


if __name__ == "__main__":
    main()
