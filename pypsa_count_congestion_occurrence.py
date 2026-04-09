"""
Step 4: Kupferzell Corridor Congestion Analysis
================================================
Identifies Kupferzell-area transmission lines, counts congestion hours,
analyses seasonal and diurnal patterns, and exports all tables used
in the paper.

Congestion definition:
    Line l is congested in hour t  ⟺  |p0_{l,t}| / s_nom_l ≥ θ
    where θ = 0.98 (2% tolerance for LOPF numerical precision).

Under N-1 security, a line must be operated such that an outage of any
single element does not cause a remaining line to exceed s_nom. In PyPSA
LOPF, line thermal limits already encode this (s_nom reflects N-1 secure
capacity), so the threshold θ < 1 is the appropriate congestion flag.

Outputs (all in outputs/):
  congestion_summary.csv        — per-line annual congestion statistics
  congestion_hourly.csv         — binary congestion indicator (hours × lines)
  congestion_seasonal.csv       — monthly congestion frequencies
  congestion_diurnal.csv        — hour-of-day congestion frequencies
  figure_corridor_map.png       — geographic map of corridor lines
  figure_loading_dist.png       — distribution of line loading fractions
  figure_seasonal.png           — seasonal congestion heatmap

Run: python pypsa_count_congestion_occurrence.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pypsa
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import warnings

from pypsa_config import (
    OUTPUT_DIR, NETWORK_DIR, KUPFERZELL_LAT, KUPFERZELL_LON,
    CORRIDOR_RADIUS_DEG, CONGESTION_THRESHOLD, SIM_YEAR
)

RESULTS_FILE = os.path.join(OUTPUT_DIR, "lopf_results.nc")
LOADING_FILE = os.path.join(OUTPUT_DIR, "line_loading_hourly.csv")


def load_results() -> tuple[pypsa.Network, pd.DataFrame]:
    """Load LOPF results and line loading data."""
    print(f"  Loading LOPF network from {RESULTS_FILE} ...")
    n = pypsa.Network(RESULTS_FILE)

    print(f"  Loading line loading from {LOADING_FILE} ...")
    loading = pd.read_csv(LOADING_FILE, index_col=0, parse_dates=True)

    print(f"  ✓ {len(n.snapshots)} snapshots | {len(n.lines)} lines")
    return n, loading


def identify_corridor_lines(n: pypsa.Network,
                            radius_deg: float = CORRIDOR_RADIUS_DEG
                            ) -> pd.Index:
    """
    Return index of lines with at least one endpoint within radius_deg
    degrees of the Kupferzell substation.

    The 128-bus PyPSA-EUR network may not have a dedicated Kupferzell bus;
    the nearest bus in BadenWürttemberg northeast typically represents
    the Heilbronn/Hohenlohe cluster. We use a 0.8° radius (~80 km)
    to capture the full Kupferzell–Großgartach–TenneT corridor.
    """
    buses = n.buses.copy()
    buses["dist"] = np.sqrt(
        (buses["y"] - KUPFERZELL_LAT) ** 2 +
        (buses["x"] - KUPFERZELL_LON) ** 2
    )
    corridor_buses = buses[buses["dist"] <= radius_deg].index

    # Lines with either endpoint in corridor
    lines = n.lines
    corridor_lines = lines[
        lines["bus0"].isin(corridor_buses) |
        lines["bus1"].isin(corridor_buses)
    ].index

    print(f"  Corridor buses ({radius_deg}° radius): {list(corridor_buses)}")
    print(f"  Corridor lines: {len(corridor_lines)}")
    for l in corridor_lines:
        row = n.lines.loc[l]
        print(f"    {l:>40s}  "
              f"{row['bus0']} → {row['bus1']}  "
              f"s_nom={row['s_nom']:.0f} MW  "
              f"length={row.get('length', np.nan):.0f} km")

    return corridor_lines


def compute_congestion_stats(loading: pd.DataFrame,
                             corridor_lines: pd.Index,
                             threshold: float = CONGESTION_THRESHOLD
                             ) -> pd.DataFrame:
    """
    Compute per-line congestion statistics for corridor lines.

    Returns DataFrame with columns:
        s_nom_mw         : thermal rating (MW)
        n_congested_h    : hours with loading >= threshold
        pct_congested    : fraction of year congested
        mean_loading     : mean hourly loading fraction
        p95_loading      : 95th percentile loading fraction
        max_loading      : maximum hourly loading fraction
        n_congested_n1   : (placeholder) N-1 contingency hours
    """
    # Restrict to corridor lines present in loading df
    avail = [l for l in corridor_lines if l in loading.columns]
    if not avail:
        raise ValueError(
            "No corridor lines found in loading DataFrame. "
            "Check that LOPF ran successfully and corridor identification "
            "uses the same network."
        )
    corridor_loading = loading[avail]

    stats = pd.DataFrame(index=avail)
    stats["n_congested_h"]  = (corridor_loading >= threshold).sum()
    stats["pct_congested"]  = stats["n_congested_h"] / len(loading) * 100
    stats["mean_loading"]   = corridor_loading.mean()
    stats["p95_loading"]    = corridor_loading.quantile(0.95)
    stats["max_loading"]    = corridor_loading.max()

    # Total congestion-hours (across all corridor lines, double-counting possible)
    total_ch = stats["n_congested_h"].sum()
    print(f"\n  ── Congestion Statistics (θ = {threshold}) ─────────────────")
    print(f"  {'Line':<45} {'s_nom':>8} {'CongH':>7} {'Pct%':>6} "
          f"{'Mean':>7} {'P95':>7} {'Max':>7}")
    print("  " + "-" * 90)
    for line, row in stats.iterrows():
        print(f"  {line:<45} {row.get('s_nom_mw', np.nan):>8.0f} "
              f"{row['n_congested_h']:>7.0f} {row['pct_congested']:>6.1f}% "
              f"{row['mean_loading']:>7.3f} {row['p95_loading']:>7.3f} "
              f"{row['max_loading']:>7.3f}")
    print(f"\n  Total corridor congestion-hours: {total_ch:.0f} h/year")
    print(f"  (Sum across all {len(avail)} corridor lines; "
          f"each line counted independently)")

    return stats


def seasonal_congestion_matrix(loading: pd.DataFrame,
                               corridor_lines: pd.Index,
                               threshold: float = CONGESTION_THRESHOLD
                               ) -> pd.DataFrame:
    """
    Compute monthly × hour-of-day congestion frequency matrix
    for the primary corridor line (highest congestion frequency).
    """
    avail = [l for l in corridor_lines if l in loading.columns]
    if not avail:
        return pd.DataFrame()

    # Use the most-congested line as representative
    n_cong = (loading[avail] >= threshold).sum()
    primary_line = n_cong.idxmax()
    print(f"\n  Primary corridor line (most congested): {primary_line}")

    s = loading[primary_line].copy()
    s.index = pd.to_datetime(s.index, utc=True)

    df = pd.DataFrame({
        "loading": s,
        "month": s.index.month,
        "hour": s.index.hour,
        "congested": (s >= threshold).astype(int)
    })

    matrix = df.pivot_table(
        values="congested",
        index="month",
        columns="hour",
        aggfunc="mean"
    ) * 100   # as percentage

    matrix.index = pd.to_datetime(
        [f"2024-{m:02d}-01" for m in matrix.index]
    ).strftime("%b")

    return matrix, primary_line


def aggregate_congestion_by_period(loading: pd.DataFrame,
                                    corridor_lines: pd.Index,
                                    threshold: float = CONGESTION_THRESHOLD
                                    ) -> pd.DataFrame:
    """
    Monthly aggregate congestion hours for all corridor lines.
    Returns DataFrame: index = months, columns = lines.
    """
    avail = [l for l in corridor_lines if l in loading.columns]
    congested = (loading[avail] >= threshold).astype(int)
    congested.index = pd.to_datetime(congested.index, utc=True)
    monthly = congested.resample("ME").sum()
    monthly.index = monthly.index.strftime("%Y-%m")
    return monthly


def plot_loading_distribution(loading: pd.DataFrame,
                               corridor_lines: pd.Index) -> None:
    """Plot distribution of hourly loading fractions for corridor lines."""
    avail = [l for l in corridor_lines if l in loading.columns]
    if not avail:
        return

    fig, axes = plt.subplots(
        len(avail), 1,
        figsize=(8, 2.5 * len(avail)),
        sharex=True
    )
    if len(avail) == 1:
        axes = [axes]

    for ax, line in zip(axes, avail):
        data = loading[line].dropna()
        ax.hist(data, bins=100, range=(0, 1.05),
                color="#2E86AB", edgecolor="none", alpha=0.8)
        ax.axvline(CONGESTION_THRESHOLD, color="#E84855", lw=1.5, ls="--",
                   label=f"Threshold ({CONGESTION_THRESHOLD})")
        pct = (data >= CONGESTION_THRESHOLD).mean() * 100
        ax.set_title(
            f"{line}  —  {pct:.1f}% congested hours",
            fontsize=9, pad=4
        )
        ax.set_ylabel("Hours", fontsize=8)
        ax.yaxis.set_tick_params(labelsize=7)
        ax.xaxis.set_tick_params(labelsize=7)
        if ax == axes[0]:
            ax.legend(fontsize=7)

    axes[-1].set_xlabel("Line loading fraction (|p0| / s_nom)", fontsize=9)
    fig.suptitle(
        f"Kupferzell Corridor Line Loading Distribution — {SIM_YEAR}",
        fontsize=11, fontweight="bold", y=1.01
    )
    plt.tight_layout()
    out_path = os.path.join(OUTPUT_DIR, "figure_loading_dist.png")
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  ✓ Saved {out_path}")


def plot_seasonal_heatmap(matrix: pd.DataFrame,
                           primary_line: str) -> None:
    """Plot month × hour congestion frequency heatmap."""
    if matrix.empty:
        return

    fig, ax = plt.subplots(figsize=(12, 4))
    cmap = plt.cm.YlOrRd
    im = ax.imshow(matrix.values, aspect="auto", cmap=cmap,
                   vmin=0, vmax=100, interpolation="nearest")

    ax.set_xticks(range(24))
    ax.set_xticklabels([f"{h:02d}:00" for h in range(24)],
                        rotation=45, ha="right", fontsize=7)
    ax.set_yticks(range(len(matrix.index)))
    ax.set_yticklabels(matrix.index, fontsize=8)
    ax.set_xlabel("Hour of day (local time)", fontsize=9)
    ax.set_ylabel("Month", fontsize=9)
    ax.set_title(
        f"Congestion Frequency (%) — {primary_line} — {SIM_YEAR}",
        fontsize=10, fontweight="bold"
    )
    plt.colorbar(im, ax=ax, label="% of hours congested")
    plt.tight_layout()
    out_path = os.path.join(OUTPUT_DIR, "figure_seasonal_heatmap.png")
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  ✓ Saved {out_path}")


def plot_monthly_congestion(monthly: pd.DataFrame) -> None:
    """Bar chart of monthly congestion hours per corridor line."""
    if monthly.empty:
        return

    fig, ax = plt.subplots(figsize=(10, 4))
    x = np.arange(len(monthly))
    width = 0.8 / max(len(monthly.columns), 1)
    colors = plt.cm.tab10.colors

    for i, col in enumerate(monthly.columns):
        offset = (i - len(monthly.columns) / 2 + 0.5) * width
        ax.bar(x + offset, monthly[col], width=width * 0.9,
               label=col, color=colors[i % 10], alpha=0.85)

    ax.set_xticks(x)
    ax.set_xticklabels(monthly.index, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Congested hours per month", fontsize=9)
    ax.set_xlabel("Month", fontsize=9)
    ax.set_title(
        f"Monthly Congestion Hours — Kupferzell Corridor — {SIM_YEAR}",
        fontsize=10, fontweight="bold"
    )
    ax.legend(fontsize=7, loc="upper right")
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    out_path = os.path.join(OUTPUT_DIR, "figure_monthly_congestion.png")
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  ✓ Saved {out_path}")


def export_paper_tables(stats: pd.DataFrame,
                         monthly: pd.DataFrame,
                         corridor_lines: pd.Index) -> None:
    """
    Export LaTeX-formatted summary table for direct inclusion in paper.
    """
    # Table 1: Per-line congestion summary
    stats_out = stats.copy()
    stats_out = stats_out.round(3)
    stats_out.index = [l.replace("_", "\\_") for l in stats_out.index]
    latex_t1 = stats_out[
        ["n_congested_h", "pct_congested", "mean_loading",
         "p95_loading", "max_loading"]
    ].to_latex(
        float_format="%.2f",
        caption=(
            f"Kupferzell corridor line congestion statistics, {SIM_YEAR}. "
            r"Congestion defined as $|p_{0}| / s_{\mathrm{nom}} \geq 0.98$. "
            "Source: PyPSA-EUR LOPF simulation using SMARD observed dispatch."
        ),
        label="tab:kupferzell_congestion",
        column_format="lrrrrrr",
        escape=False,
    )
    with open(os.path.join(OUTPUT_DIR, "table_congestion_summary.tex"), "w") as f:
        f.write(latex_t1)
    print(f"  ✓ LaTeX table: table_congestion_summary.tex")

    # Table 2: Monthly congestion
    monthly_out = monthly.copy()
    monthly_out.index.name = "Month"
    monthly_out.columns = [c.replace("_", "\\_") for c in monthly_out.columns]
    latex_t2 = monthly_out.to_latex(
        float_format="%.0f",
        caption=(
            f"Monthly congestion hours per corridor line, {SIM_YEAR}. "
            "Values indicate number of hours in each calendar month "
            r"with line loading $\geq 0.98 \cdot s_{\mathrm{nom}}$."
        ),
        label="tab:monthly_congestion",
        column_format="l" + "r" * len(monthly_out.columns),
        escape=False,
    )
    with open(os.path.join(OUTPUT_DIR, "table_monthly_congestion.tex"), "w") as f:
        f.write(latex_t2)
    print(f"  ✓ LaTeX table: table_monthly_congestion.tex")


def print_headline_results(stats: pd.DataFrame) -> None:
    """Print headline numbers for paper abstract / introduction."""
    print("\n" + "═" * 60)
    print("  HEADLINE RESULTS FOR PAPER")
    print("═" * 60)

    total_ch = stats["n_congested_h"].sum()
    max_pct = stats["pct_congested"].max()
    most_congested = stats["pct_congested"].idxmax()

    print(f"\n  Total corridor congestion-hours ({SIM_YEAR}): "
          f"{total_ch:.0f} h")
    print(f"  Most congested line: {most_congested}")
    print(f"    → Congested {max_pct:.1f}% of the year "
          f"({stats.loc[most_congested, 'n_congested_h']:.0f} h)")
    print(f"\n  Mean corridor loading: "
          f"{stats['mean_loading'].mean():.3f} (fraction of s_nom)")
    print(f"  P95 corridor loading: "
          f"{stats['p95_loading'].max():.3f}")

    print("\n  NOTE: These are simulation-derived estimates, subject to:")
    print("    (a) PyPSA-EUR DC linearisation error (~5–15% vs AC flows)")
    print("    (b) 128-bus spatial aggregation uncertainty")
    print("    (c) SMARD data completeness (verify NaN fraction < 1%)")
    print("    (d) Generator capacity assumptions (BNetzA verification needed)")
    print("═" * 60)


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Step 4: Kupferzell Corridor Congestion Analysis")
    print("=" * 60)

    print("\n[4.1] Loading LOPF results ...")
    n, loading = load_results()

    print("\n[4.2] Identifying Kupferzell corridor lines ...")
    corridor_lines = identify_corridor_lines(n)

    if corridor_lines.empty:
        raise RuntimeError(
            "No corridor lines identified. Possible causes:\n"
            "  1. The 128-bus clustering aggregates all of BW into 1 node\n"
            "     → Try a finer clustering (256 or 512 buses)\n"
            "  2. Bus geographic coordinates are missing\n"
            "     → Check n.buses['x'] and n.buses['y'] columns\n"
            "  3. Increase CORRIDOR_RADIUS_DEG in config\n"
        )

    print("\n[4.3] Computing congestion statistics ...")
    stats = compute_congestion_stats(loading, corridor_lines)

    print("\n[4.4] Computing seasonal patterns ...")
    seasonal_matrix, primary_line = seasonal_congestion_matrix(
        loading, corridor_lines
    )

    print("\n[4.5] Computing monthly breakdown ...")
    monthly = aggregate_congestion_by_period(loading, corridor_lines)

    print("\n[4.6] Saving CSV outputs ...")
    stats.to_csv(os.path.join(OUTPUT_DIR, "congestion_summary.csv"))
    monthly.to_csv(os.path.join(OUTPUT_DIR, "congestion_monthly.csv"))

    congested_binary = (loading[
        [l for l in corridor_lines if l in loading.columns]
    ] >= CONGESTION_THRESHOLD).astype(int)
    congested_binary.to_csv(os.path.join(OUTPUT_DIR, "congestion_hourly.csv"))
    print(f"  ✓ CSVs saved to {OUTPUT_DIR}/")

    print("\n[4.7] Generating figures ...")
    plot_loading_distribution(loading, corridor_lines)
    plot_seasonal_heatmap(seasonal_matrix, primary_line)
    plot_monthly_congestion(monthly)

    print("\n[4.8] Exporting LaTeX tables ...")
    export_paper_tables(stats, monthly, corridor_lines)

    print_headline_results(stats)

    print(f"\n✓ Step 4 complete. All outputs in {OUTPUT_DIR}/")
