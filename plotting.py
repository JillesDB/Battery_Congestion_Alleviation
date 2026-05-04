"""Plotting helpers for PyPSA post-processing outputs.

This module consolidates every plot produced by the HPC-side
post-processing pipeline (occurrence + alleviation + load map).  It is
matplotlib-only -- no cartopy / geopandas / geopy -- so it runs on the
minimal venv described in ``requirements.txt``.

Public functions
----------------
- ``plot_top_congested_lines``      -- horizontal bar chart of worst lines
- ``plot_kupferzell_loading``       -- 2-week loading timeseries near the booster
- ``plot_monthly_congestion``       -- monthly line-hour totals
- ``plot_average_load_map``         -- nodes coloured by mean load MW
- ``plot_congestion_severity_map``  -- lines coloured by congested hours
- ``plot_congestion_occurrence_map``-- alias of the severity map with the
                                       publication-ready defaults used in the paper
- ``plot_congestion_alleviation_map``-- lines coloured by saved congestion cost
                                        (Kupferzell-area zoom, YlGn palette,
                                         all network lines shown)

The occurrence map uses a yellow -> orange -> red (YlOrRd) hue gradient
(severity cue).  The alleviation map uses a yellow -> green (YlGn) palette
to signal that congestion relief is a positive outcome; it zooms into the
Kupferzell area and draws the full local network so lines with zero
alleviation appear as thin black backdrop lines.  The Kupferzell site
(the GridBooster location) is highlighted on every map.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.collections import LineCollection
from matplotlib.colors import LogNorm, Normalize
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, MaxNLocator

# ---------------------------------------------------------------------------
# Kupferzell site -- shared reference point across every map
# ---------------------------------------------------------------------------
KUPFERZELL_LAT = 49.2333
KUPFERZELL_LON = 9.6833
# Radius used by find_kupferzell_lines() in congestion_occurence_pypsa.py;
# kept here so run_validation_pypsa.py can apply the same selection.
KUPFERZELL_RADIUS_DEG = 0.8

# Approximate bounding box of the Baden-Wuerttemberg / N-S corridor study
# area.  Used purely for the default map extent so the relevant substations
# stay centred in the figure -- it does NOT filter the data.
DEFAULT_MAP_EXTENT: tuple[float, float, float, float] = (
    5.5,   # lon_min
    12.5,  # lon_max
    47.0,  # lat_min
    52.5,  # lat_max
)

# Focused bounding box for the congestion-alleviation map.  Congestion is
# alleviated only in the Kupferzell area so we zoom in to that region.
ALLEVIATION_MAP_EXTENT: tuple[float, float, float, float] = (
    5.0,   # lon_min
    12.5,  # lon_max
    46.0,  # lat_min
    52.0,  # lat_max
)

# Zoomed extent for the Kupferzell study-area network map (lon 6–12, lat 46–52).
KUPFERZELL_ZOOM_EXTENT: tuple[float, float, float, float] = (
    6.0,   # lon_min
    12.0,  # lon_max
    46.0,  # lat_min
    52.0,  # lat_max
)

PROJECT_DIR = Path(__file__).resolve().parent
PYPSA_EUR_RESOURCES = PROJECT_DIR.parent / "pypsa-eur" / "resources"
COUNTRY_SHAPES_FILE = PYPSA_EUR_RESOURCES / "country_shapes.geojson"


# ---------------------------------------------------------------------------
# Classic scalar / timeseries plots (kept unchanged from the legacy module)
# ---------------------------------------------------------------------------
def plot_top_congested_lines(summary: pd.DataFrame, output_path: str, top_n: int = 30) -> None:
    """Plot horizontal bar chart for lines with most congested hours."""
    top = summary.sort_values("congested_hours", ascending=False).head(top_n).iloc[::-1]
    plt.figure(figsize=(10, 8))
    plt.barh(top.index.astype(str), top["congested_hours"], color="#cc3300")
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
    plt.plot(monthly_df.index.astype(str), monthly_df["congested_hours"], marker="o", color="#cc3300")
    plt.xticks(rotation=45, ha="right")
    plt.ylabel("Congested line-hours")
    plt.title("Monthly congestion intensity")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


# ---------------------------------------------------------------------------
# Shared map helpers
# ---------------------------------------------------------------------------
def _filter_eligible_lines(
    values: pd.Series,
    buses: pd.DataFrame,
    lines: pd.DataFrame,
    minimum_voltage: float,
    all_lines: bool = False,
) -> pd.DataFrame:
    """Return a DataFrame with (x0, y0, x1, y1) for lines that can be drawn.

    When ``all_lines=True`` every line in ``lines`` is included (not just
    those present in ``values``); lines absent from ``values`` receive a
    metric of 0 and are drawn as the black base network.
    """
    eligible = lines.copy() if all_lines else lines.reindex(values.index).copy()
    if minimum_voltage > 0 and "v_nom" in eligible.columns:
        v_nom = pd.to_numeric(eligible["v_nom"], errors="coerce")
        eligible = eligible.loc[v_nom >= minimum_voltage]
    if eligible.empty:
        return eligible

    coords = buses[["x", "y"]].copy()
    eligible["x0"] = eligible["bus0"].map(coords["x"])
    eligible["y0"] = eligible["bus0"].map(coords["y"])
    eligible["x1"] = eligible["bus1"].map(coords["x"])
    eligible["y1"] = eligible["bus1"].map(coords["y"])
    eligible = eligible.dropna(subset=["x0", "y0", "x1", "y1"])
    return eligible


def _empty_map(output_path: str, message: str) -> None:
    plt.figure(figsize=(7, 5))
    plt.text(0.5, 0.5, message, ha="center", va="center")
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


def _highlight_kupferzell(ax: plt.Axes) -> None:
    """Draw a distinctive marker at the Kupferzell node on every map."""
    ax.plot(
        KUPFERZELL_LON,
        KUPFERZELL_LAT,
        marker="*",
        markersize=14,
        markerfacecolor="#1f77b4",
        markeredgecolor="white",
        markeredgewidth=1.0,
        linestyle="None",
        zorder=5,
        label="Kupferzell GridBooster",
    )


def _apply_map_extent(ax: plt.Axes, buses: pd.DataFrame) -> None:
    """Set axis limits so the whole bus set is visible with a small margin.

    Falls back to ``DEFAULT_MAP_EXTENT`` when the bus set is empty.
    """
    if buses.empty or "x" not in buses.columns or "y" not in buses.columns:
        ax.set_xlim(DEFAULT_MAP_EXTENT[0], DEFAULT_MAP_EXTENT[1])
        ax.set_ylim(DEFAULT_MAP_EXTENT[2], DEFAULT_MAP_EXTENT[3])
        return

    x = pd.to_numeric(buses["x"], errors="coerce").dropna()
    y = pd.to_numeric(buses["y"], errors="coerce").dropna()
    if x.empty or y.empty:
        ax.set_xlim(DEFAULT_MAP_EXTENT[0], DEFAULT_MAP_EXTENT[1])
        ax.set_ylim(DEFAULT_MAP_EXTENT[2], DEFAULT_MAP_EXTENT[3])
        return

    pad_x = 0.03 * max(x.max() - x.min(), 1.0)
    pad_y = 0.03 * max(y.max() - y.min(), 1.0)
    ax.set_xlim(x.min() - pad_x, x.max() + pad_x)
    ax.set_ylim(y.min() - pad_y, y.max() + pad_y)


def _iter_geojson_rings(geometry: dict) -> Iterable[list[list[float]]]:
    """Yield rings as lon/lat coordinate sequences from Polygon-like GeoJSON."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if gtype == "Polygon":
        for ring in coords:
            yield ring
    elif gtype == "MultiPolygon":
        for polygon in coords:
            for ring in polygon:
                yield ring
    elif gtype == "LineString":
        yield coords
    elif gtype == "MultiLineString":
        for line in coords:
            yield line


def _draw_geojson_boundaries(
    ax: plt.Axes,
    geojson_path: Path,
    color: str,
    linewidth: float,
    alpha: float,
    zorder: int,
) -> None:
    """Draw GeoJSON polygon/line boundaries as a background overlay."""
    if not geojson_path.exists():
        return
    try:
        with geojson_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    for feature in data.get("features", []):
        geometry = feature.get("geometry") or {}
        for ring in _iter_geojson_rings(geometry):
            if not ring:
                continue
            arr = np.asarray(ring, dtype=float)
            if arr.ndim != 2 or arr.shape[1] < 2:
                continue
            ax.plot(
                arr[:, 0],
                arr[:, 1],
                color=color,
                linewidth=linewidth,
                alpha=alpha,
                zorder=zorder,
            )


def _draw_background_overlays(ax: plt.Axes, buses: pd.DataFrame) -> None:
    """Overlay country outlines plus network nodes."""
    _draw_geojson_boundaries(
        ax=ax,
        geojson_path=COUNTRY_SHAPES_FILE,
        color="#5f5f5f",
        linewidth=0.8,
        alpha=0.55,
        zorder=0,
    )

    if not buses.empty and {"x", "y"}.issubset(buses.columns):
        ax.scatter(
            buses["x"],
            buses["y"],
            s=3.5,
            facecolors="none",
            edgecolors="#4f4f4f",
            linewidths=0.25,
            alpha=0.45,
            zorder=2,
        )


def _bus_country_codes(buses: pd.DataFrame) -> pd.Series:
    """Return per-bus country codes (prefer explicit column, else infer from id prefix)."""
    if "country" in buses.columns:
        return buses["country"].astype(str).str.upper()
    return buses.index.to_series().astype(str).str[:2].str.upper()


def _draw_kupferzell_lines(
    ax: plt.Axes,
    kupferzell_line_ids: pd.Index,
    buses: pd.DataFrame,
    lines: pd.DataFrame,
) -> None:
    """Overlay Kupferzell-connected lines with a dashed blue highlight."""
    if kupferzell_line_ids is None or len(kupferzell_line_ids) == 0:
        return
    kup = lines.reindex(kupferzell_line_ids).dropna(subset=["bus0", "bus1"])
    if kup.empty:
        return
    coords = buses[["x", "y"]]
    kup = kup.copy()
    kup["x0"] = kup["bus0"].map(coords["x"])
    kup["y0"] = kup["bus0"].map(coords["y"])
    kup["x1"] = kup["bus1"].map(coords["x"])
    kup["y1"] = kup["bus1"].map(coords["y"])
    kup = kup.dropna(subset=["x0", "y0", "x1", "y1"])
    if kup.empty:
        return
    segs = [[(r.x0, r.y0), (r.x1, r.y1)] for r in kup.itertuples(index=False)]
    lc = LineCollection(
        segs,
        colors="#1f77b4",
        linewidths=3.0,
        linestyle="-",
        alpha=0.85,
        zorder=4,
        label="Kupferzell GridBooster target lines",
    )
    ax.add_collection(lc)


def _plot_line_metric_map(
    values: pd.Series,
    buses: pd.DataFrame,
    lines: pd.DataFrame,
    output_path: str,
    title: str,
    colorbar_label: str,
    minimum_voltage: float = 0.0,
    cmap_name: str = "YlOrRd",
    log_scale: bool = False,
    colorbar_vmin: float | None = None,
    scale_linewidth: bool = True,
    linewidth_range: tuple[float, float] = (0.8, 3.2),
    highlight_kupferzell: bool = True,
    kupferzell_line_ids: pd.Index | None = None,
    fixed_extent: tuple[float, float, float, float] | None = None,
    show_all_network_lines: bool = False,
) -> None:
    """Plot a line-level metric on a simple network map.

    Parameters
    ----------
    values
        Per-line scalar (indexed by line id).  Non-positive / NaN values are
        treated as "uncongested" and drawn in the neutral base colour.
    buses, lines
        ``pypsa.Network`` component frames (``.buses``, ``.lines``).
    output_path
        Destination PNG path.
    title, colorbar_label
        Figure title and colourbar label respectively.
    minimum_voltage
        Drop any line whose ``v_nom`` is below this threshold (kV).
        Set to 0 to disable voltage filtering.
    cmap_name
        Matplotlib colormap name.  Defaults to ``YlOrRd`` -- a yellow ->
        orange -> red gradient -- which is the standard severity cue used
        in the paper.
    log_scale
        If ``True`` and there are at least two distinct positive values,
        a ``LogNorm`` is used for colour and linewidth scaling.  This is
        particularly useful for occurrence (hours) and cost magnitudes
        where a long tail dominates the distribution.
    colorbar_vmin
        Optional explicit minimum for the colorbar range (linear scaling only).
    scale_linewidth
        If ``True`` (default), line thickness scales with the metric as a
        secondary cue.
    linewidth_range
        ``(min, max)`` line widths used for the thickness scaling.
    highlight_kupferzell
        Draw the GridBooster site as a distinctive marker.
    """
    if (values.empty and not show_all_network_lines) or buses.empty or lines.empty:
        _empty_map(output_path, "No line data available")
        return

    eligible = _filter_eligible_lines(values, buses, lines, minimum_voltage, all_lines=show_all_network_lines)
    if eligible.empty:
        _empty_map(output_path, "No high-voltage lines to plot")
        return

    metric = values.reindex(eligible.index).fillna(0.0).astype(float)
    segments = [
        [(row.x0, row.y0), (row.x1, row.y1)]
        for row in eligible.itertuples(index=False)
    ]

    fig, ax = plt.subplots(figsize=(9, 8))

    kup_set = set(kupferzell_line_ids) if (kupferzell_line_ids is not None and len(kupferzell_line_ids) > 0) else set()

    # Base network: Kupferzell lines drawn bolder, rest thin — same black colour.
    if kup_set:
        reg_segs = [seg for seg, lid in zip(segments, eligible.index) if lid not in kup_set]
        kup_segs = [seg for seg, lid in zip(segments, eligible.index) if lid in kup_set]
        if reg_segs:
            ax.add_collection(LineCollection(reg_segs, colors="black", linewidths=0.5, alpha=0.55, zorder=1))
        if kup_segs:
            ax.add_collection(LineCollection(kup_segs, colors="black", linewidths=2.0, alpha=0.70, zorder=1))
    else:
        ax.add_collection(LineCollection(segments, colors="black", linewidths=0.5, alpha=0.55, zorder=1))

    positive_mask = metric > 0
    if positive_mask.any():
        pos_segments = [seg for seg, keep in zip(segments, positive_mask) if keep]
        pos_values = metric[positive_mask].astype(float)

        vmin_pos = float(pos_values.min())
        vmax_pos = float(pos_values.max())
        vmin = float(vmin_pos if colorbar_vmin is None else colorbar_vmin)
        vmax = float(vmax_pos)
        if log_scale and vmin > 0 and vmax > vmin:
            norm = LogNorm(vmin=vmin, vmax=vmax)
        else:
            norm = Normalize(vmin=vmin, vmax=vmax)
        cmap = plt.get_cmap(cmap_name)

        if scale_linewidth and vmax_pos > vmin_pos:
            lw_min, lw_max = linewidth_range
            if log_scale and vmin_pos > 0:
                log_lo, log_hi = np.log10(vmin_pos), np.log10(vmax_pos)
                scaled = (np.log10(pos_values) - log_lo) / max(log_hi - log_lo, 1e-9)
            else:
                scaled = (pos_values - vmin_pos) / max(vmax_pos - vmin_pos, 1e-9)
            linewidths = lw_min + scaled.to_numpy() * (lw_max - lw_min)
        else:
            linewidths = np.full(len(pos_values), float(linewidth_range[1]))

        # Boost linewidth for Kupferzell lines — colour stays unchanged.
        if kup_set:
            pos_ids = eligible.index[positive_mask.values]
            linewidths = np.where(
                [lid in kup_set for lid in pos_ids],
                linewidths * 2.5,
                linewidths,
            )

        lc = LineCollection(
            pos_segments,
            cmap=cmap,
            norm=norm,
            linewidths=linewidths,
            zorder=3,
        )
        lc.set_array(pos_values.to_numpy())
        ax.add_collection(lc)
        cbar = fig.colorbar(lc, ax=ax, shrink=0.82, pad=0.02)
        cbar.set_label(colorbar_label)

    # Lightweight node dots so single-ended features are discernible.
    if not buses.empty and {"x", "y"}.issubset(buses.columns):
        ax.scatter(
            buses["x"],
            buses["y"],
            s=2.5,
            color="#2f2f2f",
            alpha=0.35,
            zorder=2,
        )

    if highlight_kupferzell:
        _highlight_kupferzell(ax)

    if highlight_kupferzell or kup_set:
        handles, _ = ax.get_legend_handles_labels()
        if kup_set:
            handles.append(Line2D([0], [0], color="black", linewidth=2.5,
                                  label="Kupferzell-connected lines"))
        ax.legend(handles=handles, loc="upper right", fontsize=8, frameon=True)

    if fixed_extent is not None:
        ax.set_xlim(fixed_extent[0], fixed_extent[1])
        ax.set_ylim(fixed_extent[2], fixed_extent[3])
    else:
        _apply_map_extent(ax, buses)
    _draw_background_overlays(ax, buses)
    ax.set_title(title)
    ax.set_xlabel("Longitude [deg]")
    ax.set_ylabel("Latitude [deg]")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, linestyle=":", linewidth=0.3, alpha=0.5)
    fig.tight_layout()
    plt.savefig(output_path, dpi=180)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Public map plots
# ---------------------------------------------------------------------------
def plot_congestion_severity_map(
    summary: pd.DataFrame,
    buses: pd.DataFrame,
    lines: pd.DataFrame,
    output_path: str,
    minimum_voltage: float = 0.0,
    log_scale: bool = True,
    colorbar_vmin: float | None = None,
    kupferzell_line_ids: pd.Index | None = None,
    fixed_extent: tuple[float, float, float, float] | None = None,
    show_all_network_lines: bool = False,
) -> None:
    """Plot line congestion severity (hours) on a simple network map."""
    if summary.empty or "congested_hours" not in summary.columns:
        values = pd.Series(dtype=float)
    else:
        values = summary["congested_hours"].astype(float)

    _plot_line_metric_map(
        values=values,
        buses=buses,
        lines=lines,
        output_path=output_path,
        title="High-voltage line congestion occurrence",
        colorbar_label="Congested hours per year",
        minimum_voltage=minimum_voltage,
        cmap_name="YlOrRd",
        log_scale=log_scale,
        colorbar_vmin=colorbar_vmin,
        kupferzell_line_ids=kupferzell_line_ids,
        fixed_extent=fixed_extent,
        show_all_network_lines=show_all_network_lines,
    )


# Alias kept for semantic clarity in publication scripts.  Occurrence and
# severity are the same quantity here (hours/year a line is at its limit).
plot_congestion_occurrence_map = plot_congestion_severity_map


def plot_congestion_alleviation_map(
    line_saved_cost_eur: pd.Series,
    buses: pd.DataFrame,
    lines: pd.DataFrame,
    output_path: str,
    minimum_voltage: float = 0.0,
    log_scale: bool = True,
    kupferzell_line_ids: pd.Index | None = None,
) -> None:
    """Plot saved congestion cost per line on a Kupferzell-area zoomed map.

    All lines in the network (filtered by voltage) are drawn so the local
    context is visible.  Lines with zero alleviation appear as a thin black
    backdrop; alleviated lines are coloured with a yellow-to-green (YlGn)
    palette that signals a positive outcome.
    """
    _plot_line_metric_map(
        values=line_saved_cost_eur,
        buses=buses,
        lines=lines,
        output_path=output_path,
        title="High-voltage line congestion cost alleviation",
        colorbar_label="Saved congestion cost [EUR / yr]",
        minimum_voltage=minimum_voltage,
        cmap_name="YlGn",
        log_scale=log_scale,
        kupferzell_line_ids=kupferzell_line_ids,
        fixed_extent=ALLEVIATION_MAP_EXTENT,
        show_all_network_lines=True,
    )


def plot_kupferzell_zoomed_network_map(
    buses: pd.DataFrame,
    lines: pd.DataFrame,
    output_path: str,
    kupferzell_line_ids: pd.Index | None = None,
    minimum_voltage: float = 0.0,
) -> None:
    """Zoomed map of the Kupferzell study area with per-line ID labels.

    Draws all lines whose both endpoints lie within KUPFERZELL_ZOOM_EXTENT
    (lon 6–12, lat 46–52). Target lines are highlighted in bold blue; every
    visible line is annotated with its network ID at its midpoint.
    """
    lon_min, lon_max, lat_min, lat_max = KUPFERZELL_ZOOM_EXTENT

    eligible = _filter_eligible_lines(
        pd.Series(dtype=float),
        buses=buses,
        lines=lines,
        minimum_voltage=minimum_voltage,
        all_lines=True,
    )
    if eligible.empty:
        _empty_map(output_path, "No line data available")
        return

    # Keep lines whose both endpoints fall inside the zoom box.
    in_extent = (
        eligible["x0"].between(lon_min, lon_max)
        & eligible["y0"].between(lat_min, lat_max)
        & eligible["x1"].between(lon_min, lon_max)
        & eligible["y1"].between(lat_min, lat_max)
    )
    visible = eligible[in_extent]
    if visible.empty:
        _empty_map(output_path, "No lines in zoom extent")
        return

    kup_set = (
        set(kupferzell_line_ids)
        if (kupferzell_line_ids is not None and len(kupferzell_line_ids) > 0)
        else set()
    )
    visible_ids = visible.index.tolist()
    visible_segs = [
        [(r.x0, r.y0), (r.x1, r.y1)] for r in visible.itertuples(index=False)
    ]

    fig, ax = plt.subplots(figsize=(10, 9))

    reg_segs = [seg for seg, lid in zip(visible_segs, visible_ids) if lid not in kup_set]
    kup_segs = [seg for seg, lid in zip(visible_segs, visible_ids) if lid in kup_set]

    if reg_segs:
        ax.add_collection(LineCollection(reg_segs, colors="black", linewidths=0.8, alpha=0.5, zorder=1))
    if kup_segs:
        ax.add_collection(LineCollection(kup_segs, colors="#1f77b4", linewidths=2.5, alpha=0.9, zorder=2))

    # Nodes within the zoom extent.
    bx = pd.to_numeric(buses.get("x", pd.Series(dtype=float)), errors="coerce")
    by = pd.to_numeric(buses.get("y", pd.Series(dtype=float)), errors="coerce")
    visible_buses = buses[(bx.between(lon_min, lon_max)) & (by.between(lat_min, lat_max))]
    if not visible_buses.empty:
        ax.scatter(
            bx.reindex(visible_buses.index),
            by.reindex(visible_buses.index),
            s=8, color="#2f2f2f", alpha=0.6, zorder=3,
        )

    # Annotate every visible line with its network ID at the line midpoint.
    for seg, lid in zip(visible_segs, visible_ids):
        mid_x = (seg[0][0] + seg[1][0]) / 2.0
        mid_y = (seg[0][1] + seg[1][1]) / 2.0
        ax.annotate(
            str(lid),
            xy=(mid_x, mid_y),
            fontsize=4.5,
            ha="center",
            va="center",
            color="#1f77b4" if lid in kup_set else "#444444",
            zorder=5,
            clip_on=True,
        )

    _highlight_kupferzell(ax)

    # Legend
    legend_handles, _ = ax.get_legend_handles_labels()
    legend_handles.append(Line2D([0], [0], color="black", linewidth=0.8, label="Network lines"))
    if kup_set:
        legend_handles.append(
            Line2D([0], [0], color="#1f77b4", linewidth=2.5,
                   label="Kupferzell GridBooster target lines")
        )
    ax.legend(handles=legend_handles, loc="upper right", fontsize=7, frameon=True)

    ax.set_xlim(lon_min, lon_max)
    ax.set_ylim(lat_min, lat_max)
    _draw_background_overlays(ax, buses)
    ax.set_title("Kupferzell study area — network map with line IDs")
    ax.set_xlabel("Longitude [deg]")
    ax.set_ylabel("Latitude [deg]")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, linestyle=":", linewidth=0.3, alpha=0.5)
    fig.tight_layout()
    plt.savefig(output_path, dpi=180)
    plt.close(fig)


def plot_average_load_map(
    buses: pd.DataFrame,
    load_per_bus_mw: pd.Series,
    lines: pd.DataFrame,
    output_path: str,
    minimum_voltage: float = 0.0,
    cmap_name: str = "viridis",
    title: str = "Average bus load",
    colorbar_label: str = "Mean load [MW]",
    size_range: tuple[float, float] = (8.0, 120.0),
    normalization_country: str | None = "DE",
    kupferzell_line_ids: pd.Index | None = None,
) -> None:
    """Plot mean load per bus as coloured / sized circles on a network map.

    The transmission network is drawn as a thin grey backdrop so the
    spatial context matches the congestion maps exactly.  The Kupferzell
    site is always highlighted.

    Parameters
    ----------
    buses
        ``pypsa.Network.buses`` (must contain x, y columns).
    load_per_bus_mw
        Per-bus mean load in MW, indexed by bus id.
    lines
        ``pypsa.Network.lines`` (used only for the backdrop).
    output_path
        Destination PNG path.
    minimum_voltage
        Drop backdrop lines below this kV threshold.  0 disables.
    cmap_name
        Colormap for the load magnitude.  Defaults to ``viridis`` which
        contrasts well against the yellow/orange/red severity maps.
    size_range
        ``(min_marker, max_marker)`` circle sizes (matplotlib ``s``).
    normalization_country
        Country code used for colormap normalization (default ``DE``).  The
        color range is derived from positive-load buses in this country, while
        all buses are still plotted.  Buses above that range saturate at the
        maximum color.
    """
    if buses.empty or not {"x", "y"}.issubset(buses.columns):
        _empty_map(output_path, "No bus coordinates available")
        return

    load = pd.to_numeric(load_per_bus_mw, errors="coerce").reindex(buses.index).fillna(0.0)

    # ---- Backdrop network -------------------------------------------------
    fig, ax = plt.subplots(figsize=(9, 8))
    kup_set = set(kupferzell_line_ids) if (kupferzell_line_ids is not None and len(kupferzell_line_ids) > 0) else set()
    if not lines.empty and {"bus0", "bus1"}.issubset(lines.columns):
        backdrop_values = pd.Series(1.0, index=lines.index)
        backdrop = _filter_eligible_lines(backdrop_values, buses, lines, minimum_voltage)
        if not backdrop.empty:
            all_segs = [
                [(r.x0, r.y0), (r.x1, r.y1)] for r in backdrop.itertuples(index=False)
            ]
            if kup_set:
                reg_segs = [seg for seg, lid in zip(all_segs, backdrop.index) if lid not in kup_set]
                kup_segs = [seg for seg, lid in zip(all_segs, backdrop.index) if lid in kup_set]
                if reg_segs:
                    ax.add_collection(LineCollection(reg_segs, colors="black", linewidths=0.4, alpha=0.55, zorder=1))
                if kup_segs:
                    ax.add_collection(LineCollection(kup_segs, colors="black", linewidths=1.8, alpha=0.75, zorder=1))
            else:
                ax.add_collection(LineCollection(all_segs, colors="black", linewidths=0.4, alpha=0.55, zorder=1))

    # ---- Bus load scatter -------------------------------------------------
    positive = load > 0
    if positive.any():
        ref_values = load[positive]
        if normalization_country:
            country_codes = _bus_country_codes(buses)
            country_mask = country_codes.eq(str(normalization_country).upper())
            ref_values_country = load[positive & country_mask]
            if not ref_values_country.empty:
                ref_values = ref_values_country

        lo = float(ref_values.min())
        hi = float(ref_values.max())
        s_min, s_max = size_range
        if hi > lo:
            scaled = (load[positive] - lo) / (hi - lo)
            sizes = s_min + scaled * (s_max - s_min)
        else:
            sizes = pd.Series(s_max, index=load[positive].index)

        color_values = load[positive].clip(lower=lo, upper=hi)

        sc = ax.scatter(
            buses.loc[positive, "x"],
            buses.loc[positive, "y"],
            c=color_values,
            s=sizes,
            cmap=cmap_name,
            vmin=lo,
            vmax=hi,
            edgecolor="white",
            linewidth=0.4,
            alpha=0.9,
            zorder=3,
        )
        cbar = fig.colorbar(sc, ax=ax, shrink=0.82, pad=0.02)
        cbar.set_label(colorbar_label)

    # Zero-load buses as tiny grey dots so coverage is visible.
    zero = ~positive
    if zero.any():
        ax.scatter(
            buses.loc[zero, "x"],
            buses.loc[zero, "y"],
            s=3,
            color="#808080",
            alpha=0.4,
            zorder=2,
        )

    _highlight_kupferzell(ax)
    handles, _ = ax.get_legend_handles_labels()
    if kup_set:
        handles.append(Line2D([0], [0], color="black", linewidth=2.5,
                               label="Kupferzell-connected lines"))
    ax.legend(handles=handles, loc="upper right", fontsize=8, frameon=True)

    _apply_map_extent(ax, buses)
    _draw_background_overlays(ax, buses)
    ax.set_title(title)
    ax.set_xlabel("Longitude [deg]")
    ax.set_ylabel("Latitude [deg]")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, linestyle=":", linewidth=0.3, alpha=0.5)
    fig.tight_layout()
    plt.savefig(output_path, dpi=180)
    plt.close(fig)


def plot_average_line_loading_map(
    line_loading_pu: pd.Series,
    buses: pd.DataFrame,
    lines: pd.DataFrame,
    output_path: str,
    minimum_voltage: float = 0.0,
    cmap_name: str = "viridis",
    title: str = "Average line power loading",
    colorbar_label: str = "Mean line loading [pu]",
    log_scale: bool = False,
    kupferzell_line_ids: pd.Index | None = None,
    linewidth_range: tuple[float, float] = (0.8, 3.2),
) -> None:
    """Plot mean line loading on the same map layout as the congestion maps."""
    _plot_line_metric_map(
        values=line_loading_pu,
        buses=buses,
        lines=lines,
        output_path=output_path,
        title=title,
        colorbar_label=colorbar_label,
        minimum_voltage=minimum_voltage,
        cmap_name=cmap_name,
        log_scale=log_scale,
        kupferzell_line_ids=kupferzell_line_ids,
        linewidth_range=linewidth_range,
    )


# ---------------------------------------------------------------------------
# Convenience helper -- useful when callers already have a Network at hand
# ---------------------------------------------------------------------------
def plot_average_load_map_from_network(
    n,
    output_path: str,
    minimum_voltage: float = 0.0,
    normalization_country: str | None = "DE",
    kupferzell_line_ids: pd.Index | None = None,
) -> None:
    """Compute the mean load per bus from ``n`` and delegate to ``plot_average_load_map``.

    ``n`` is a ``pypsa.Network``.  This helper hides the boilerplate of
    summing ``n.loads_t.p_set`` / ``n.loads_t.p`` across carriers and
    mapping the per-load series onto buses via the ``bus`` column of
    ``n.loads``.
    """
    if not hasattr(n, "loads_t") or not hasattr(n, "loads"):
        _empty_map(output_path, "Network has no load components")
        return

    # Prefer realised load (``p``) if the LOPF wrote it, fall back to set-point.
    p_t = getattr(n.loads_t, "p", None)
    if p_t is None or p_t.empty:
        p_t = getattr(n.loads_t, "p_set", None)
    if p_t is None or p_t.empty:
        _empty_map(output_path, "No hourly load data available")
        return

    mean_per_load = p_t.mean(axis=0)
    load_bus_map = n.loads["bus"].reindex(mean_per_load.index)
    load_per_bus = (
        pd.DataFrame({"bus": load_bus_map, "mw": mean_per_load.values})
        .groupby("bus")["mw"]
        .sum()
    )

    de_mask = _bus_country_codes(n.buses).eq("DE")
    de_buses = n.buses[de_mask]
    de_lines = n.lines[n.lines["bus0"].isin(de_buses.index) | n.lines["bus1"].isin(de_buses.index)]
    load_per_bus = load_per_bus.reindex(de_buses.index)

    plot_average_load_map(
        buses=de_buses,
        load_per_bus_mw=load_per_bus,
        lines=de_lines,
        output_path=output_path,
        minimum_voltage=minimum_voltage,
        normalization_country=normalization_country,
        kupferzell_line_ids=kupferzell_line_ids,
    )


def plot_average_line_loading_map_from_network(
    n,
    output_path: str,
    minimum_voltage: float = 0.0,
    kupferzell_line_ids: pd.Index | None = None,
) -> None:
    """Compute the mean line loading from ``n`` and delegate to ``plot_average_line_loading_map``."""
    if not hasattr(n, "lines_t") or not hasattr(n, "lines"):
        _empty_map(output_path, "Network has no line components")
        return

    p0_t = getattr(n.lines_t, "p0", None)
    if p0_t is None or p0_t.empty:
        _empty_map(output_path, "No hourly line-flow data available")
        return

    line_loading = p0_t.abs().div(n.lines["s_nom"], axis=1).mean(axis=0)

    de_mask = _bus_country_codes(n.buses).eq("DE")
    de_buses = n.buses[de_mask]
    de_lines = n.lines[n.lines["bus0"].isin(de_buses.index) | n.lines["bus1"].isin(de_buses.index)]
    line_loading = line_loading.reindex(de_lines.index)

    plot_average_line_loading_map(
        line_loading_pu=line_loading,
        buses=de_buses,
        lines=de_lines,
        output_path=output_path,
        minimum_voltage=minimum_voltage,
        kupferzell_line_ids=kupferzell_line_ids,
    )

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

ALLOCATION_METHOD_LABELS = {
    "temporal": "Temporal",
    "tso_priority": "TSO priority",
    "optimal_revenue": "Optimal revenue",
}

ALLOCATION_METHOD_ORDER = ["temporal", "tso_priority", "optimal_revenue"]

ALLEVIATION_METHOD_LABELS = {
    "flat_one_line": "Flat one-line",
    "dynamic_one_line": "Dynamic one-line",
    "dynamic_multiple_lines": "Dynamic multiple-lines",
    # Legacy aliases for existing result files.
    "simple": "Flat one-line",
    "one_line": "Dynamic one-line",
    "optimal": "Dynamic multiple-lines",
    "optimal_alleviation": "Dynamic multiple-lines",
}

ALLEVIATION_METHOD_ALIASES = {
    "flat_one_line": "flat_one_line",
    "simple": "flat_one_line",
    "dynamic_one_line": "dynamic_one_line",
    "one_line": "dynamic_one_line",
    "dynamic_multiple_lines": "dynamic_multiple_lines",
    "optimal": "dynamic_multiple_lines",
    "optimal_alleviation": "dynamic_multiple_lines",
}

ALLEVIATION_COLUMN_CANDIDATES = {
    "flat_one_line": [
        "congestion_relief_flat_one_line_eur",
        "congestion_relief_simple_eur",
    ],
    "dynamic_one_line": [
        "congestion_relief_dynamic_one_line_eur",
        "congestion_relief_one_line_eur",
    ],
    "dynamic_multiple_lines": [
        "congestion_relief_dynamic_multiple_lines_eur",
        "congestion_relief_optimal_eur",
    ],
}


def _canonical_alleviation_method(method: str | None) -> str | None:
    if method is None:
        return None
    return ALLEVIATION_METHOD_ALIASES.get(method, method)


def _first_existing_column(df: pd.DataFrame, candidates: list[str]) -> str:
    for col in candidates:
        if col in df.columns:
            return col
    raise ValueError(f"None of {candidates} found in columns: {list(df.columns)}")


def _apply_publication_bar_style(ax: plt.Axes, ylabel: str) -> None:
    ax.set_ylabel(ylabel)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#d8d8d8", linestyle="-", linewidth=0.6, alpha=0.75)
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", labelsize=9)


def _save_publication_figure(fig: plt.Figure, output_path: str | Path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def _eur_millions_formatter(x: float, _pos: int) -> str:
    return f"{x / 1e6:.1f}"


def _add_bar_labels(ax: plt.Axes, bars, scale: float = 1.0, fmt: str = "{:.1f}") -> None:
    ymax = max([abs(bar.get_height()) for bar in bars] + [1.0])
    offset = ymax * 0.015
    for bar in bars:
        height = bar.get_height()
        va = "bottom" if height >= 0 else "top"
        y = height + offset if height >= 0 else height - offset
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            y,
            fmt.format(height / scale),
            ha="center",
            va=va,
            fontsize=8,
            rotation=0,
            clip_on=False,
        )
    ymin, ymax_current = ax.get_ylim()
    if ymax_current >= 0:
        ax.set_ylim(ymin, max(ymax_current, ymax * 1.14))


def _add_grouped_bar_labels(
    ax: plt.Axes,
    containers,
    scale: float = 1.0,
    fmt: str = "{:.1f}",
    fontsize: float = 6.5,
) -> None:
    all_bars = [bar for container in containers for bar in container]
    ymax = max([abs(bar.get_height()) for bar in all_bars] + [1.0])
    offset = ymax * 0.015
    for bar in all_bars:
        height = bar.get_height()
        if abs(height) < 1e-9:
            continue
        va = "bottom" if height >= 0 else "top"
        y = height + offset if height >= 0 else height - offset
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            y,
            fmt.format(height / scale),
            ha="center",
            va=va,
            fontsize=fontsize,
            rotation=90,
            clip_on=False,
        )
    ymin, ymax_current = ax.get_ylim()
    if ymax_current >= 0:
        ax.set_ylim(ymin, max(ymax_current, ymax * 1.18))


def _read_time_indexed_csv(csv_path: str | Path, year: int | None = None) -> pd.DataFrame:
    csv_path = Path(csv_path)
    df = pd.read_csv(csv_path)
    ts_col = next(
        (c for c in df.columns if str(c).strip().lower() in ("time_cet", "timestamp", "time")),
        None,
    )
    if ts_col is None:
        raise ValueError(f"No timestamp column found in {csv_path}.")
    df[ts_col] = pd.to_datetime(df[ts_col])
    df = df.set_index(ts_col).sort_index()
    if year is not None:
        df = df.loc[df.index.year == year]
    return df


def _simple_alleviation_months_from_merged(merged_csv_path: str | Path, year: int) -> list[int]:
    merged = _read_time_indexed_csv(merged_csv_path, year)
    try:
        col = _first_existing_column(merged, ALLEVIATION_COLUMN_CANDIDATES["flat_one_line"])
    except ValueError:
        return []
    active = pd.to_numeric(merged[col], errors="coerce").fillna(0.0) > 0.0
    months = sorted(merged.index[active].month.unique().tolist())
    return months[:1]


def _scope_simple_alleviation_to_first_month(df: pd.DataFrame) -> pd.DataFrame:
    try:
        col = _first_existing_column(df, ALLEVIATION_COLUMN_CANDIDATES["flat_one_line"])
    except ValueError:
        return df
    active = pd.to_numeric(df[col], errors="coerce").fillna(0.0) > 0.0
    months = sorted(df.index[active].month.unique().tolist())
    if not months:
        return df
    out = df.copy()
    out.loc[out.index.month != months[0], col] = 0.0
    return out


def _simple_alleviation_months_from_final_dir(final_allocation_dir: Path, year: int) -> list[int]:
    merged_csv = (
        final_allocation_dir.parent
        / "congestion_alleviation"
        / f"alleviation_revenues_merged_{year}.csv"
    )
    if not merged_csv.exists():
        return []
    return _simple_alleviation_months_from_merged(merged_csv, year)


def _filter_outputs_to_months(
    outputs: dict[str, pd.DataFrame],
    months: list[int],
) -> dict[str, pd.DataFrame]:
    if not months:
        return outputs
    keep = set(months)
    return {
        method: df.loc[df.index.month.isin(keep)].copy()
        for method, df in outputs.items()
    }


def plot_total_revenue_bar(merged_csv_path: str, output_path: str) -> None:
    """Publication-style annual congestion-relief revenue by alleviation method."""
    df = _scope_simple_alleviation_to_first_month(_read_time_indexed_csv(merged_csv_path))
    mode_keys = ["flat_one_line", "dynamic_one_line", "dynamic_multiple_lines"]
    mode_cols = [_first_existing_column(df, ALLEVIATION_COLUMN_CANDIDATES[key]) for key in mode_keys]
    mode_labels = [ALLEVIATION_METHOD_LABELS[key] for key in mode_keys]
    totals = pd.Series({label: df[col].sum() for label, col in zip(mode_labels, mode_cols)})

    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    bars = ax.bar(
        totals.index,
        totals.values,
        color=["#4C78A8", "#F58518", "#54A24B"],
        edgecolor="#222222",
        linewidth=0.6,
    )
    _apply_publication_bar_style(ax, "Annual congestion relief [million EUR]")
    ax.yaxis.set_major_formatter(FuncFormatter(_eur_millions_formatter))
    ax.set_title("Annual congestion-relief value by alleviation method", fontsize=11, pad=10)
    _add_bar_labels(ax, bars, scale=1e6, fmt="{:.1f}")
    _save_publication_figure(fig, output_path)


def plot_monthly_revenue_grouped_bar(merged_csv_path: str, output_path: str) -> None:
    """Publication-style monthly congestion-relief revenue by alleviation method."""
    df = _scope_simple_alleviation_to_first_month(_read_time_indexed_csv(merged_csv_path))
    mode_keys = ["flat_one_line", "dynamic_one_line", "dynamic_multiple_lines"]
    mode_cols = [_first_existing_column(df, ALLEVIATION_COLUMN_CANDIDATES[key]) for key in mode_keys]
    mode_labels = [ALLEVIATION_METHOD_LABELS[key] for key in mode_keys]
    monthly = df.groupby(df.index.month)[mode_cols].sum().reindex(range(1, 13), fill_value=0.0)

    x = np.arange(12)
    width = 0.24
    fig, ax = plt.subplots(figsize=(9.2, 4.6))
    colors = ["#4C78A8", "#F58518", "#54A24B"]
    containers = []
    for i, (col, label, color) in enumerate(zip(mode_cols, mode_labels, colors)):
        bars = ax.bar(
            x + (i - 1) * width,
            monthly[col].values,
            width,
            label=label,
            color=color,
            edgecolor="#222222",
            linewidth=0.4,
        )
        containers.append(bars)
    _apply_publication_bar_style(ax, "Monthly congestion relief [million EUR]")
    ax.yaxis.set_major_formatter(FuncFormatter(_eur_millions_formatter))
    ax.set_xticks(x)
    ax.set_xticklabels(MONTH_ABBR)
    ax.set_title("Monthly congestion-relief value by alleviation method", fontsize=11, pad=10)
    ax.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.12))
    _add_grouped_bar_labels(ax, containers, scale=1e6, fmt="{:.1f}", fontsize=6.0)
    _save_publication_figure(fig, output_path)


def _merchant_file_label(path: Path) -> str:
    stem = path.stem
    if "unconstrained" in stem:
        return "Unconstrained"
    if "tso_constrained_flat_one_line" in stem or "tso_constrained_simple" in stem:
        return "TSO-constrained, flat one-line"
    if "tso_constrained_dynamic_one_line" in stem or "tso_constrained_one_line" in stem:
        return "TSO-constrained, dynamic one-line"
    if "tso_constrained_dynamic_multiple_lines" in stem or "tso_constrained_optimal" in stem:
        return "TSO-constrained, dynamic multiple-lines"
    return stem.replace("dam_merchant_revenues_", "").replace("_", " ").title()


def _merchant_net_revenue(df: pd.DataFrame) -> pd.Series:
    revenue_raw = df["hourly_revenue_eur"] if "hourly_revenue_eur" in df.columns else pd.Series(0.0, index=df.index)
    oc_raw = df["hourly_oc_cost_eur"] if "hourly_oc_cost_eur" in df.columns else pd.Series(0.0, index=df.index)
    revenue = pd.to_numeric(revenue_raw, errors="coerce").fillna(0.0)
    oc_cost = pd.to_numeric(oc_raw, errors="coerce").fillna(0.0)
    return revenue - oc_cost


def _merchant_allowed_hours(df: pd.DataFrame) -> pd.Series:
    if "tso_locked" not in df.columns:
        return pd.Series(1, index=df.index, dtype=int)
    locked = df["tso_locked"]
    if locked.dtype == bool:
        tso_locked = locked.fillna(False)
    else:
        tso_locked = locked.astype(str).str.strip().str.lower().isin({"true", "1", "yes"})
    return (~tso_locked).astype(int)


def plot_merchant_revenue_bars(
    merchant_dir: str | Path,
    year: int,
    annual_output_path: str | Path | None = None,
    monthly_output_path: str | Path | None = None,
) -> tuple[Path, Path]:
    """Plot annual and monthly net merchant revenues from all merchant CSVs in a folder."""
    merchant_dir = Path(merchant_dir)
    csvs = sorted(merchant_dir.glob(f"dam_merchant_revenues_*_{year}.csv"))
    if not csvs:
        raise FileNotFoundError(f"No merchant revenue CSVs found in {merchant_dir} for {year}.")

    labels: list[str] = []
    annual_values: list[float] = []
    monthly_parts: list[pd.Series] = []
    for csv in csvs:
        df = _read_time_indexed_csv(csv, year)
        labels.append(_merchant_file_label(csv))
        net = _merchant_net_revenue(df)
        annual_values.append(float(net.sum()))
        monthly_parts.append(net.groupby(net.index.month).sum().reindex(range(1, 13), fill_value=0.0))

    annual_output = Path(annual_output_path) if annual_output_path else merchant_dir / f"figure_merchant_revenues_annual_{year}.png"
    monthly_output = Path(monthly_output_path) if monthly_output_path else merchant_dir / f"figure_merchant_revenues_monthly_{year}.png"

    fig, ax = plt.subplots(figsize=(8.0, 4.4))
    colors = ["#4C78A8", "#72B7B2", "#F58518", "#54A24B"][:len(labels)]
    bars = ax.bar(labels, annual_values, color=colors, edgecolor="#222222", linewidth=0.6)
    _apply_publication_bar_style(ax, "Annual net merchant revenue [million EUR]")
    ax.yaxis.set_major_formatter(FuncFormatter(_eur_millions_formatter))
    ax.set_title("Annual day-ahead merchant value by operating regime", fontsize=11, pad=10)
    ax.tick_params(axis="x", rotation=20)
    _add_bar_labels(ax, bars, scale=1e6, fmt="{:.1f}")
    _save_publication_figure(fig, annual_output)

    monthly = pd.DataFrame({label: s for label, s in zip(labels, monthly_parts)})
    x = np.arange(12)
    width = min(0.78 / max(len(labels), 1), 0.24)
    fig, ax = plt.subplots(figsize=(10.2, 4.8))
    offsets = (np.arange(len(labels)) - (len(labels) - 1) / 2) * width
    containers = []
    for i, label in enumerate(labels):
        bars = ax.bar(
            x + offsets[i],
            monthly[label].values,
            width,
            label=label,
            color=colors[i],
            edgecolor="#222222",
            linewidth=0.35,
        )
        containers.append(bars)
    _apply_publication_bar_style(ax, "Monthly net merchant revenue [million EUR]")
    ax.yaxis.set_major_formatter(FuncFormatter(_eur_millions_formatter))
    ax.set_xticks(x)
    ax.set_xticklabels(MONTH_ABBR)
    ax.set_title("Monthly day-ahead merchant value by operating regime", fontsize=11, pad=10)
    ax.legend(frameon=False, ncol=2, loc="upper center", bbox_to_anchor=(0.5, 1.18), fontsize=8)
    _add_grouped_bar_labels(ax, containers, scale=1e6, fmt="{:.1f}", fontsize=5.8)
    _save_publication_figure(fig, monthly_output)
    return annual_output, monthly_output


def plot_merchant_hour_bars(
    merchant_dir: str | Path,
    year: int,
    annual_output_path: str | Path | None = None,
    monthly_output_path: str | Path | None = None,
) -> tuple[Path, Path]:
    """Plot annual and monthly merchant-allowed hours from all merchant CSVs in a folder."""
    merchant_dir = Path(merchant_dir)
    csvs = sorted(merchant_dir.glob(f"dam_merchant_revenues_*_{year}.csv"))
    if not csvs:
        raise FileNotFoundError(f"No merchant revenue CSVs found in {merchant_dir} for {year}.")

    labels: list[str] = []
    annual_values: list[float] = []
    monthly_parts: list[pd.Series] = []
    for csv in csvs:
        df = _read_time_indexed_csv(csv, year)
        labels.append(_merchant_file_label(csv))
        hours = _merchant_allowed_hours(df)
        annual_values.append(float(hours.sum()))
        monthly_parts.append(hours.groupby(hours.index.month).sum().reindex(range(1, 13), fill_value=0.0))

    annual_output = Path(annual_output_path) if annual_output_path else merchant_dir / f"figure_merchant_hours_annual_{year}.png"
    monthly_output = Path(monthly_output_path) if monthly_output_path else merchant_dir / f"figure_merchant_hours_monthly_{year}.png"

    fig, ax = plt.subplots(figsize=(8.0, 4.4))
    colors = ["#4C78A8", "#72B7B2", "#F58518", "#54A24B"][:len(labels)]
    bars = ax.bar(labels, annual_values, color=colors, edgecolor="#222222", linewidth=0.6)
    _apply_publication_bar_style(ax, "Annual merchant operation [h]")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_title("Annual merchant-operation hours by operating regime", fontsize=11, pad=10)
    ax.tick_params(axis="x", rotation=20)
    _add_bar_labels(ax, bars, scale=1.0, fmt="{:.0f}")
    _save_publication_figure(fig, annual_output)

    monthly = pd.DataFrame({label: s for label, s in zip(labels, monthly_parts)})
    x = np.arange(12)
    width = min(0.78 / max(len(labels), 1), 0.24)
    fig, ax = plt.subplots(figsize=(10.2, 4.8))
    offsets = (np.arange(len(labels)) - (len(labels) - 1) / 2) * width
    containers = []
    for i, label in enumerate(labels):
        bars = ax.bar(
            x + offsets[i],
            monthly[label].values,
            width,
            label=label,
            color=colors[i],
            edgecolor="#222222",
            linewidth=0.35,
        )
        containers.append(bars)
    _apply_publication_bar_style(ax, "Monthly merchant operation [h]")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xticks(x)
    ax.set_xticklabels(MONTH_ABBR)
    ax.set_title("Monthly merchant-operation hours by operating regime", fontsize=11, pad=10)
    ax.legend(frameon=False, ncol=2, loc="upper center", bbox_to_anchor=(0.5, 1.18), fontsize=8)
    _add_grouped_bar_labels(ax, containers, scale=1.0, fmt="{:.0f}", fontsize=5.8)
    _save_publication_figure(fig, monthly_output)
    return annual_output, monthly_output


def _parse_allocation_file(path: Path, year: int) -> tuple[str, str] | None:
    prefix = "allocation_"
    suffix = f"_{year}"
    stem = path.stem
    if stem.endswith("_kpi") or not stem.startswith(prefix) or not stem.endswith(suffix):
        return None
    middle = stem[len(prefix):-len(suffix)]
    for allocation_method in ALLOCATION_METHOD_ORDER:
        marker = f"{allocation_method}_"
        if middle.startswith(marker):
            return allocation_method, middle[len(marker):]
    return None


def _load_allocation_outputs(
    final_allocation_dir: str | Path,
    year: int,
    alleviation_method: str | None = None,
) -> dict[str, pd.DataFrame]:
    final_allocation_dir = Path(final_allocation_dir)
    method = None
    if alleviation_method is not None:
        method = _canonical_alleviation_method(alleviation_method)

    outputs: dict[str, pd.DataFrame] = {}
    for csv in sorted(final_allocation_dir.glob(f"allocation_*_{year}.csv")):
        parsed = _parse_allocation_file(csv, year)
        if parsed is None:
            continue
        allocation_method, file_method = parsed
        if method is not None and _canonical_alleviation_method(file_method) != method:
            continue
        outputs[allocation_method] = _read_time_indexed_csv(csv, year)
    if not outputs:
        raise FileNotFoundError(
            f"No final allocation CSVs found in {final_allocation_dir} for {year}"
            + (f" and alleviation_method={method}." if method else ".")
        )
    return outputs


def plot_final_allocation_bars(
    final_allocation_dir: str | Path,
    year: int,
    alleviation_method: str | None = None,
    allocation_method: str | None = None,
) -> tuple[Path, Path, Path, Path]:
    """Create annual/monthly revenue and annual/monthly hour allocation bar charts."""
    final_allocation_dir = Path(final_allocation_dir)
    method_tag = _canonical_alleviation_method(alleviation_method) or "all"
    method_label = ALLEVIATION_METHOD_LABELS.get(method_tag, str(method_tag).replace("_", " ").title())
    outputs = _load_allocation_outputs(final_allocation_dir, year, method_tag if method_tag != "all" else None)
    simple_months: list[int] = []
    if method_tag == "flat_one_line":
        simple_months = _simple_alleviation_months_from_final_dir(final_allocation_dir, year)
        outputs = _filter_outputs_to_months(outputs, simple_months)

    ordered_methods = [m for m in ALLOCATION_METHOD_ORDER if m in outputs]
    selected_method = allocation_method if allocation_method in outputs else ordered_methods[0]
    selected = outputs[selected_method]
    plot_months = simple_months if simple_months else list(range(1, 13))
    month_labels = [MONTH_ABBR[m - 1] for m in plot_months]
    duration_note = (
        f"Congestion-relief stream: {method_label}; duration: "
        f"{', '.join(month_labels)}"
        if simple_months
        else f"Congestion-relief stream: {method_label}"
    )

    annual_revenue_path = final_allocation_dir / f"figure_allocation_annual_revenue_{method_tag}_{year}.png"
    monthly_revenue_path = final_allocation_dir / f"figure_allocation_monthly_revenue_{selected_method}_{method_tag}_{year}.png"
    annual_hours_path = final_allocation_dir / f"figure_allocation_annual_hours_{method_tag}_{year}.png"
    monthly_hours_path = final_allocation_dir / f"figure_allocation_monthly_hours_{selected_method}_{method_tag}_{year}.png"

    x = np.arange(len(ordered_methods))
    width = 0.34
    congestion = np.array([pd.to_numeric(outputs[m]["congestion_relief_eur"], errors="coerce").fillna(0.0).sum() for m in ordered_methods])
    merchant = np.array([pd.to_numeric(outputs[m]["merchant_revenue_eur"], errors="coerce").fillna(0.0).sum() for m in ordered_methods])

    fig, ax = plt.subplots(figsize=(7.8, 4.6))
    bars_congestion = ax.bar(x - width / 2, congestion, width, label="Congestion relief", color="#4C78A8", edgecolor="#222222", linewidth=0.5)
    bars_merchant = ax.bar(x + width / 2, merchant, width, label="Merchant revenue", color="#F58518", edgecolor="#222222", linewidth=0.5)
    _apply_publication_bar_style(ax, "Annual value [million EUR]")
    ax.yaxis.set_major_formatter(FuncFormatter(_eur_millions_formatter))
    ax.set_xticks(x)
    ax.set_xticklabels([ALLOCATION_METHOD_LABELS[m] for m in ordered_methods])
    ax.set_title("Annual value by allocation method", fontsize=11, pad=10)
    ax.text(0.01, 0.97, duration_note, transform=ax.transAxes,
            ha="left", va="top", fontsize=9, bbox={"facecolor": "white", "edgecolor": "#bdbdbd", "linewidth": 0.6})
    ax.legend(frameon=False, ncol=2, loc="upper center", bbox_to_anchor=(0.5, 1.14))
    _add_grouped_bar_labels(ax, [bars_congestion, bars_merchant], scale=1e6, fmt="{:.1f}", fontsize=7.5)
    _save_publication_figure(fig, annual_revenue_path)

    monthly = selected.groupby(selected.index.month)[["congestion_relief_eur", "merchant_revenue_eur"]].sum().reindex(plot_months, fill_value=0.0)
    x = np.arange(len(plot_months))
    fig, ax = plt.subplots(figsize=(9.4, 4.7))
    bars_congestion = ax.bar(x - width / 2, monthly["congestion_relief_eur"], width, label="Congestion relief", color="#4C78A8", edgecolor="#222222", linewidth=0.4)
    bars_merchant = ax.bar(x + width / 2, monthly["merchant_revenue_eur"], width, label="Merchant revenue", color="#F58518", edgecolor="#222222", linewidth=0.4)
    _apply_publication_bar_style(ax, "Monthly value [million EUR]")
    ax.yaxis.set_major_formatter(FuncFormatter(_eur_millions_formatter))
    ax.set_xticks(x)
    ax.set_xticklabels(month_labels)
    ax.set_title(f"Monthly value distribution: {ALLOCATION_METHOD_LABELS[selected_method]}", fontsize=11, pad=10)
    ax.text(0.01, 0.97, duration_note, transform=ax.transAxes,
            ha="left", va="top", fontsize=9, bbox={"facecolor": "white", "edgecolor": "#bdbdbd", "linewidth": 0.6})
    ax.legend(frameon=False, ncol=2, loc="upper center", bbox_to_anchor=(0.5, 1.14))
    _add_grouped_bar_labels(ax, [bars_congestion, bars_merchant], scale=1e6, fmt="{:.1f}", fontsize=7.5)
    _save_publication_figure(fig, monthly_revenue_path)

    tso_hours = np.array([(outputs[m]["mode"].astype(str) == "tso").sum() for m in ordered_methods], dtype=float)
    merchant_hours = np.array([(outputs[m]["mode"].astype(str) == "merchant").sum() for m in ordered_methods], dtype=float)
    x = np.arange(len(ordered_methods))
    fig, ax = plt.subplots(figsize=(7.8, 4.6))
    bars_tso = ax.bar(x - width / 2, tso_hours, width, label="Congestion relief", color="#4C78A8", edgecolor="#222222", linewidth=0.5)
    bars_merchant = ax.bar(x + width / 2, merchant_hours, width, label="Merchant", color="#F58518", edgecolor="#222222", linewidth=0.5)
    _apply_publication_bar_style(ax, "Allocated hours [h]")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xticks(x)
    ax.set_xticklabels([ALLOCATION_METHOD_LABELS[m] for m in ordered_methods])
    ax.set_title("Annual operating-hour allocation by method", fontsize=11, pad=10)
    ax.text(0.01, 0.97, duration_note, transform=ax.transAxes,
            ha="left", va="top", fontsize=9, bbox={"facecolor": "white", "edgecolor": "#bdbdbd", "linewidth": 0.6})
    ax.legend(frameon=False, ncol=2, loc="upper center", bbox_to_anchor=(0.5, 1.14))
    _add_grouped_bar_labels(ax, [bars_tso, bars_merchant], scale=1.0, fmt="{:.1f}", fontsize=7.5)
    _save_publication_figure(fig, annual_hours_path)

    hour_counts = (
        selected.assign(
            tso=(selected["mode"].astype(str) == "tso").astype(int),
            merchant=(selected["mode"].astype(str) == "merchant").astype(int),
        )
        .groupby(selected.index.month)[["tso", "merchant"]]
        .sum()
        .reindex(plot_months, fill_value=0)
    )
    x = np.arange(len(plot_months))
    fig, ax = plt.subplots(figsize=(9.4, 4.7))
    bars_tso = ax.bar(x - width / 2, hour_counts["tso"], width, label="Congestion relief", color="#4C78A8", edgecolor="#222222", linewidth=0.4)
    bars_merchant = ax.bar(x + width / 2, hour_counts["merchant"], width, label="Merchant", color="#F58518", edgecolor="#222222", linewidth=0.4)
    _apply_publication_bar_style(ax, "Allocated hours [h]")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xticks(x)
    ax.set_xticklabels(month_labels)
    ax.set_title(f"Monthly operating-hour allocation: {ALLOCATION_METHOD_LABELS[selected_method]}", fontsize=11, pad=10)
    ax.text(0.01, 0.97, duration_note, transform=ax.transAxes,
            ha="left", va="top", fontsize=9, bbox={"facecolor": "white", "edgecolor": "#bdbdbd", "linewidth": 0.6})
    ax.legend(frameon=False, ncol=2, loc="upper center", bbox_to_anchor=(0.5, 1.14))
    _add_grouped_bar_labels(ax, [bars_tso, bars_merchant], scale=1.0, fmt="{:.1f}", fontsize=7.5)
    _save_publication_figure(fig, monthly_hours_path)

    return annual_revenue_path, monthly_revenue_path, annual_hours_path, monthly_hours_path
