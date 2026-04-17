"""Congestion occurrence post-processing for solved PyPSA networks."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

from plotting import plot_kupferzell_loading, plot_monthly_congestion, plot_top_congested_lines

SIM_YEAR = 2025
CONGESTION_THRESHOLD = 0.98
CSV_FLOAT_FORMAT = "%.3f"
KUPFERZELL_LAT = 49.2333
KUPFERZELL_LON = 9.6833
KUPFERZELL_RADIUS_DEG = 0.8
PROJECT_DIR = Path(__file__).resolve().parent
PYPSA_EUR_DIR = PROJECT_DIR.parent / "pypsa-eur"
DEFAULT_SOLVED_NETWORK = PYPSA_EUR_DIR / "results" / "kupferzell_2024_simple" / "networks" / "base_s_256_elec_.nc"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "results"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export congestion occurrence diagnostics")
    p.add_argument("--network", type=Path, default=DEFAULT_SOLVED_NETWORK, help="Solved PyPSA network netcdf")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Output root directory. Results are written to <output-dir>/<scenario>/congestion_occurrence",
    )
    p.add_argument("--threshold", type=float, default=CONGESTION_THRESHOLD, help="Congestion threshold in pu")
    p.add_argument(
        "--line",
        action="append",
        default=None,
        help="Specific line id to analyze (repeatable).",
    )
    p.add_argument(
        "--lines",
        type=str,
        default="",
        help="Comma-separated list of specific line ids to analyze.",
    )
    return p.parse_args()


def infer_validation_scenario(network: Path) -> str:
    text = str(network).lower()
    parts = [p.lower() for p in network.parts]

    for part in parts:
        if "kupferzell" in part and "simple" in part:
            return "kupferzell_simple"
        if "kupferzell" in part and "full" in part:
            return "kupferzell_full"

    if "kupferzell" in text and "simple" in text:
        return "kupferzell_simple"
    if "kupferzell" in text and "full" in text:
        return "kupferzell_full"
    return "default"


def resolve_congestion_output_dir(network: Path, output_root: Path) -> tuple[Path, str]:
    scenario = infer_validation_scenario(network)
    return output_root / scenario / "congestion_occurrence", scenario


def normalize_requested_lines(line_args: list[str] | None, lines_csv: str) -> list[str]:
    values: list[str] = []
    if line_args:
        values.extend([v.strip() for v in line_args if v and v.strip()])
    if lines_csv:
        values.extend([v.strip() for v in lines_csv.split(",") if v.strip()])
    # Keep stable ordering while removing duplicates.
    return list(dict.fromkeys(values))


def select_target_lines(n: pypsa.Network, requested_lines: list[str]) -> tuple[pd.Index, str]:
    if requested_lines:
        missing = [line for line in requested_lines if line not in n.lines.index]
        if missing:
            raise ValueError(f"Requested line(s) not found in network: {', '.join(missing)}")
        return pd.Index(requested_lines), "custom"

    kup_lines = find_kupferzell_lines(n)
    if len(kup_lines) > 0:
        return kup_lines, "kupferzell"

    return n.lines.index, "all_lines_fallback"


def find_kupferzell_lines(n: pypsa.Network) -> pd.Index:
    """Return line ids with at least one endpoint near Kupferzell."""
    buses = n.buses.copy()
    buses["dist_deg"] = np.sqrt((buses["y"] - KUPFERZELL_LAT) ** 2 + (buses["x"] - KUPFERZELL_LON) ** 2)
    near = buses[buses["dist_deg"] <= KUPFERZELL_RADIUS_DEG].index
    lines = n.lines[n.lines.bus0.isin(near) | n.lines.bus1.isin(near)]
    return lines.index


def compute_line_loading(n: pypsa.Network) -> pd.DataFrame:
    """Compute hourly loading fraction for each AC line."""
    return n.lines_t.p0.abs().div(n.lines.s_nom, axis=1)


def _country_pair(bus0: str, bus1: str) -> str:
    c0, c1 = bus0[:2], bus1[:2]
    return "-".join(sorted([c0, c1]))


def summarize_line_congestion(n: pypsa.Network, loading: pd.DataFrame, threshold: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create line-level congestion summary and hourly binary flags."""
    flags = (loading >= threshold).astype(int)

    summary = pd.DataFrame(index=loading.columns)
    summary["bus0"] = n.lines.reindex(summary.index)["bus0"]
    summary["bus1"] = n.lines.reindex(summary.index)["bus1"]
    summary["country_pair"] = [
        _country_pair(summary.at[idx, "bus0"], summary.at[idx, "bus1"]) for idx in summary.index
    ]
    summary["s_nom_mw"] = n.lines.reindex(summary.index)["s_nom"]
    summary["mean_loading"] = loading.mean()
    summary["p95_loading"] = loading.quantile(0.95)
    summary["max_loading"] = loading.max()
    summary["congested_hours"] = flags.sum()
    summary["congested_share_pct"] = 100.0 * summary["congested_hours"] / len(loading)
    summary = summary.sort_values("congested_hours", ascending=False)

    return summary, flags


def summarize_monthly_congestion(flags: pd.DataFrame) -> pd.DataFrame:
    """Aggregate congested line-hours by month."""
    monthly = flags.sum(axis=1).to_frame("congested_line_hours")
    out = monthly.groupby(monthly.index.to_period("M")).sum()
    out.index = out.index.astype(str)
    out = out.rename(columns={"congested_line_hours": "congested_hours"})
    return out


def summarize_country_pair_congestion(summary: pd.DataFrame) -> pd.DataFrame:
    """Aggregate congestion to country-pair interfaces."""
    out = (
        summary.groupby("country_pair", as_index=True)[["congested_hours", "congested_share_pct"]]
        .agg({"congested_hours": "sum", "congested_share_pct": "mean"})
        .sort_values("congested_hours", ascending=False)
    )
    return out


def export_kupferzell_proximity(n: pypsa.Network, loading: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """Create long-format hourly loading table for lines close to Kupferzell."""
    kup_lines = find_kupferzell_lines(n)
    rows: list[dict[str, object]] = []
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
                    "at_or_above_capacity_threshold": int(value >= threshold),
                }
            )
    return pd.DataFrame(rows)


def run_congestion_postprocess(
    network: Path = DEFAULT_SOLVED_NETWORK,
    output_dir: Path = DEFAULT_OUTPUT_ROOT,
    threshold: float = CONGESTION_THRESHOLD,
    requested_lines: list[str] | None = None,
) -> None:
    if not network.exists():
        raise FileNotFoundError(f"Solved network not found: {network}")

    resolved_output_dir, scenario = resolve_congestion_output_dir(network, output_dir)
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    n = pypsa.Network(network)
    target_lines, line_scope = select_target_lines(n, requested_lines or [])
    loading = compute_line_loading(n)[target_lines]

    summary, flags = summarize_line_congestion(n, loading, threshold)
    monthly = summarize_monthly_congestion(flags)
    by_interface = summarize_country_pair_congestion(summary)
    kupferzell_df = export_kupferzell_proximity(n, loading, threshold) if line_scope == "kupferzell" else pd.DataFrame()

    prefix = f"congestion_{line_scope}_{SIM_YEAR}"
    loading.to_csv(
        resolved_output_dir / f"{prefix}_line_loading_hourly.csv",
        float_format=CSV_FLOAT_FORMAT,
    )
    flags.to_csv(
        resolved_output_dir / f"{prefix}_hourly_flags.csv",
        float_format=CSV_FLOAT_FORMAT,
    )
    summary.to_csv(
        resolved_output_dir / f"{prefix}_by_line.csv",
        float_format=CSV_FLOAT_FORMAT,
    )
    monthly.to_csv(
        resolved_output_dir / f"{prefix}_monthly.csv",
        float_format=CSV_FLOAT_FORMAT,
    )
    by_interface.to_csv(
        resolved_output_dir / f"{prefix}_by_country_pair.csv",
        float_format=CSV_FLOAT_FORMAT,
    )
    if not kupferzell_df.empty:
        kupferzell_df.to_csv(
            resolved_output_dir / f"kupferzell_line_proximity_hourly_{SIM_YEAR}.csv",
            index=False,
            float_format=CSV_FLOAT_FORMAT,
        )

    plot_top_congested_lines(
        summary,
        str(resolved_output_dir / f"figure_{prefix}_occurrence_per_line.png"),
    )
    if not kupferzell_df.empty:
        plot_kupferzell_loading(
            kupferzell_df,
            str(resolved_output_dir / f"figure_kupferzell_line_loading_{SIM_YEAR}.png"),
            threshold,
        )
    plot_monthly_congestion(
        monthly,
        str(resolved_output_dir / f"figure_{prefix}_monthly.png"),
    )

    print(f"Scenario: {scenario}")
    print(f"Line scope: {line_scope}")
    print(f"Analyzed lines: {len(target_lines)}")
    print(f"Saved congestion outputs in: {resolved_output_dir}")


def main() -> None:
    args = parse_args()
    requested_lines = normalize_requested_lines(args.line, args.lines)
    run_congestion_postprocess(args.network, args.output_dir, args.threshold, requested_lines)


if __name__ == "__main__":
    main()

