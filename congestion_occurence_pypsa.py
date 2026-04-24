"""Congestion occurrence post-processing for solved PyPSA networks."""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

from plotting import plot_kupferzell_loading, plot_monthly_congestion, plot_top_congested_lines
from plotting import plot_congestion_severity_map, plot_average_line_loading_map_from_network

SIM_YEAR = 2025
CONGESTION_THRESHOLD = 0.98
DEFAULT_MINIMUM_VOLTAGE = 0.0
DEFAULT_METHOD = "loading"
DEFAULT_TARGET_AREA = "kupferzell"
CSV_FLOAT_FORMAT = "%.3f"
KUPFERZELL_LAT = 49.2333
KUPFERZELL_LON = 9.6833
KUPFERZELL_RADIUS_DEG = 0.8
BBOX_CORRIDOR = (7.5, 10.6, 47.5, 51.0)  # lon_min, lon_max, lat_min, lat_max
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
        "--method",
        type=str,
        default=DEFAULT_METHOD,
        choices=["loading", "n_minus_1", "redispatch_trigger"],
        help="Congestion detection method (compatibility: non-loading modes currently fall back to loading).",
    )
    p.add_argument(
        "--target-area",
        type=str,
        default=DEFAULT_TARGET_AREA,
        choices=["kupferzell", "corridor", "all"],
        help="Line selection scope for reporting.",
    )
    p.add_argument(
        "--minimum-voltage",
        type=float,
        default=DEFAULT_MINIMUM_VOLTAGE,
        help=(
            "Minimum line voltage in kV to include in the analysis. "
            "Set to 0 to disable voltage filtering."
        ),
    )
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


def filter_lines_by_minimum_voltage(
    n: pypsa.Network,
    line_ids: pd.Index,
    minimum_voltage: float,
) -> tuple[pd.Index, int]:
    """Filter candidate lines by v_nom when a positive minimum voltage is set."""
    if minimum_voltage <= 0:
        return line_ids, 0

    if "v_nom" not in n.lines.columns:
        raise ValueError("Network lines do not expose a 'v_nom' column; cannot apply minimum_voltage.")

    v_nom = pd.to_numeric(n.lines.loc[line_ids, "v_nom"], errors="coerce")
    eligible = v_nom.index[v_nom >= minimum_voltage]
    excluded = int(len(line_ids) - len(eligible))

    if len(eligible) == 0:
        raise ValueError(
            f"No lines remain after applying minimum_voltage={minimum_voltage:.1f} kV."
        )

    if excluded > 0:
        warnings.warn(
            f"Excluded {excluded} line(s) below minimum_voltage={minimum_voltage:.1f} kV (or with invalid v_nom).",
            UserWarning,
            stacklevel=2,
        )

    return pd.Index(eligible), excluded


def select_target_lines(
    n: pypsa.Network,
    requested_lines: list[str],
    minimum_voltage: float,
) -> tuple[pd.Index, str, int]:
    if requested_lines:
        missing = [line for line in requested_lines if line not in n.lines.index]
        if missing:
            raise ValueError(f"Requested line(s) not found in network: {', '.join(missing)}")
        candidate_lines = pd.Index(requested_lines)
        line_scope = "custom"
    else:
        candidate_lines = find_kupferzell_lines(n)
        if len(candidate_lines) > 0:
            line_scope = "kupferzell"
        else:
            candidate_lines = n.lines.index
            line_scope = "all_lines_fallback"

    target_lines, excluded = filter_lines_by_minimum_voltage(n, candidate_lines, minimum_voltage)
    return target_lines, line_scope, excluded


def find_kupferzell_lines(n: pypsa.Network) -> pd.Index:
    """Return line ids with at least one endpoint near Kupferzell."""
    buses = n.buses.copy()
    buses["dist_deg"] = np.sqrt((buses["y"] - KUPFERZELL_LAT) ** 2 + (buses["x"] - KUPFERZELL_LON) ** 2)
    near = buses[buses["dist_deg"] <= KUPFERZELL_RADIUS_DEG].index
    lines = n.lines[n.lines.bus0.isin(near) | n.lines.bus1.isin(near)]
    return lines.index


def find_corridor_lines(n: pypsa.Network) -> pd.Index:
    """Return line ids with at least one endpoint in the corridor bounding box."""
    buses = n.buses.copy()
    x = pd.to_numeric(buses.get("x"), errors="coerce")
    y = pd.to_numeric(buses.get("y"), errors="coerce")
    lon_min, lon_max, lat_min, lat_max = BBOX_CORRIDOR
    in_bbox = buses.index[(x >= lon_min) & (x <= lon_max) & (y >= lat_min) & (y <= lat_max)]
    lines = n.lines[n.lines.bus0.isin(in_bbox) | n.lines.bus1.isin(in_bbox)]
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


def export_kupferzell_proximity(
    n: pypsa.Network,
    loading: pd.DataFrame,
    threshold: float,
    line_ids: pd.Index | None = None,
) -> pd.DataFrame:
    """Create long-format hourly loading table for lines close to Kupferzell."""
    kup_lines = line_ids if line_ids is not None else find_kupferzell_lines(n)
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
    minimum_voltage: float = DEFAULT_MINIMUM_VOLTAGE,
    requested_lines: list[str] | None = None,
    method: str = DEFAULT_METHOD,
    target_area: str = DEFAULT_TARGET_AREA,
) -> None:
    if not network.exists():
        raise FileNotFoundError(f"Solved network not found: {network}")

    resolved_output_dir, scenario = resolve_congestion_output_dir(network, output_dir)
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    n = pypsa.Network(network)
    # Compatibility: accept enhanced method flags from job scripts while keeping loading-based logic.
    if method != "loading":
        warnings.warn(
            f"Method '{method}' is accepted for CLI compatibility and currently uses loading-based detection.",
            UserWarning,
            stacklevel=2,
        )

    if requested_lines:
        target_lines, line_scope, excluded_by_voltage = select_target_lines(n, requested_lines or [], minimum_voltage)
    elif target_area == "corridor":
        corridor = find_corridor_lines(n)
        if len(corridor) == 0:
            corridor = n.lines.index
            line_scope = "all_lines_fallback"
        else:
            line_scope = "corridor"
        target_lines, excluded_by_voltage = filter_lines_by_minimum_voltage(n, pd.Index(corridor), minimum_voltage)
    elif target_area == "all":
        line_scope = "all_lines"
        target_lines, excluded_by_voltage = filter_lines_by_minimum_voltage(n, n.lines.index, minimum_voltage)
    else:
        target_lines, line_scope, excluded_by_voltage = select_target_lines(n, requested_lines or [], minimum_voltage)

    loading = compute_line_loading(n)[target_lines]

    summary, flags = summarize_line_congestion(n, loading, threshold)
    monthly = summarize_monthly_congestion(flags)
    by_interface = summarize_country_pair_congestion(summary)
    kupferzell_df = export_kupferzell_proximity(n, loading, threshold, line_ids=target_lines)

    prefix = f"congestion_{line_scope}_{method}_{SIM_YEAR}"
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
    if line_scope in {"kupferzell", "corridor"} and not kupferzell_df.empty:
        plot_kupferzell_loading(
            kupferzell_df,
            str(resolved_output_dir / f"figure_kupferzell_line_loading_{SIM_YEAR}.png"),
            threshold,
        )
    plot_monthly_congestion(
        monthly,
        str(resolved_output_dir / f"figure_{prefix}_monthly.png"),
    )
    plot_congestion_severity_map(
        summary=summary,
        buses=n.buses,
        lines=n.lines,
        output_path=str(resolved_output_dir / f"figure_{prefix}_congestion_severity_map.png"),
        minimum_voltage=minimum_voltage,
    )

    plot_average_line_loading_map_from_network(
        n,
        str(resolved_output_dir / f"figure_{prefix}_average_line_loading_map.png"),
        minimum_voltage=minimum_voltage,
    )

    print(f"Scenario: {scenario}")
    print(f"Method: {method}")
    print(f"Target area: {target_area} (resolved: {line_scope})")
    print(f"Line scope: {line_scope}")
    print(f"Minimum voltage: {minimum_voltage:.1f} kV")
    print(f"Excluded by voltage filter: {excluded_by_voltage}")
    print(f"Analyzed lines: {len(target_lines)}")
    print(f"Saved congestion outputs in: {resolved_output_dir}")


def main() -> None:
    args = parse_args()
    requested_lines = normalize_requested_lines(args.line, args.lines)
    run_congestion_postprocess(
        args.network,
        args.output_dir,
        args.threshold,
        args.minimum_voltage,
        requested_lines,
        args.method,
        args.target_area,
    )


if __name__ == "__main__":
    main()



