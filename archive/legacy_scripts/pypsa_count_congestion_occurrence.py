"""Steps 11-12: model pypsa-validation + congestion occurrence post-processing."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pypsa
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from pypsa_config import (
    SOLVED_NETWORK_PATH,
    LINE_LOADING_PATH,
    CONGESTION_THRESHOLD,
    KUPFERZELL_LAT,
    KUPFERZELL_LON,
    KUPFERZELL_RADIUS_DEG,
    VALIDATION_SUMMARY_PATH,
    CONGESTION_BY_LINE_PATH,
    CONGESTION_HOURLY_PATH,
    KUPFERZELL_LINE_HOURLY_PATH,
    CONGESTION_FIGURE_PATH,
    KUPFERZELL_FIGURE_PATH,
)


def validate_model(n: pypsa.Network, loading: pd.DataFrame) -> pd.DataFrame:
    checks: list[tuple[str, str, str]] = []

    checks.append(("snapshots_present", "pass" if len(n.snapshots) > 0 else "fail", f"{len(n.snapshots)} snapshots"))
    checks.append(("line_flows_present", "pass" if not n.lines_t.p0.empty else "fail", f"shape={n.lines_t.p0.shape}"))

    if n.generators_t.p.empty:
        checks.append(("generator_dispatch_present", "warning", "No generator dispatch time series in solved network"))
    else:
        checks.append(("generator_dispatch_present", "pass", f"shape={n.generators_t.p.shape}"))

    if n.loads_t.p_set.empty:
        checks.append(("load_timeseries_present", "fail", "No load timeseries"))
    else:
        load = n.loads_t.p_set.sum(axis=1)
        checks.append(("load_positive", "pass" if (load >= 0).all() else "fail", f"min={load.min():.2f} MW"))

    line_limits = n.lines.s_nom.reindex(loading.columns)
    violations = (loading > 1.001).sum().sum()
    checks.append(("line_limit_violations", "pass" if violations == 0 else "warning", f"violations={int(violations)} cells"))

    if np.isfinite(getattr(n, "objective", np.nan)):
        checks.append(("objective_finite", "pass", f"objective={float(n.objective):.2f}"))
    else:
        checks.append(("objective_finite", "warning", "objective not stored in netcdf"))

    df = pd.DataFrame(checks, columns=["check", "status", "detail"])
    df.to_csv(VALIDATION_SUMMARY_PATH, index=False)
    return df


def congestion_tables(n: pypsa.Network, loading: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    flags = (loading >= CONGESTION_THRESHOLD).astype(int)
    flags.to_csv(CONGESTION_HOURLY_PATH)

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
    summary.to_csv(CONGESTION_BY_LINE_PATH)
    return summary, flags


def find_kupferzell_lines(n: pypsa.Network) -> pd.Index:
    buses = n.buses.copy()
    buses["dist_deg"] = np.sqrt((buses["y"] - KUPFERZELL_LAT) ** 2 + (buses["x"] - KUPFERZELL_LON) ** 2)
    kupferzell_buses = buses[buses["dist_deg"] <= KUPFERZELL_RADIUS_DEG].index
    lines = n.lines[n.lines.bus0.isin(kupferzell_buses) | n.lines.bus1.isin(kupferzell_buses)]
    return lines.index


def export_kupferzell_hourly(n: pypsa.Network, loading: pd.DataFrame) -> pd.DataFrame:
    kup_lines = find_kupferzell_lines(n)
    rows = []
    for line in kup_lines:
        s_nom = float(n.lines.at[line, "s_nom"])
        for ts, value in loading[line].items():
            rows.append(
                {
                    "timestamp": ts,
                    "line": line,
                    "bus0": n.lines.at[line, "bus0"],
                    "bus1": n.lines.at[line, "bus1"],
                    "s_nom_mw": s_nom,
                    "loading_fraction": float(value),
                    "distance_to_capacity_fraction": float(1.0 - value),
                    "at_or_above_capacity_threshold": int(value >= CONGESTION_THRESHOLD),
                }
            )

    out = pd.DataFrame(rows)
    out.to_csv(KUPFERZELL_LINE_HOURLY_PATH, index=False)
    return out


def make_figures(summary: pd.DataFrame, kupferzell_hourly: pd.DataFrame) -> None:
    top = summary.head(30).iloc[::-1]
    plt.figure(figsize=(10, 8))
    plt.barh(top.index.astype(str), top["congested_hours"], color="#3366cc")
    plt.xlabel("Congested hours in year")
    plt.ylabel("Transmission line")
    plt.title("Congestion occurrence per line (top 30)")
    plt.tight_layout()
    plt.savefig(CONGESTION_FIGURE_PATH, dpi=150)
    plt.close()

    plt.figure(figsize=(11, 4))
    if not kupferzell_hourly.empty:
        for line, group in kupferzell_hourly.groupby("line"):
            s = group.sort_values("timestamp").head(336)  # first 2 weeks for readability
            plt.plot(pd.to_datetime(s["timestamp"]), s["loading_fraction"], label=line, linewidth=1)
    plt.axhline(CONGESTION_THRESHOLD, linestyle="--", color="red", label=f"threshold {CONGESTION_THRESHOLD}")
    plt.ylabel("Loading fraction")
    plt.title("Kupferzell-area line loading (first 2 weeks)")
    plt.legend(fontsize=7, loc="upper right")
    plt.tight_layout()
    plt.savefig(KUPFERZELL_FIGURE_PATH, dpi=150)
    plt.close()


def main() -> None:
    n = pypsa.Network(SOLVED_NETWORK_PATH)
    loading = pd.read_csv(LINE_LOADING_PATH, index_col=0, parse_dates=True)

    validation = validate_model(n, loading)
    print("Validation summary")
    print(validation)

    summary, _flags = congestion_tables(n, loading)
    kupferzell_hourly = export_kupferzell_hourly(n, loading)
    make_figures(summary, kupferzell_hourly)

    print(f"Saved pypsa-validation: {VALIDATION_SUMMARY_PATH}")
    print(f"Saved congestion summary: {CONGESTION_BY_LINE_PATH}")
    print(f"Saved congestion flags: {CONGESTION_HOURLY_PATH}")
    print(f"Saved Kupferzell hourly csv: {KUPFERZELL_LINE_HOURLY_PATH}")
    print(f"Saved figure: {CONGESTION_FIGURE_PATH}")
    print(f"Saved figure: {KUPFERZELL_FIGURE_PATH}")


if __name__ == "__main__":
    main()
