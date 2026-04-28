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
        if log_scale and vmin_pos > 0 and vmax_pos > vmin_pos:
            norm = LogNorm(vmin=vmin_pos, vmax=vmax_pos)
        else:
            norm = Normalize(vmin=vmin_pos, vmax=vmax_pos)
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
    kupferzell_line_ids: pd.Index | None = None,
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
        kupferzell_line_ids=kupferzell_line_ids,
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




