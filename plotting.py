"""Plotting helpers for PyPSA post-processing outputs."""

from __future__ import annotations

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.colors import Normalize


def plot_top_congested_lines(summary: pd.DataFrame, output_path: str, top_n: int = 30) -> None:
    """Plot horizontal bar chart for lines with most congested hours."""
    top = summary.sort_values("congested_hours", ascending=False).head(top_n).iloc[::-1]
    plt.figure(figsize=(10, 8))
    plt.barh(top.index.astype(str), top["congested_hours"], color="#3366cc")
    plt.xlabel("Congested hours")
    plt.ylabel("Transmission line")
    plt.title(f"Congestion occurrence per line (top {top_n})")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


def plot_kupferzell_loading(kupferzell_df: pd.DataFrame, output_path: str, threshold: float) -> None:
    """Plot first two weeks of loading for Kupferzell-area lines."""
    plt.figure(figsize=(11, 4))
    for line, group in kupferzell_df.groupby("line"):
        s = group.sort_values("timestamp").head(336)
        plt.plot(pd.to_datetime(s["timestamp"]), s["loading_fraction"], label=line, linewidth=1)

    plt.axhline(threshold, linestyle="--", color="red", label=f"threshold {threshold}")
    plt.ylabel("Loading fraction")
    plt.title("Kupferzell-area line loading (first 2 weeks)")
    if not kupferzell_df.empty:
        plt.legend(fontsize=7, loc="upper right")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


def plot_monthly_congestion(monthly_df: pd.DataFrame, output_path: str) -> None:
    """Plot monthly congested-hour totals for the full network."""
    plt.figure(figsize=(10, 4))
    plt.plot(monthly_df.index.astype(str), monthly_df["congested_hours"], marker="o", color="#1f77b4")
    plt.xticks(rotation=45, ha="right")
    plt.ylabel("Congested line-hours")
    plt.title("Monthly congestion intensity")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


def _plot_line_metric_map(
    values: pd.Series,
    buses: pd.DataFrame,
    lines: pd.DataFrame,
    output_path: str,
    title: str,
    colorbar_label: str,
    minimum_voltage: float = 0.0,
    cmap_name: str = "YlOrRd",
) -> None:
    """Plot a line-level metric on a simple network map with a fixed layout/style."""
    if values.empty or buses.empty or lines.empty:
        plt.figure(figsize=(7, 5))
        plt.text(0.5, 0.5, "No line data available", ha="center", va="center")
        plt.axis("off")
        plt.tight_layout()
        plt.savefig(output_path, dpi=150)
        plt.close()
        return

    eligible = lines.reindex(values.index).copy()
    if minimum_voltage > 0 and "v_nom" in eligible.columns:
        v_nom = pd.to_numeric(eligible["v_nom"], errors="coerce")
        eligible = eligible.loc[v_nom >= minimum_voltage]

    if eligible.empty:
        plt.figure(figsize=(7, 5))
        plt.text(0.5, 0.5, "No high-voltage lines to plot", ha="center", va="center")
        plt.axis("off")
        plt.tight_layout()
        plt.savefig(output_path, dpi=150)
        plt.close()
        return

    coords = buses[["x", "y"]].copy()
    eligible["x0"] = eligible["bus0"].map(coords["x"])
    eligible["y0"] = eligible["bus0"].map(coords["y"])
    eligible["x1"] = eligible["bus1"].map(coords["x"])
    eligible["y1"] = eligible["bus1"].map(coords["y"])
    eligible = eligible.dropna(subset=["x0", "y0", "x1", "y1"])
    if eligible.empty:
        plt.figure(figsize=(7, 5))
        plt.text(0.5, 0.5, "No line coordinates available", ha="center", va="center")
        plt.axis("off")
        plt.tight_layout()
        plt.savefig(output_path, dpi=150)
        plt.close()
        return

    metric = values.reindex(eligible.index).fillna(0.0).astype(float)
    segments = [
        [(row.x0, row.y0), (row.x1, row.y1)]
        for row in eligible.itertuples(index=False)
    ]

    fig, ax = plt.subplots(figsize=(8, 6))
    base = LineCollection(segments, colors="black", linewidths=0.6, alpha=0.5, zorder=1)
    ax.add_collection(base)

    positive = metric > 0
    if positive.any():
        pos_segments = [seg for seg, is_pos in zip(segments, positive) if is_pos]
        pos_values = metric[positive]
        norm = Normalize(vmin=pos_values.min(), vmax=pos_values.max())
        cmap = plt.get_cmap(cmap_name)
        lc = LineCollection(pos_segments, cmap=cmap, norm=norm, linewidths=1.2, zorder=2)
        lc.set_array(pos_values)
        ax.add_collection(lc)
        cbar = fig.colorbar(lc, ax=ax, shrink=0.85)
        cbar.set_label(colorbar_label)

    ax.set_title(title)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal", adjustable="box")
    fig.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close(fig)


def plot_congestion_severity_map(
    summary: pd.DataFrame,
    buses: pd.DataFrame,
    lines: pd.DataFrame,
    output_path: str,
    minimum_voltage: float = 0.0,
) -> None:
    """Plot line congestion severity (hours) on a simple network map."""
    if summary.empty or "congested_hours" not in summary.columns:
        values = pd.Series(dtype=float)
    else:
        values = summary["congested_hours"]

    _plot_line_metric_map(
        values=values,
        buses=buses,
        lines=lines,
        output_path=output_path,
        title="High-voltage line congestion severity",
        colorbar_label="Congested hours",
        minimum_voltage=minimum_voltage,
        cmap_name="YlOrRd",
    )


def plot_congestion_alleviation_map(
    line_saved_cost_eur: pd.Series,
    buses: pd.DataFrame,
    lines: pd.DataFrame,
    output_path: str,
    minimum_voltage: float = 0.0,
) -> None:
    """Plot saved congestion cost per line with the same layout/style as severity maps."""
    _plot_line_metric_map(
        values=line_saved_cost_eur,
        buses=buses,
        lines=lines,
        output_path=output_path,
        title="High-voltage line congestion cost alleviation",
        colorbar_label="Saved congestion cost [EUR]",
        minimum_voltage=minimum_voltage,
        cmap_name="YlOrRd",
    )

