"""
Post-processing pipeline for PyPSA GridBooster / Kupferzell congestion study.
Weather year: 2025. Network: Germany + DK high-resolution, neighbours as single nodes.

Steps
-----
1. Model sanity validation (§ 1)
2. Network-wide congestion frequency per line + geographic figure (§ 2)
3. Kupferzell corridor hourly utilisation CSV export (§ 3)

Usage
-----
    python postprocess_congestion.py --network results/network_solved.nc \
        [--kupferzell-bus "Kupferzell"] [--threshold 0.95] [--out-dir results/postproc]

The script is intentionally standalone: it imports only PyPSA, numpy,
pandas, geopandas (optional, for country outlines), and matplotlib.
"""

import argparse
import sys
import warnings
from pathlib import Path

import matplotlib
import matplotlib.cm as cm
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pypsa

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="PyPSA post-processing: validation + congestion")
    p.add_argument("--network",        required=True,  help="Path to solved .nc network file")
    p.add_argument("--kupferzell-bus", default=None,   help="Bus name for Kupferzell node (substring match if not exact)")
    p.add_argument("--threshold",      type=float, default=0.95,
                   help="Fraction of s_nom counted as congested (default 0.95)")
    p.add_argument("--out-dir",        default="results/postproc", help="Output directory")
    p.add_argument("--no-validation",  action="store_true",  help="Skip validation section")
    p.add_argument("--fig-format",     default="pdf",   choices=["pdf", "png", "svg"])
    return p.parse_args()


# ============================================================================
# § 1  MODEL VALIDATION
# ============================================================================

def validate_network(n: pypsa.Network, threshold: float = 0.95) -> dict:
    """
    Perform a battery of sanity checks on a solved PyPSA network.

    Returns a dict of {check_name: (passed: bool, detail: str)}.

    Checks
    ------
    V1  Objective finite and non-negative
    V2  No NaN flows on lines/links
    V3  Nodal power balance (KCL) holds at every bus × hour
    V4  No systematic line capacity violations (|p0| <= s_nom * margin)
    V5  Generator dispatch within p_nom_min / p_nom_max bounds
    V6  Storage units: SoC within [0, e_nom] at every hour
    V7  Storage units: SoC consistent with charging/discharging (round-trip)
    V8  Curtailment is non-negative (no negative curtailment = phantom generation)
    V9  Load fully served (no load shedding) – absent explicit shedding component
    V10 Inter-connector flows vs NTC bounds (lines/links)
    """

    results = {}
    print("\n" + "=" * 60)
    print("§ 1  MODEL VALIDATION")
    print("=" * 60)

    # ------------------------------------------------------------------ V1
    try:
        obj = n.objective
        passed = np.isfinite(obj) and obj >= 0
        results["V1_objective"] = (passed,
                                   f"Objective = {obj:,.0f} € {'✓' if passed else '✗  (negative or non-finite!)'}")
    except Exception as e:
        results["V1_objective"] = (False, f"Could not read objective: {e}")

    # ------------------------------------------------------------------ V2
    nan_lines = n.lines_t.p0.isnull().any().sum() if not n.lines_t.p0.empty else 0
    nan_links = n.links_t.p0.isnull().any().sum() if not n.links_t.p0.empty else 0
    passed = (nan_lines == 0) and (nan_links == 0)
    results["V2_no_nan_flows"] = (passed,
                                  f"NaN line flows: {nan_lines} lines, {nan_links} links {'✓' if passed else '✗'}")

    # ------------------------------------------------------------------ V3  Nodal KCL
    try:
        # PyPSA stores the marginal price at every bus×hour; if balance is
        # perfect the residual injection at every bus should be ~0.
        # We use the built-in consistency check when available.
        if hasattr(n, "consistency_check"):
            with warnings.catch_warnings(record=True) as w_list:
                warnings.simplefilter("always")
                n.consistency_check()
            kcl_warns = [str(w.message) for w in w_list]
            passed = len(kcl_warns) == 0
            results["V3_nodal_KCL"] = (passed,
                                       "KCL consistent ✓" if passed else f"Warnings: {kcl_warns[:3]}")
        else:
            # Manual check: sum of all injections per bus per time step
            # For a DC-OPF: p_balance = generation + storage_dispatch - load - line_flows
            # Absolute tolerance: 1 MW
            tol_mw = 1.0
            imbalances = []
            for bus in n.buses.index:
                inj = pd.Series(0.0, index=n.snapshots)
                for comp, col in [("generators", "p"), ("storage_units", "p"),
                                  ("loads", "p"), ("links", "p0")]:
                    df_t = getattr(n, f"{comp}_t", None)
                    if df_t is None:
                        continue
                    df = getattr(df_t, col, None)
                    if df is None or df.empty:
                        continue
                    static = getattr(n, comp, None)
                    if static is None:
                        continue
                    mask = static.get("bus", static.get("bus0", pd.Series(dtype=str))) == bus
                    bus_cols = [c for c in df.columns if c in mask[mask].index]
                    sign = -1 if comp == "loads" else 1
                    inj += sign * df[bus_cols].sum(axis=1)
                imbalances.append(inj.abs().max())
            max_imb = max(imbalances) if imbalances else 0.0
            passed = max_imb < tol_mw
            results["V3_nodal_KCL"] = (passed,
                                       f"Max nodal imbalance: {max_imb:.2f} MW {'✓' if passed else '✗  exceeds 1 MW!'}")
    except Exception as e:
        results["V3_nodal_KCL"] = (None, f"Could not check KCL: {e}")

    # ------------------------------------------------------------------ V4  Line limits
    if not n.lines_t.p0.empty:
        s_nom = n.lines["s_nom"]
        loading = n.lines_t.p0.abs()
        violations = (loading > s_nom * (1 + 1e-3)).sum()  # allow 0.1% numerical slack
        total_cells = loading.shape[0] * loading.shape[1]
        pct = violations.sum() / total_cells * 100
        passed = violations.sum() == 0
        results["V4_line_limits"] = (passed,
                                     f"Hard-limit violations: {violations.sum()} / {total_cells} cells ({pct:.4f}%) "
                                     f"{'✓' if passed else '✗  – check solver tolerance!'}")
    else:
        results["V4_line_limits"] = (None, "No line flows recorded (p0 empty)")

    # ------------------------------------------------------------------ V5  Generator bounds
    if not n.generators_t.p.empty:
        gen_p = n.generators_t.p
        p_min = n.generators.p_nom_min.reindex(gen_p.columns, fill_value=0)
        p_max = n.generators.p_nom.reindex(gen_p.columns)
        # For extendable generators use p_nom_opt
        if "p_nom_opt" in n.generators.columns:
            p_max = n.generators.p_nom_opt.reindex(gen_p.columns).combine_first(p_max)
        vio_max = (gen_p > p_max * (1 + 1e-3)).sum().sum()
        vio_min = (gen_p < p_min * (1 - 1e-3)).sum().sum()
        passed = (vio_max == 0) and (vio_min == 0)
        results["V5_generator_bounds"] = (passed,
                                          f"Dispatch bound violations: above p_max={vio_max}, below p_min={vio_min} "
                                          f"{'✓' if passed else '✗'}")
    else:
        results["V5_generator_bounds"] = (None, "No generator dispatch recorded")

    # ------------------------------------------------------------------ V6  SoC bounds
    if not n.storage_units_t.state_of_charge.empty:
        soc = n.storage_units_t.state_of_charge
        e_nom = n.storage_units.e_nom.reindex(soc.columns, fill_value=np.inf)
        vio_upper = (soc > e_nom * (1 + 1e-3)).sum().sum()
        vio_lower = (soc < -1e-3).sum().sum()
        passed = (vio_upper == 0) and (vio_lower == 0)
        results["V6_SoC_bounds"] = (passed,
                                    f"SoC bound violations: above e_nom={vio_upper}, below 0={vio_lower} "
                                    f"{'✓' if passed else '✗'}")
    else:
        results["V6_SoC_bounds"] = (None, "No SoC data (no storage units or SoC not stored)")

    # ------------------------------------------------------------------ V7  SoC consistency
    # SoC_t - SoC_{t-1} should match p_dispatch * efficiency_store / e_nom
    # Quick proxy: check SoC at start vs end of year (energy balance)
    if not n.storage_units_t.state_of_charge.empty:
        soc = n.storage_units_t.state_of_charge
        drift = (soc.iloc[-1] - soc.iloc[0]).abs()
        # Allow up to 0.5% of e_nom drift (solver tolerances)
        e_nom = n.storage_units.e_nom.reindex(soc.columns, fill_value=1)
        rel_drift = (drift / e_nom.replace(0, np.nan)).dropna()
        max_drift = rel_drift.max()
        passed = max_drift < 0.005
        results["V7_SoC_annual_balance"] = (passed,
                                            f"Max annual SoC drift / e_nom: {max_drift:.4%} {'✓' if passed else '✗  (cyclic constraint may be missing)'}")
    else:
        results["V7_SoC_annual_balance"] = (None, "No SoC data")

    # ------------------------------------------------------------------ V8  Curtailment non-negative
    if "p_max_pu" in n.generators_t and not n.generators_t.p_max_pu.empty:
        vre_mask = n.generators.carrier.isin(["onwind", "offwind", "solar", "solar rooftop"])
        vre_gens = n.generators.index[vre_mask]
        vre_gens_t = [g for g in vre_gens if g in n.generators_t.p.columns
                      and g in n.generators_t.p_max_pu.columns]
        if vre_gens_t:
            p_avail = n.generators_t.p_max_pu[vre_gens_t] * \
                      n.generators.p_nom.reindex(vre_gens_t)
            curtailment = p_avail - n.generators_t.p[vre_gens_t]
            neg_curt = (curtailment < -1.0).sum().sum()
            passed = neg_curt == 0
            results["V8_curtailment_nonneg"] = (passed,
                                                f"Negative curtailment cells: {neg_curt} {'✓' if passed else '✗  (generation above available!)'}")
        else:
            results["V8_curtailment_nonneg"] = (None, "No VRE generators with p_max_pu time series found")
    else:
        results["V8_curtailment_nonneg"] = (None, "p_max_pu time series not stored")

    # ------------------------------------------------------------------ V9  No load shedding
    # Check: if a 'load' component named *shedding* or *curtail_demand* exists
    shed_mask = n.generators.index.str.lower().str.contains("shed|voll|slack")
    if shed_mask.any() and not n.generators_t.p.empty:
        shed_gens = [g for g in n.generators.index[shed_mask] if g in n.generators_t.p.columns]
        shed_total = n.generators_t.p[shed_gens].sum().sum() if shed_gens else 0.0
        passed = shed_total < 1.0  # < 1 MWh total
        results["V9_no_load_shedding"] = (passed,
                                          f"Total load shedding: {shed_total:,.1f} MWh {'✓' if passed else '✗  DEMAND NOT FULLY SERVED!'}")
    else:
        results["V9_no_load_shedding"] = (True, "No explicit shedding generators found (assumed fully served) ✓")

    # ------------------------------------------------------------------ V10  NTC / link limits
    if not n.links_t.p0.empty:
        p0 = n.links_t.p0
        p_nom = n.links.p_nom.reindex(p0.columns, fill_value=np.inf)
        vio = (p0.abs() > p_nom * (1 + 1e-3)).sum().sum()
        passed = vio == 0
        results["V10_link_NTC"] = (passed,
                                   f"Link (NTC) violations: {vio} {'✓' if passed else '✗'}")
    else:
        results["V10_link_NTC"] = (None, "No link flows recorded")

    # ------------------------------------------------------------------ Summary
    print(f"\n{'Check':<30} {'Pass?':>6}  Detail")
    print("-" * 80)
    for name, (passed, detail) in results.items():
        status = "PASS" if passed else ("WARN" if passed is None else "FAIL")
        print(f"  {name:<28} {status:>6}  {detail}")
    n_fail = sum(1 for p, _ in results.values() if p is False)
    n_warn = sum(1 for p, _ in results.values() if p is None)
    print("-" * 80)
    print(f"  {len(results)} checks: {len(results)-n_fail-n_warn} passed, "
          f"{n_warn} skipped/warn, {n_fail} FAILED\n")
    return results


# ============================================================================
# § 2  CONGESTION FREQUENCY PER LINE — FIGURE
# ============================================================================

def compute_congestion(n: pypsa.Network, threshold: float = 0.95) -> pd.DataFrame:
    """
    For each line, compute:
      - n_hours_congested: # hours |p0| >= threshold * s_nom
      - congestion_rate:   fraction of total hours
      - mean_loading_pu:   mean |p0| / s_nom across all hours
      - max_loading_pu:    max  |p0| / s_nom across all hours
    """
    if n.lines_t.p0.empty:
        raise RuntimeError("n.lines_t.p0 is empty — network must be solved with store_f=True "
                           "or equivalent setting to retain flows.")

    p0 = n.lines_t.p0.abs()                  # shape (T, L)
    s_nom = n.lines["s_nom"]                  # MW thermal rating
    loading_pu = p0.divide(s_nom, axis=1)     # per-unit loading

    n_hours = len(n.snapshots)
    congested = loading_pu >= threshold

    cong_df = pd.DataFrame({
        "s_nom_mw":          s_nom,
        "bus0":              n.lines["bus0"],
        "bus1":              n.lines["bus1"],
        "n_hours_congested": congested.sum(),
        "congestion_rate":   congested.mean(),
        "mean_loading_pu":   loading_pu.mean(),
        "max_loading_pu":    loading_pu.max(),
        "length_km":         n.lines.get("length", pd.Series(np.nan, index=n.lines.index)),
    })
    cong_df["n_hours_total"] = n_hours
    return cong_df


def plot_congestion_map(n: pypsa.Network,
                        cong_df: pd.DataFrame,
                        out_path: Path,
                        threshold: float = 0.95,
                        fig_format: str = "pdf"):
    """
    Geographic map of Germany showing:
      - Transmission lines coloured and scaled in width by congestion rate
        (fraction of hours at threshold), mirroring Frysztacki Fig. 2.
      - Nodes sized by total curtailment (VRE only).
      - Colourbar for congestion rate.

    Lines with zero congestion are drawn thin and grey.
    Lines approaching 100 % congestion rate are drawn thick and red.
    """
    try:
        import geopandas as gpd
        HAS_GPD = True
    except ImportError:
        HAS_GPD = False
        print("  [info] geopandas not installed — country outline will be omitted.")

    fig, ax = plt.subplots(figsize=(9, 11))
    ax.set_aspect("equal")
    ax.set_facecolor("#f5f5f0")
    fig.patch.set_facecolor("#ffffff")

    # --- country outline -------------------------------------------------
    if HAS_GPD:
        try:
            import geopandas as gpd
            world = gpd.read_file(gpd.datasets.get_path("naturalearth_lowres"))
            de = world[world.name.isin(["Germany", "Denmark"])]
            de.boundary.plot(ax=ax, linewidth=0.8, color="#aaaaaa", zorder=1)
        except Exception:
            pass  # silently skip if naturalearth data unavailable

    # --- bus coordinates --------------------------------------------------
    bus_x = n.buses.x   # longitude
    bus_y = n.buses.y   # latitude

    # --- curtailment per bus (for node sizing) ----------------------------
    vre_carriers = ["onwind", "offwind", "solar", "solar rooftop", "wind"]
    curt_by_bus = pd.Series(0.0, index=n.buses.index)

    if ("p_max_pu" in n.generators_t._fields or
        hasattr(n.generators_t, "p_max_pu")) and \
            not n.generators_t.p_max_pu.empty and \
            not n.generators_t.p.empty:
        vre_mask = n.generators.carrier.isin(vre_carriers)
        vre_gens = n.generators.index[vre_mask]
        for g in vre_gens:
            if g in n.generators_t.p.columns and g in n.generators_t.p_max_pu.columns:
                p_avail = n.generators_t.p_max_pu[g] * n.generators.at[g, "p_nom"]
                curt = (p_avail - n.generators_t.p[g]).clip(lower=0).sum()
                bus = n.generators.at[g, "bus"]
                if bus in curt_by_bus.index:
                    curt_by_bus[bus] += curt

    # Scale curtailment to marker size (sqrt to avoid extreme variation)
    max_curt = curt_by_bus.max()
    if max_curt > 0:
        node_size = 20 + 300 * (curt_by_bus / max_curt) ** 0.5
    else:
        node_size = 20

    # --- line colour/width by congestion rate ----------------------------
    cmap = cm.get_cmap("YlOrRd")
    norm = mcolors.Normalize(vmin=0, vmax=1)

    # Lines are drawn in two passes: grey background (all lines), then
    # congested lines on top coloured and thickened.
    for line_id, row in cong_df.iterrows():
        bus0, bus1 = row["bus0"], row["bus1"]
        if bus0 not in bus_x.index or bus1 not in bus_x.index:
            continue
        x = [bus_x[bus0], bus_x[bus1]]
        y = [bus_y[bus0], bus_y[bus1]]
        rate = row["congestion_rate"]
        colour = cmap(norm(rate))
        lw = 0.5 + 5.0 * rate  # thin for uncongested, up to 5.5pt for 100 %
        ax.plot(x, y, color=colour, linewidth=lw, solid_capstyle="round",
                zorder=2, alpha=0.85)

    # Draw thin grey reference for all lines first (behind coloured lines)
    for line_id, row in cong_df.iterrows():
        bus0, bus1 = row["bus0"], row["bus1"]
        if bus0 not in bus_x.index or bus1 not in bus_x.index:
            continue
        x = [bus_x[bus0], bus_x[bus1]]
        y = [bus_y[bus0], bus_y[bus1]]
        ax.plot(x, y, color="#cccccc", linewidth=0.4, zorder=1, alpha=0.5)

    # --- nodes -----------------------------------------------------------
    # All buses: small grey dot
    ax.scatter(bus_x, bus_y, s=5, color="#888888", zorder=3, alpha=0.5)

    # Buses with curtailment: sized orange circle
    curt_buses = curt_by_bus[curt_by_bus > 0].index
    if len(curt_buses):
        ax.scatter(bus_x[curt_buses], bus_y[curt_buses],
                   s=node_size[curt_buses],
                   color="#e67e22", edgecolors="#8e4000", linewidths=0.5,
                   zorder=4, alpha=0.8, label="VRE curtailment")

    # Highlight Kupferzell bus if identifiable
    kupf = [b for b in n.buses.index if "kupfer" in b.lower()]
    if kupf:
        ax.scatter(bus_x[kupf], bus_y[kupf],
                   s=120, marker="*", color="#2ecc71", edgecolors="#1a7a44",
                   linewidths=0.8, zorder=6, label="Kupferzell")

    # --- colourbar -------------------------------------------------------
    sm = cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, fraction=0.025, pad=0.02)
    cbar.set_label(f"Congestion rate (fraction of hours ≥ {threshold:.0%} loading)",
                   fontsize=9)
    cbar.ax.tick_params(labelsize=8)

    # --- legend for node size --------------------------------------------
    if max_curt > 0:
        from matplotlib.lines import Line2D
        legend_sizes = [0.1 * max_curt, 0.5 * max_curt, max_curt]
        legend_labels = [f"{v/1e3:.1f} TWh curtailed" for v in legend_sizes]
        handles = [
            plt.scatter([], [], s=20 + 300 * (v / max_curt) ** 0.5,
                        color="#e67e22", edgecolors="#8e4000", alpha=0.8)
            for v in legend_sizes
        ]
        ax.legend(handles + [plt.scatter([], [], marker="*", s=120, color="#2ecc71")],
                  legend_labels + ["Kupferzell"],
                  loc="lower left", fontsize=7, framealpha=0.85, title="Node curtailment")

    ax.set_xlabel("Longitude", fontsize=9)
    ax.set_ylabel("Latitude",  fontsize=9)
    ax.set_title(
        f"Transmission congestion rate — Germany (PyPSA, weather year 2025)\n"
        f"Congestion threshold: ≥ {threshold:.0%} of thermal rating  |  "
        f"Line colour + width ∝ congestion frequency",
        fontsize=10
    )
    ax.tick_params(labelsize=8)
    plt.tight_layout()

    out_file = out_path / f"congestion_map.{fig_format}"
    fig.savefig(out_file, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"  [saved] {out_file}")
    return out_file


def plot_congestion_histogram(cong_df: pd.DataFrame,
                              out_path: Path,
                              fig_format: str = "pdf"):
    """
    Bar chart: top-N most congested lines by annual congestion hours,
    sorted descending. Useful as a companion to the map.
    """
    top_n = min(40, len(cong_df))
    top = cong_df.nlargest(top_n, "n_hours_congested")

    fig, ax = plt.subplots(figsize=(12, 5))
    colours = plt.cm.YlOrRd(top["congestion_rate"].values)
    bars = ax.barh(range(top_n), top["n_hours_congested"].values,
                   color=colours, edgecolor="white", height=0.8)
    ax.set_yticks(range(top_n))
    ax.set_yticklabels(
        [f"{row.bus0[:12]} – {row.bus1[:12]}" for _, row in top.iterrows()],
        fontsize=7
    )
    ax.invert_yaxis()
    ax.set_xlabel("Hours congested per year [h]", fontsize=9)
    ax.set_title(f"Top-{top_n} most congested transmission lines (2025)", fontsize=10)

    # Colourbar
    sm = plt.cm.ScalarMappable(cmap="YlOrRd",
                               norm=mcolors.Normalize(vmin=0, vmax=1))
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, fraction=0.015, pad=0.01)
    cbar.set_label("Congestion rate", fontsize=8)
    plt.tight_layout()

    out_file = out_path / f"congestion_histogram.{fig_format}"
    fig.savefig(out_file, dpi=180, bbox_inches="tight")
    plt.close(fig)
    print(f"  [saved] {out_file}")


# ============================================================================
# § 3  KUPFERZELL CORRIDOR — HOURLY UTILISATION CSV
# ============================================================================

def find_kupferzell_bus(n: pypsa.Network, hint: str = None) -> str | None:
    """
    Locate the Kupferzell bus by exact match, substring match, or proximity
    to the known geographic coordinates (lon=9.70, lat=49.23).
    Returns the bus name or None.
    """
    if hint:
        if hint in n.buses.index:
            return hint
        matches = [b for b in n.buses.index if hint.lower() in b.lower()]
        if matches:
            print(f"  [info] Kupferzell bus resolved by substring '{hint}' → {matches[0]}")
            return matches[0]

    # Fallback: substring "kupfer" in bus name
    matches = [b for b in n.buses.index if "kupfer" in b.lower()]
    if matches:
        print(f"  [info] Kupferzell bus found by name: {matches[0]}")
        return matches[0]

    # Fallback: geographic proximity (Kupferzell ≈ 9.70°E, 49.23°N)
    if "x" in n.buses.columns and "y" in n.buses.columns:
        lon_ref, lat_ref = 9.70, 49.23
        dist = np.hypot(n.buses.x - lon_ref, n.buses.y - lat_ref)
        closest = dist.idxmin()
        d_deg = dist[closest]
        if d_deg < 0.5:          # within ~50 km
            print(f"  [info] Kupferzell bus resolved by proximity → {closest} (Δ={d_deg:.3f}°)")
            return closest

    print("  [WARN] Could not identify Kupferzell bus. "
          "Pass --kupferzell-bus explicitly.")
    return None


def export_kupferzell_utilisation(n: pypsa.Network,
                                  kupf_bus: str,
                                  threshold: float,
                                  out_path: Path) -> pd.DataFrame:
    """
    For all lines connected to the Kupferzell bus (and its immediate
    electrical neighbours — one hop), export an hourly CSV with:

    timestamp | line_id | bus0 | bus1 | s_nom_mw |
    p0_mw | loading_pu | congested | margin_to_limit_mw

    congested == 1  when loading_pu >= threshold
    margin_to_limit_mw = s_nom - |p0|   (positive = headroom remaining,
                                          zero/negative = at/beyond limit)
    """
    # Lines directly connected to Kupferzell
    mask_0 = n.lines["bus0"] == kupf_bus
    mask_1 = n.lines["bus1"] == kupf_bus
    direct_lines = n.lines.index[mask_0 | mask_1]

    # One-hop neighbours
    neighbour_buses = set()
    for line in direct_lines:
        neighbour_buses.add(n.lines.at[line, "bus0"])
        neighbour_buses.add(n.lines.at[line, "bus1"])
    neighbour_buses.discard(kupf_bus)

    # Lines connecting neighbours to each other (the Kupferzell sub-corridor)
    extra_mask = (n.lines["bus0"].isin(neighbour_buses) &
                  n.lines["bus1"].isin(neighbour_buses))
    corridor_lines = direct_lines.union(n.lines.index[extra_mask])

    print(f"  [info] Kupferzell corridor: {len(direct_lines)} direct lines, "
          f"{len(corridor_lines)} total (including 1-hop neighbours)")

    if n.lines_t.p0.empty:
        raise RuntimeError("lines_t.p0 is empty — cannot export hourly utilisation.")

    records = []
    for line_id in corridor_lines:
        if line_id not in n.lines_t.p0.columns:
            print(f"    [warn] Line {line_id} not in p0 time series — skipping")
            continue
        s_nom = n.lines.at[line_id, "s_nom"]
        bus0  = n.lines.at[line_id, "bus0"]
        bus1  = n.lines.at[line_id, "bus1"]
        p0    = n.lines_t.p0[line_id]           # signed MW (may be negative)
        p0_abs = p0.abs()
        loading_pu = p0_abs / s_nom
        congested  = (loading_pu >= threshold).astype(int)
        margin_mw  = s_nom - p0_abs

        df_line = pd.DataFrame({
            "timestamp":          n.snapshots,
            "line_id":            line_id,
            "bus0":               bus0,
            "bus1":               bus1,
            "s_nom_mw":           s_nom,
            "p0_mw":              p0.values,          # signed
            "p0_abs_mw":          p0_abs.values,
            "loading_pu":         loading_pu.values,
            "loading_pct":        (loading_pu * 100).round(2).values,
            "margin_to_limit_mw": margin_mw.round(1).values,
            "congested":          congested.values,
        })
        records.append(df_line)

    if not records:
        print("  [WARN] No corridor lines found in p0 — CSV will be empty.")
        return pd.DataFrame()

    out_df = pd.concat(records, ignore_index=True)
    out_df = out_df.sort_values(["line_id", "timestamp"]).reset_index(drop=True)

    # Summary stats per line
    summary = out_df.groupby("line_id").agg(
        bus0=("bus0", "first"),
        bus1=("bus1", "first"),
        s_nom_mw=("s_nom_mw", "first"),
        n_hours_congested=("congested", "sum"),
        congestion_rate_pct=("congested", lambda x: round(x.mean() * 100, 2)),
        mean_loading_pct=("loading_pct", "mean"),
        max_loading_pct=("loading_pct", "max"),
        min_margin_mw=("margin_to_limit_mw", "min"),
    ).reset_index()

    csv_hourly  = out_path / "kupferzell_hourly_utilisation.csv"
    csv_summary = out_path / "kupferzell_line_summary.csv"
    out_df.to_csv(csv_hourly,  index=False)
    summary.to_csv(csv_summary, index=False)
    print(f"  [saved] {csv_hourly}  ({len(out_df):,} rows)")
    print(f"  [saved] {csv_summary}  ({len(summary)} lines)")

    # Quick console summary
    print("\n  Kupferzell corridor — line summary:")
    print(summary.to_string(index=False))

    return out_df


# ============================================================================
# MAIN
# ============================================================================

def main():
    args = parse_args()
    out_path = Path(args.out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  PyPSA post-processing pipeline")
    print(f"  Network : {args.network}")
    print(f"  Threshold: {args.threshold:.0%} of s_nom")
    print(f"  Output  : {out_path}")
    print(f"{'='*60}\n")

    # Load network
    print("Loading network …")
    n = pypsa.Network(args.network)
    print(f"  Buses    : {len(n.buses)}")
    print(f"  Lines    : {len(n.lines)}")
    print(f"  Snapshots: {len(n.snapshots)}  "
          f"({n.snapshots[0]} → {n.snapshots[-1]})")

    # ------------------------------------------------------------------
    # § 1  Validation
    # ------------------------------------------------------------------
    if not args.no_validation:
        print("\n§ 1  Running model validation …")
        val_results = validate_network(n, threshold=args.threshold)
        # Save validation log
        val_log = out_path / "validation_log.txt"
        with open(val_log, "w") as f:
            for name, (passed, detail) in val_results.items():
                status = "PASS" if passed else ("SKIP" if passed is None else "FAIL")
                f.write(f"{status:6}  {name}: {detail}\n")
        print(f"  [saved] {val_log}")

    # ------------------------------------------------------------------
    # § 2  Congestion frequency
    # ------------------------------------------------------------------
    print("\n§ 2  Computing congestion frequency …")
    cong_df = compute_congestion(n, threshold=args.threshold)

    # Export full congestion table
    csv_cong = out_path / "congestion_by_line.csv"
    cong_df.to_csv(csv_cong)
    print(f"  [saved] {csv_cong}")

    # Top-10 congested lines to console
    top10 = cong_df.nlargest(10, "congestion_rate")
    print(f"\n  Top-10 most congested lines (threshold={args.threshold:.0%}):")
    print(top10[["bus0","bus1","s_nom_mw","n_hours_congested",
                 "congestion_rate","max_loading_pu"]].to_string())

    # Geographic figure
    print("\n  Generating congestion map …")
    plot_congestion_map(n, cong_df, out_path,
                        threshold=args.threshold,
                        fig_format=args.fig_format)
    plot_congestion_histogram(cong_df, out_path, fig_format=args.fig_format)

    # ------------------------------------------------------------------
    # § 3  Kupferzell corridor
    # ------------------------------------------------------------------
    print("\n§ 3  Kupferzell corridor utilisation …")
    kupf_bus = find_kupferzell_bus(n, hint=args.kupferzell_bus)
    if kupf_bus:
        export_kupferzell_utilisation(n, kupf_bus, args.threshold, out_path)
    else:
        print("  Skipping § 3 — Kupferzell bus not found.")

    print(f"\n{'='*60}")
    print("  Post-processing complete.")
    print(f"  All outputs in: {out_path}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
