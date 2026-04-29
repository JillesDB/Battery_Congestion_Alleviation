"""Congestion occurrence post-processing for solved PyPSA networks.

This script was rewritten for the GridBooster paper to support:

1. A richer target-area definition than the original radius-around-Kupferzell
   filter.  The default ``corridor`` mode keeps the N-S corridor feeders
   from Hessen / Thueringen plus the BW-internal lines evacuating load
   onto Stuttgart / Heilbronn / Karlsruhe / Mannheim -- reflecting the
   actual stress pattern the Kupferzell GridBooster was commissioned to
   relieve.  ``kupferzell`` (legacy radius) and ``all`` remain available.

2. A redispatch-style congestion trigger.  The legacy
   ``|p0|/s_nom >= 0.98`` threshold returns zero events under PyPSA-Eur's
   default ``s_max_pu = 0.7`` because the LP physically cannot exceed
   70 % nameplate loading.  When ``s_max_pu`` is raised to 1.0 (see
   ``hpc_mirror/SETUP_GUIDE.md``), two complementary triggers become
   meaningful and the script exposes both:

   * ``loading``             -- classic base-case loading threshold
   * ``n_minus_1``           -- LODF-based worst-case contingency check
   * ``redispatch_trigger``  -- union of the two (default in the paper)

3. Figure generation delegated to ``plotting.py``, which was augmented
   with an occurrence + alleviation map pair sharing the same layout /
   colormap / legend position so the two can be placed side-by-side in
   the paper without further alignment.

The CLI is backward compatible: the legacy arguments
(``--network --output-dir --threshold --minimum-voltage --line --lines``)
still work and behave as before when ``--method`` is ``loading`` and
``--target-area`` is ``kupferzell`` (the defaults preserved for drop-in
behaviour with old bsub scripts).  Set ``--method redispatch_trigger``
and ``--target-area corridor`` to activate the new paper pipeline.
"""

from __future__ import annotations

import argparse
import re
import warnings
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import pypsa

from plotting import (
    plot_kupferzell_loading,
    plot_monthly_congestion,
    plot_top_congested_lines,
    plot_congestion_occurrence_map,
    plot_kupferzell_zoomed_network_map,
    plot_average_load_map_from_network,
    plot_average_line_loading_map_from_network,
    KUPFERZELL_ZOOM_EXTENT,
)

# ---------------------------------------------------------------------------
# Static configuration
# ---------------------------------------------------------------------------
SIM_YEAR = 2025

# Legacy threshold preserved so existing callers keep working.
CONGESTION_THRESHOLD = 0.98
DEFAULT_MINIMUM_VOLTAGE = 0.0

# Paper-recommended thresholds.  These are the ones we use when
# ``--method redispatch_trigger`` is selected.  The base-case threshold
# is 0.90 (operational redispatch trigger) and the N-1 threshold is 1.00
# (worst-case post-contingency thermal limit).
PAPER_LOADING_THRESHOLD = 0.90
PAPER_N1_THRESHOLD = 1.00
DUAL_TOL = 1e-3

# Kupferzell site (unchanged from legacy).
KUPFERZELL_LAT = 49.2333
KUPFERZELL_LON = 9.6833
KUPFERZELL_RADIUS_DEG = 0.8

# Tight N-1 physical reach radius for the "corridor" target area.
# 1° lat ≈ 111 km; 1° lon ≈ 72 km at 49°N → 1.3° covers ~100–115 km in all
# directions from Kupferzell (49.23°N, 9.68°E).  The BOTH-endpoints requirement
# in find_corridor_lines() is the critical filter: a line connecting a BW node
# to a distant Hessen/Bavaria node will have one endpoint outside the radius
# and be excluded, keeping only lines the Kupferzell battery can physically
# influence under N-1.
KUPFERZELL_N1_RADIUS_DEG: float = 1.3

# BW load-centre name patterns used as a secondary filter WITHIN the radius
# to recover lines whose PyPSA-Eur bus coordinates are slightly imprecise.
# Far-field Hessen/Bavaria entries (Mecklar, Borken, Dipperz, Grafenrheinfeld,
# Redwitz, Bergrheinfeld, Raitersaich) are removed — all ≥ 150 km from
# Kupferzell with negligible PTDF.
TARGET_LOAD_CENTRE_PATTERNS: tuple[str, ...] = (
    # Kupferzell and directly adjacent BW 380 kV substations
    "kupferzell", "grossgartach", "goldshoefe",
    "pulverdingen", "muehlhausen", "altbach", "deizisau", "hoheneck",
    # BW Rhine-side load centres within N-1 reach
    "daxlanden", "philippsburg", "rheinau", "weinheim",
    "mannheim", "ludwigshafen", "heilbronn",
    # Southern BW
    "herbertingen", "tiengen", "bad-sackingen", "sackingen",
)

# ---------------------------------------------------------------------------
# Targeted line selection (paper default, brochure-faithful, clustering-agnostic).
# Translates the three TransnetBW Kupferzell GridBooster site-selection
# criteria from netzboosterpilotanlagebroschuere into purely structural tests
# so the same corridor is returned for any PyPSA-Eur clustering choice
# (k-means / modularity / hierarchical -- per Brown & Frysztacki, 16-Apr-2026).
#   (1) close to overloaded lines  -> EHV + both endpoints in radius
#   (2) N-S overload flow          -> non-trivial latitude span on the line
#   (3) SW power plants available  -> dispatchable cap SW of line midpoint
TARGETED_RADIUS_DEG: float = 0.9
TARGETED_MIN_VOLTAGE_KV: float = 220.0
TARGETED_MIN_LAT_SPAN_DEG: float = 0.05
TARGETED_MIN_SOUTHWEST_GEN_MW: float = 500.0

TARGETED_DISPATCHABLE_CARRIERS: tuple[str, ...] = (
    "CCGT", "OCGT", "coal", "lignite", "oil", "nuclear",
    "biomass", "geothermal", "reservoir", "waste",
)

CSV_FLOAT_FORMAT = "%.5f"

PROJECT_DIR = Path(__file__).resolve().parent
PYPSA_EUR_DIR = PROJECT_DIR.parent / "pypsa-eur"
DEFAULT_SOLVED_NETWORK = (
    PYPSA_EUR_DIR / "results" / "kupferzell_2024_full" / "networks" / "base_s_256_elec_.nc"
)
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "results"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export congestion occurrence diagnostics")
    p.add_argument("--network", type=Path, default=DEFAULT_SOLVED_NETWORK,
                   help="Solved PyPSA network netcdf")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Output root directory. Results are written to "
             "<output-dir>/<scenario>/congestion_occurrence",
    )
    p.add_argument("--threshold", type=float, default=CONGESTION_THRESHOLD,
                   help="Base-case congestion threshold in pu of s_nom "
                        "(only used by --method loading / redispatch_trigger).")
    p.add_argument("--threshold-n1", type=float, default=PAPER_N1_THRESHOLD,
                   help="N-1 post-contingency threshold in pu of s_nom "
                        "(only used by --method n_minus_1 / redispatch_trigger).")
    p.add_argument(
        "--method",
        choices=("dual", "loading", "n_minus_1", "redispatch_trigger"),
        default="dual",
        help=(
            "Congestion detection method:\n"
            "  dual               -- shadow-price (paper default, requires keep_shadowprices=True)\n"
            "  loading            -- base-case |p0|/s_nom >= --threshold (legacy default)\n"
            "  n_minus_1          -- LODF-based worst-case post-outage loading\n"
            "  redispatch_trigger -- union of the two (paper default)"
        ),
    )
    p.add_argument(
        "--target-area",
        choices=("kupferzell_node", "kupferzell_corridor",
                 "kupferzell_brochure_line_selection", "all", "custom_lines"),
        default="custom_lines",
        help=(
            "Target area for reporting and plotting:\n"
            "  kupferzell_node                  -- lines attached to the Kupferzell node (legacy)\n"
            "  kupferzell_corridor              -- both endpoints within KUPFERZELL_N1_RADIUS_DEG (backup)\n"
            "  kupferzell_brochure_line_selection -- brochure-faithful structural filter (paper default)\n"
            "  all                              -- every line in the network\n"
            "  custom_lines                     -- use --custom-lines argument (default)"
        ),
    )
    p.add_argument(
        "--custom-lines",
        type=str,
        default="",
        help=(
            "Custom comma-separated line IDs to analyze (overrides --target-area). "
            "E.g. 'Line 5234, Line 5235' or '111,222,333'"
        ),
    )
    p.add_argument(
        "--minimum-voltage",
        type=float,
        default=DEFAULT_MINIMUM_VOLTAGE,
        help=(
            "Minimum line voltage in kV to include in the analysis.  "
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
    p.add_argument(
        "--skip-load-map",
        action="store_true",
        help="Do not emit the per-node average load map "
             "(which requires n.loads_t.p or p_set to be populated).",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Scenario / output-dir helpers (unchanged from legacy)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Voltage filtering (unchanged from legacy)
# ---------------------------------------------------------------------------
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
            f"Excluded {excluded} line(s) below minimum_voltage={minimum_voltage:.1f} kV "
            f"(or with invalid v_nom).",
            UserWarning,
            stacklevel=2,
        )

    return pd.Index(eligible), excluded


# ---------------------------------------------------------------------------
# Target-area selection
# ---------------------------------------------------------------------------
def find_kupferzell_lines(n: pypsa.Network) -> pd.Index:
    """Return line ids with at least one endpoint within KUPFERZELL_RADIUS_DEG."""
    buses = n.buses.copy()
    buses["dist_deg"] = np.sqrt(
        (buses["y"] - KUPFERZELL_LAT) ** 2 + (buses["x"] - KUPFERZELL_LON) ** 2
    )
    near = buses[buses["dist_deg"] <= KUPFERZELL_RADIUS_DEG].index
    lines = n.lines[n.lines.bus0.isin(near) | n.lines.bus1.isin(near)]
    return lines.index


def _normalise_name(value: str) -> str:
    """Lower-case ascii-fold a bus/substation name for pattern matching."""
    if not isinstance(value, str):
        return ""
    s = value.lower()
    # Common German -> ascii substitutions so the pattern list stays readable.
    for src, dst in (
        ("\u00e4", "ae"), ("\u00f6", "oe"), ("\u00fc", "ue"), ("\u00df", "ss"),
    ):
        s = s.replace(src, dst)
    # Drop punctuation that sometimes appears in bus ids.
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def find_corridor_lines(n: pypsa.Network) -> pd.Index:
    """Return line ids within the Kupferzell GridBooster's N-1 physical reach.

    Inclusion criterion (AND of both conditions):
      1. Both bus0 and bus1 lie within KUPFERZELL_N1_RADIUS_DEG of Kupferzell.
         Rationale: if either endpoint is far away, the PTDF of a Kupferzell
         injection for that line is small and N-1 contingency relief is not
         physically meaningful.  This removes the Hessen / Bavaria far-field
         lines that the old bounding-box approach incorrectly captured.
      2. At least one endpoint matches a named BW load-centre pattern OR lies
         within the radius (condition 1 already implies this for both endpoints).

    The named patterns in TARGET_LOAD_CENTRE_PATTERNS serve only as a secondary
    check for PyPSA-Eur bus ids whose coordinates may be slightly imprecise; they
    do NOT override the radius filter.
    """
    buses = n.buses.copy()
    x = pd.to_numeric(buses["x"], errors="coerce")
    y = pd.to_numeric(buses["y"], errors="coerce")

    # --- 1. Radius filter (primary) ----------------------------------------
    dist_deg = np.sqrt(
        (y - KUPFERZELL_LAT) ** 2 + (x - KUPFERZELL_LON) ** 2
    )
    in_reach = dist_deg <= KUPFERZELL_N1_RADIUS_DEG

    # --- 2. Named-pattern filter (secondary, within reach only) ------------
    candidates = [buses.index.astype(str).to_series().values]
    if "name" in buses.columns:
        candidates.append(buses["name"].astype(str).values)
    haystacks = pd.DataFrame(
        {i: vals for i, vals in enumerate(candidates)}, index=buses.index
    ).astype(str)
    normalised = haystacks.applymap(_normalise_name)

    name_hit = pd.Series(False, index=buses.index)
    for pat in TARGET_LOAD_CENTRE_PATTERNS:
        pat_norm = _normalise_name(pat)
        if not pat_norm:
            continue
        hit = normalised.apply(lambda col: col.str.contains(pat_norm, regex=False)).any(axis=1)
        name_hit |= hit

    # A bus is eligible only when it is within the tight N-1 radius.  The
    # name pattern cannot promote a bus outside the radius (effectively in_reach).
    eligible = in_reach | (name_hit & in_reach)
    eligible_buses = buses[eligible.fillna(False)].index

    # --- 3. Require BOTH endpoints within reach ----------------------------
    # A line connecting an in-reach bus to an out-of-reach bus is excluded:
    # the far endpoint implies the line traverses territory where the
    # Kupferzell PTDF is too small for meaningful N-1 contingency relief.
    lines = n.lines[
        n.lines.bus0.isin(eligible_buses) & n.lines.bus1.isin(eligible_buses)
    ]

    if len(lines) == 0:
        warnings.warn(
            f"find_corridor_lines: no lines found with both endpoints within "
            f"{KUPFERZELL_N1_RADIUS_DEG}° of Kupferzell. Check network bus "
            f"coordinates or relax KUPFERZELL_N1_RADIUS_DEG.",
            UserWarning,
            stacklevel=2,
        )
    return lines.index


# ---------------------------------------------------------------------------
# Targeted (brochure-faithful) line selection -- new paper default
# ---------------------------------------------------------------------------
def _dispatchable_capacity_at_bus(n: pypsa.Network) -> pd.Series:
    """Sum p_nom (or p_nom_opt when populated) of dispatchable generators per bus."""
    if n.generators.empty:
        return pd.Series(dtype=float)
    gens = n.generators
    if "p_nom_opt" in gens.columns and (gens["p_nom_opt"].fillna(0.0) > 0).any():
        cap_col = "p_nom_opt"
    else:
        cap_col = "p_nom"
    carrier = gens["carrier"].astype(str).str.lower()
    keep = carrier.isin([c.lower() for c in TARGETED_DISPATCHABLE_CARRIERS])
    if not keep.any():
        warnings.warn(
            "find_targeted_lines: no generator carriers matched "
            f"{TARGETED_DISPATCHABLE_CARRIERS}; criterion (3) cannot be "
            "evaluated and will not exclude any line.",
            RuntimeWarning, stacklevel=3,
        )
        return pd.Series(dtype=float)
    cap_per_bus = gens.loc[keep].groupby("bus")[cap_col].sum().astype(float)
    return cap_per_bus.reindex(n.buses.index, fill_value=0.0)


def find_targeted_lines(
    n: pypsa.Network,
    *,
    radius_deg: float = TARGETED_RADIUS_DEG,
    min_voltage_kv: float = TARGETED_MIN_VOLTAGE_KV,
    min_lat_span_deg: float = TARGETED_MIN_LAT_SPAN_DEG,
    min_southwest_gen_mw: float = TARGETED_MIN_SOUTHWEST_GEN_MW,
) -> pd.Index:
    """Brochure-faithful line selection. Depends only on n.buses, n.lines,
    n.generators -- no LOPF results -- so it is reproducible across any
    PyPSA-Eur clustering choice."""
    buses, lines = n.buses, n.lines
    if buses.empty or lines.empty:
        warnings.warn("find_targeted_lines: empty network.", UserWarning, stacklevel=2)
        return pd.Index([], name="Line")

    x = pd.to_numeric(buses["x"], errors="coerce")
    y = pd.to_numeric(buses["y"], errors="coerce")
    dist_deg = np.sqrt((y - KUPFERZELL_LAT) ** 2 + (x - KUPFERZELL_LON) ** 2)
    in_reach_buses = buses.index[(dist_deg <= radius_deg).fillna(False)]

    # (1) Both endpoints in radius + EHV
    cand = lines[lines["bus0"].isin(in_reach_buses) & lines["bus1"].isin(in_reach_buses)]
    if "v_nom" in cand.columns:
        v_nom = pd.to_numeric(cand["v_nom"], errors="coerce")
        cand = cand[v_nom >= min_voltage_kv]
    if cand.empty:
        warnings.warn(
            f"find_targeted_lines: no EHV lines have both endpoints within "
            f"{radius_deg}° of Kupferzell.", UserWarning, stacklevel=2,
        )
        return pd.Index([], name="Line")

    # (2) N-S extent
    lat0, lat1 = cand["bus0"].map(y), cand["bus1"].map(y)
    lon0, lon1 = cand["bus0"].map(x), cand["bus1"].map(x)
    cand = cand[(lat0 - lat1).abs() >= min_lat_span_deg]
    if cand.empty:
        warnings.warn(
            f"find_targeted_lines: no candidate has lat span >= "
            f"{min_lat_span_deg}°.", UserWarning, stacklevel=2,
        )
        return pd.Index([], name="Line")

    # (3) Dispatchable capacity SW of midpoint
    if min_southwest_gen_mw > 0:
        cap_per_bus = _dispatchable_capacity_at_bus(n)
        if not cap_per_bus.empty:
            cap_in_reach = cap_per_bus.reindex(in_reach_buses, fill_value=0.0)
            blat = y.reindex(in_reach_buses)
            blon = x.reindex(in_reach_buses)
            mid_lat = (lat0.loc[cand.index] + lat1.loc[cand.index]) / 2.0
            mid_lon = (lon0.loc[cand.index] + lon1.loc[cand.index]) / 2.0
            keep_mask = pd.Series(False, index=cand.index)
            for line_id in cand.index:
                sw = (blat < mid_lat[line_id]) & (blon <= mid_lon[line_id])
                cap_sw = float(cap_in_reach[sw.fillna(False)].sum())
                keep_mask[line_id] = cap_sw >= min_southwest_gen_mw
            cand = cand[keep_mask]

    if cand.empty:
        warnings.warn(
            f"find_targeted_lines: no candidate has >= {min_southwest_gen_mw:.0f} MW "
            f"dispatchable cap SW within {radius_deg}° of Kupferzell.",
            UserWarning, stacklevel=2,
        )
        return pd.Index([], name="Line")
    return cand.index


def select_target_lines(
    n: pypsa.Network,
    target_area: str,
    requested_lines: list[str],
    minimum_voltage: float,
    custom_lines: str = "",
) -> tuple[pd.Index, str, int]:
    """Pick the set of lines to report on, respecting custom_lines override."""
    # Custom lines takes precedence over target_area
    if custom_lines:
        custom_list = [ln.strip() for ln in custom_lines.split(",") if ln.strip()]
        missing = [line for line in custom_list if line not in n.lines.index]
        if missing:
            raise ValueError(f"Custom line(s) not found in network: {', '.join(missing)}")
        candidate_lines = pd.Index(custom_list)
        line_scope = "custom_lines"
    elif requested_lines:
        missing = [line for line in requested_lines if line not in n.lines.index]
        if missing:
            raise ValueError(f"Requested line(s) not found in network: {', '.join(missing)}")
        candidate_lines = pd.Index(requested_lines)
        line_scope = "custom_lines"
    elif target_area == "kupferzell_node":
        candidate_lines = find_kupferzell_lines(n)
        line_scope = "kupferzell_node"
        if len(candidate_lines) == 0:
            candidate_lines = n.lines.index
            line_scope = "all_lines_fallback"
    elif target_area == "kupferzell_corridor":
        candidate_lines = find_corridor_lines(n)
        line_scope = "kupferzell_corridor"
        if len(candidate_lines) == 0:
            candidate_lines = n.lines.index
            line_scope = "all_lines_fallback"
    elif target_area == "kupferzell_brochure_line_selection":
        candidate_lines = find_targeted_lines(n)
        line_scope = "kupferzell_brochure_line_selection"
        if len(candidate_lines) == 0:
            warnings.warn(
                "kupferzell_brochure_line_selection returned empty; falling back to "
                "find_corridor_lines(). Inspect TARGETED_* constants if "
                "this happens for a network you trust.",
                UserWarning, stacklevel=2,
            )
            candidate_lines = find_corridor_lines(n)
            line_scope = "corridor_fallback"
        if len(candidate_lines) == 0:
            candidate_lines = n.lines.index
            line_scope = "all_lines_fallback"
    elif target_area == "all":
        candidate_lines = n.lines.index
        line_scope = "all_lines"
    else:
        raise ValueError(f"Unknown target-area: {target_area}")

    target_lines, excluded = filter_lines_by_minimum_voltage(n, candidate_lines, minimum_voltage)
    return target_lines, line_scope, excluded


# ---------------------------------------------------------------------------
# Base-case loading (unchanged, plus helpers reused by N-1 detection)
# ---------------------------------------------------------------------------
def compute_line_loading(n: pypsa.Network) -> pd.DataFrame:
    """Compute hourly loading fraction for each AC line."""
    return n.lines_t.p0.abs().div(n.lines.s_nom, axis=1)


def _country_pair(bus0: str, bus1: str) -> str:
    c0, c1 = bus0[:2], bus1[:2]
    return "-".join(sorted([c0, c1]))


# ---------------------------------------------------------------------------
# N-1 via LODF (BODF) -- new in this revision
# ---------------------------------------------------------------------------
def _compute_bodf_for_lines(n: pypsa.Network) -> tuple[pd.DataFrame, list[str]] | None:
    """Compute a Line x Line LODF matrix via PyPSA's SubNetwork.calculate_BODF.

    Returns ``(bodf_df, line_ids)`` where ``bodf_df`` is an ``L x L``
    DataFrame (rows indexed by monitored line, columns by outaged line),
    or ``None`` if the network topology does not support a meaningful
    contingency analysis (no sub-networks, lonely buses, etc.).

    All computations are restricted to AC lines -- transformers and DC
    links are excluded from the BODF because the paper's scope is the
    AC corridor.
    """
    try:
        n.determine_network_topology()
    except Exception as exc:  # pragma: no cover - topology edge cases
        warnings.warn(f"determine_network_topology() failed: {exc}", RuntimeWarning)
        return None

    if not hasattr(n, "sub_networks") or n.sub_networks.empty:
        warnings.warn("Network has no sub-networks; skipping N-1 sweep.", RuntimeWarning)
        return None

    bodf_blocks: list[pd.DataFrame] = []
    covered_lines: list[str] = []

    for sn_name in n.sub_networks.index:
        try:
            sn_obj = n.sub_networks.at[sn_name, "obj"]
        except KeyError:
            continue
        try:
            sn_obj.calculate_BODF()
        except Exception as exc:  # pragma: no cover - BODF edge cases
            warnings.warn(
                f"SubNetwork {sn_name}: calculate_BODF() failed: {exc}",
                RuntimeWarning,
            )
            continue

        branches_i = sn_obj.branches_i()
        # Keep only AC lines (skip transformers / links)
        line_positions: list[int] = []
        line_ids: list[str] = []
        for pos, (comp, name) in enumerate(branches_i):
            if comp == "Line":
                line_positions.append(pos)
                line_ids.append(name)
        if not line_positions:
            continue

        sub_bodf = np.asarray(sn_obj.BODF)
        if sub_bodf.ndim != 2 or sub_bodf.shape[0] != len(branches_i):
            continue
        sub_bodf = sub_bodf[np.ix_(line_positions, line_positions)]
        bodf_blocks.append(pd.DataFrame(sub_bodf, index=line_ids, columns=line_ids))
        covered_lines.extend(line_ids)

    if not bodf_blocks:
        return None

    # Assemble a block-diagonal LODF for the whole AC network.
    all_lines = list(dict.fromkeys(covered_lines))
    bodf_full = pd.DataFrame(0.0, index=all_lines, columns=all_lines)
    for block in bodf_blocks:
        bodf_full.loc[block.index, block.columns] = block.values
    # Convention: outaging a line itself drops its flow to zero; BODF[l,l] = -1.
    for l in all_lines:
        bodf_full.at[l, l] = -1.0
    return bodf_full, all_lines


def compute_n1_worst_case_loading(
    n: pypsa.Network,
    target_lines: pd.Index,
    outage_lines: pd.Index | None = None,
) -> pd.DataFrame:
    """Hourly worst-case post-contingency loading fraction for every target line.

    Parameters
    ----------
    n
        Solved PyPSA network.
    target_lines
        Lines whose worst-case loading we want to monitor.
    outage_lines
        Lines allowed to be outaged.  Default = every AC line in the same
        sub-network(s).  For speed in very large networks the caller can
        restrict this to the target area plus a small buffer.

    Returns
    -------
    DataFrame of shape ``(snapshots, target_lines)`` with values in
    ``[0, inf)``.  A value >= 1.0 means that the outage of some other
    line would push the monitored line above its nameplate rating.
    """
    bodf_result = _compute_bodf_for_lines(n)
    if bodf_result is None:
        warnings.warn(
            "LODF could not be computed; returning zero-filled worst-case loadings.",
            RuntimeWarning,
        )
        return pd.DataFrame(
            0.0, index=n.snapshots, columns=list(target_lines)
        )

    bodf_df, covered_lines = bodf_result
    common_targets = [l for l in target_lines if l in covered_lines]
    if not common_targets:
        warnings.warn(
            "None of the target lines appear in the BODF coverage set; "
            "returning zero-filled worst-case loadings.",
            RuntimeWarning,
        )
        return pd.DataFrame(0.0, index=n.snapshots, columns=list(target_lines))

    if outage_lines is None:
        outage_ids = covered_lines
    else:
        outage_ids = [l for l in outage_lines if l in covered_lines]
        if not outage_ids:
            outage_ids = covered_lines

    # Base-case flows (T x L)
    p0 = n.lines_t.p0.reindex(columns=covered_lines).fillna(0.0)
    s_nom = n.lines["s_nom"].reindex(covered_lines).astype(float)

    # LODF slice: rows = monitored targets, cols = candidate outage lines.
    lodf = bodf_df.loc[common_targets, outage_ids].to_numpy()  # (M, K)
    flows_out = p0[outage_ids].to_numpy()  # (T, K)
    flows_mon = p0[common_targets].to_numpy()  # (T, M)
    s_mon = s_nom.reindex(common_targets).to_numpy()  # (M,)

    # Post-outage flow on monitored line m when outaging line k:
    #   f_m + LODF[m, k] * f_k
    # Worst case over k (excluding k == m implicitly because BODF[m,m] = -1
    # will send the flow to zero, which is never the worst case):
    # Vectorise over T to limit memory: compute in chunks of snapshots.
    out = np.empty((flows_mon.shape[0], len(common_targets)), dtype=float)
    chunk = 512  # snapshots per chunk
    for start in range(0, flows_mon.shape[0], chunk):
        stop = min(start + chunk, flows_mon.shape[0])
        f_out_chunk = flows_out[start:stop]        # (t, K)
        f_mon_chunk = flows_mon[start:stop]        # (t, M)
        # Contribution matrix: (t, M, K) = f_mon[:, :, None] + lodf[None, :, :] * f_out[:, None, :]
        contrib = f_mon_chunk[:, :, None] + lodf[None, :, :] * f_out_chunk[:, None, :]
        worst = np.max(np.abs(contrib), axis=2)  # (t, M)
        out[start:stop] = worst / s_mon[None, :]

    worst_df = pd.DataFrame(out, index=n.snapshots, columns=common_targets)
    # Fill missing target columns with zeros so the shape matches callers' expectations.
    missing = [l for l in target_lines if l not in common_targets]
    if missing:
        for l in missing:
            worst_df[l] = 0.0
        worst_df = worst_df[list(target_lines)]
    return worst_df


# ---------------------------------------------------------------------------
# Congestion flag matrices
# ---------------------------------------------------------------------------
def detect_congestion_flags(
    n: pypsa.Network,
    target_lines: pd.Index,
    method: str,
    threshold_loading: float,
    threshold_n1: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame | None]:
    """Return ``(loading, flags, n1_loading)`` matrices.

    ``loading`` is always the base-case hourly loading fraction.
    ``flags`` is the 0/1 congestion indicator for the chosen ``method``.
    ``n1_loading`` is the worst-case post-contingency loading fraction
    when ``method`` needs it, else ``None``.
    """
    loading = compute_line_loading(n)[target_lines]

    if method == "dual":
        mu_u = getattr(n.lines_t, "mu_upper", None)
        mu_l = getattr(n.lines_t, "mu_lower", None)
        if mu_u is None or len(mu_u) == 0:
            raise RuntimeError(
                "No line shadow prices on this network. Re-run the LOPF with "
                "`n.optimize(..., keep_shadowprices=True)`. Lines of interest: "
                f"{list(target_lines)[:5]}..."
            )
        mu_u = mu_u.reindex(columns=target_lines, fill_value=0.0).abs()
        if mu_l is not None and len(mu_l):
            mu_l = mu_l.reindex(columns=target_lines, fill_value=0.0).abs()
            mu_total = mu_u.add(mu_l, fill_value=0.0)
        else:
            mu_total = mu_u
        flags = (mu_total > DUAL_TOL).astype(int)
        return loading, flags, None

    if method == "loading":
        flags = (loading >= threshold_loading).astype(int)
        return loading, flags, None

    n1_loading = compute_n1_worst_case_loading(n, target_lines)
    if method == "n_minus_1":
        flags = (n1_loading >= threshold_n1).astype(int)
        return loading, flags, n1_loading

    if method == "redispatch_trigger":
        flags_load = (loading >= threshold_loading).astype(int)
        flags_n1 = (n1_loading >= threshold_n1).astype(int)
        flags = ((flags_load | flags_n1) > 0).astype(int)
        return loading, flags, n1_loading

    raise ValueError(f"Unknown method: {method}")


def compute_multi_line_congestion_occurrence(
    n: pypsa.Network,
    corridor_lines: pd.Index,
    dual_tol: float = DUAL_TOL,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Report all simultaneously congested corridor lines per hour."""
    if getattr(n.lines_t, "mu_upper", None) is None or n.lines_t.mu_upper.empty:
        raise RuntimeError(
            "mu_upper not available — solve with keep_shadowprices=True."
        )

    mu = n.lines_t.mu_upper.abs()
    common = corridor_lines.intersection(mu.columns)
    missing = corridor_lines.difference(mu.columns)
    if len(missing) > 0:
        warnings.warn(
            f"{len(missing)} corridor line(s) absent from mu_upper: {list(missing)[:5]}",
            UserWarning,
            stacklevel=2,
        )
    mu_corridor = mu[common]

    hourly_wide = mu_corridor.where(mu_corridor > dual_tol, other=0.0)

    congested_mask = mu_corridor > dual_tol
    n_congested_per_hour = congested_mask.sum(axis=1).rename("n_congested")

    records: list[dict[str, object]] = []
    for ts in mu_corridor.index:
        lines_congested = mu_corridor.columns[congested_mask.loc[ts]]
        if len(lines_congested) == 0:
            continue
        mu_vals = mu_corridor.loc[ts, lines_congested].sort_values(ascending=False)
        for rank, (line_id, mu_val) in enumerate(mu_vals.items(), start=1):
            records.append(
                {
                    "timestamp": ts,
                    "line_id": line_id,
                    "mu_abs": float(mu_val),
                    "s_nom_mw": (
                        float(n.lines.loc[line_id, "s_nom"])
                        if line_id in n.lines.index
                        else np.nan
                    ),
                    "n_congested": int(n_congested_per_hour.loc[ts]),
                    "rank_in_hour": rank,
                }
            )

    hourly_long = pd.DataFrame(records)
    if hourly_long.empty:
        hourly_long = pd.DataFrame(
            columns=[
                "timestamp",
                "line_id",
                "mu_abs",
                "s_nom_mw",
                "n_congested",
                "rank_in_hour",
            ]
        )

    return hourly_wide, hourly_long


def summarise_multi_line_congestion(hourly_long: pd.DataFrame) -> pd.DataFrame:
    """Summary statistics on simultaneous multi-line congestion."""
    if hourly_long.empty:
        return pd.DataFrame(columns=["n_congested_lines", "hours", "share_pct"])

    per_hour = (
        hourly_long.groupby("timestamp")["n_congested"]
        .first()
        .value_counts()
        .sort_index()
        .reset_index()
    )
    per_hour.columns = ["n_congested_lines", "hours"]
    total = per_hour["hours"].sum()
    per_hour["share_pct"] = (per_hour["hours"] / total * 100).round(2)
    return per_hour


# ---------------------------------------------------------------------------
# Summary tables (mostly unchanged; extended to carry the new columns)
# ---------------------------------------------------------------------------
def summarize_line_congestion(
    n: pypsa.Network,
    loading: pd.DataFrame,
    flags: pd.DataFrame,
    n1_loading: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create line-level congestion summary and hourly binary flags."""
    summary = pd.DataFrame(index=loading.columns)
    summary["bus0"] = n.lines.reindex(summary.index)["bus0"]
    summary["bus1"] = n.lines.reindex(summary.index)["bus1"]
    summary["country_pair"] = [
        _country_pair(summary.at[idx, "bus0"], summary.at[idx, "bus1"])
        for idx in summary.index
    ]
    summary["s_nom_mw"] = n.lines.reindex(summary.index)["s_nom"]
    summary["mean_loading"] = loading.mean()
    summary["p95_loading"] = loading.quantile(0.95)
    summary["max_loading"] = loading.max()
    if n1_loading is not None:
        summary["p95_n1_loading"] = n1_loading.quantile(0.95)
        summary["max_n1_loading"] = n1_loading.max()
    summary["congested_hours"] = flags.sum()
    summary["congested_share_pct"] = 100.0 * summary["congested_hours"] / max(len(loading), 1)
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
    """Create long-format hourly loading table for the monitored lines.

    The filename ``kupferzell_line_proximity_hourly_{YEAR}.csv`` is kept
    unchanged (``congestion_cost_alleviation.py`` auto-discovers it by
    that pattern).  When the paper's ``corridor`` target-area is active
    we still emit this file -- ``congestion_cost_alleviation.py`` then
    sees the full corridor, not just the radius around Kupferzell.
    """
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


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def run_congestion_postprocess(
    network: Path = DEFAULT_SOLVED_NETWORK,
    output_dir: Path = DEFAULT_OUTPUT_ROOT,
    threshold: float = CONGESTION_THRESHOLD,
    minimum_voltage: float = DEFAULT_MINIMUM_VOLTAGE,
    requested_lines: list[str] | None = None,
    method: str = "dual",
    threshold_n1: float = PAPER_N1_THRESHOLD,
    target_area: str = "kupferzell",
    custom_lines: str = "",
    skip_load_map: bool = False,
) -> None:
    if not network.exists():
        raise FileNotFoundError(f"Solved network not found: {network}")

    resolved_output_dir, scenario = resolve_congestion_output_dir(network, output_dir)
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    n = pypsa.Network(network)

    target_lines, line_scope, excluded_by_voltage = select_target_lines(
        n, target_area, requested_lines or [], minimum_voltage, custom_lines,
    )

    loading, flags, n1_loading = detect_congestion_flags(
        n=n,
        target_lines=target_lines,
        method=method,
        threshold_loading=threshold,
        threshold_n1=threshold_n1,
    )

    summary, flags = summarize_line_congestion(n, loading, flags, n1_loading)
    monthly = summarize_monthly_congestion(flags)
    by_interface = summarize_country_pair_congestion(summary)

    # Always emit the "Kupferzell proximity" CSV because the alleviation
    # stage discovers it by filename.  The *contents* reflect whatever
    # target area the user asked for.
    proximity_df = export_kupferzell_proximity(n, loading, threshold, line_ids=target_lines)

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
    rent_per_line = None
    if n1_loading is not None:
        n1_loading.to_csv(
            resolved_output_dir / f"{prefix}_n1_worst_case_loading.csv",
            float_format=CSV_FLOAT_FORMAT,
        )
    if method == "dual":
        mu_u = getattr(n.lines_t, "mu_upper", None)
        mu_l = getattr(n.lines_t, "mu_lower", None)
        # Use a canonical "corridor" prefix for the mu CSVs so that downstream
        # shell scripts (job_congestion_occurrence_pypsa.sh Step 2 and
        # job_congestion_alleviation.sh) can always locate them regardless of
        # how line_scope was resolved (e.g. "custom_lines", "kupferzell_brochure_line_selection").
        canonical_prefix = f"congestion_corridor_{method}_{SIM_YEAR}"
        if mu_u is not None and len(mu_u):
            mu_u_target = mu_u.reindex(columns=target_lines, fill_value=0.0)
            mu_u_target.to_csv(
                resolved_output_dir / f"{canonical_prefix}_mu_upper.csv",
                float_format="%.8f",
            )
        else:
            mu_u_target = pd.DataFrame(index=n.snapshots, columns=target_lines, data=0.0)
        if mu_l is not None and len(mu_l):
            mu_l_target = mu_l.reindex(columns=target_lines, fill_value=0.0)
            mu_l_target.to_csv(
                resolved_output_dir / f"{canonical_prefix}_mu_lower.csv",
                float_format="%.8f",
            )
        else:
            mu_l_target = pd.DataFrame(index=n.snapshots, columns=target_lines, data=0.0)
        s_nom = n.lines["s_nom"].reindex(target_lines).astype(float)
        mu_abs = mu_u_target.abs()
        if not mu_l_target.empty:
            mu_abs = mu_abs.add(mu_l_target.abs(), fill_value=0.0)
        rent_per_line = mu_abs.multiply(s_nom, axis=1).sum(axis=0).rename(
            "congestion_rent_eur"
        )
        rent_per_line.to_csv(
            resolved_output_dir / f"{prefix}_congestion_rent_eur_per_line.csv",
            header=True,
            float_format=CSV_FLOAT_FORMAT,
        )
        hourly_wide, hourly_long = compute_multi_line_congestion_occurrence(
            n, target_lines, dual_tol=DUAL_TOL
        )
        concurrency_summary = summarise_multi_line_congestion(hourly_long)
        wide_path = resolved_output_dir / f"corridor_congestion_shadow_wide_{SIM_YEAR}.csv"
        long_path = resolved_output_dir / f"corridor_congestion_shadow_long_{SIM_YEAR}.csv"
        conc_path = resolved_output_dir / f"corridor_congestion_concurrency_{SIM_YEAR}.csv"
        hourly_wide.to_csv(wide_path, float_format="%.5f")
        hourly_long.to_csv(long_path, index=False, float_format="%.5f")
        concurrency_summary.to_csv(conc_path, index=False)
        print(
            f"  [saved] {wide_path.name}  ({hourly_wide.shape[0]} hours × "
            f"{hourly_wide.shape[1]} corridor lines)"
        )
        print(f"  [saved] {long_path.name}  ({len(hourly_long)} congested line-hours)")
        print(f"  [saved] {conc_path.name}")
        print("\n  Concurrency breakdown:")
        print(concurrency_summary.to_string(index=False))
    if not proximity_df.empty:
        proximity_df.to_csv(
            resolved_output_dir / f"kupferzell_line_proximity_hourly_{SIM_YEAR}.csv",
            index=False,
            float_format=CSV_FLOAT_FORMAT,
        )

    # ---- Figures ---------------------------------------------------------
    # Highlight the lines actually analyzed so the bold map overlay matches
    # the active --target-area. Falls back to the legacy radius set for "all".
    if line_scope in {"kupferzell_node", "kupferzell_corridor",
                      "kupferzell_brochure_line_selection",
                      "corridor_fallback", "custom_lines"}:
        kupferzell_line_ids = pd.Index(target_lines)
    else:
        kupferzell_line_ids = find_kupferzell_lines(n)

    plot_top_congested_lines(
        summary,
        str(resolved_output_dir / f"figure_{prefix}_occurrence_per_line.png"),
    )
    if line_scope in {"kupferzell_node", "kupferzell_corridor"} and not proximity_df.empty:
        plot_kupferzell_loading(
            proximity_df,
            str(resolved_output_dir / f"figure_kupferzell_line_loading_{SIM_YEAR}.png"),
            threshold,
        )
    plot_monthly_congestion(
        monthly,
        str(resolved_output_dir / f"figure_{prefix}_monthly.png"),
    )
    plot_congestion_occurrence_map(
        summary=summary,
        buses=n.buses,
        lines=n.lines,
        output_path=str(resolved_output_dir / f"figure_{prefix}_congestion_occurrence_map.png"),
        minimum_voltage=minimum_voltage,
        kupferzell_line_ids=None,
        fixed_extent=KUPFERZELL_ZOOM_EXTENT,
        show_all_network_lines=True,
    )
    plot_kupferzell_zoomed_network_map(
        buses=n.buses,
        lines=n.lines,
        output_path=str(resolved_output_dir / f"figure_{prefix}_kupferzell_zoomed_network_map.png"),
        kupferzell_line_ids=kupferzell_line_ids,
        minimum_voltage=minimum_voltage,
    )
    if not skip_load_map:
        try:
            plot_average_load_map_from_network(
                n,
                str(resolved_output_dir / f"figure_{prefix}_average_load_map.png"),
                minimum_voltage=minimum_voltage,
                kupferzell_line_ids=kupferzell_line_ids,
            )
        except Exception as exc:
            warnings.warn(f"Average-load map skipped: {exc}", RuntimeWarning)
        try:
            plot_average_line_loading_map_from_network(
                n,
                str(resolved_output_dir / f"figure_{prefix}_average_line_loading_map.png"),
                minimum_voltage=minimum_voltage,
                kupferzell_line_ids=kupferzell_line_ids,
            )
        except Exception as exc:
            warnings.warn(f"Average-line-loading map skipped: {exc}", RuntimeWarning)

    # ---- Diagnostics log -------------------------------------------------
    print(f"Scenario         : {scenario}")
    print(f"Method           : {method}")
    print(f"Target area      : {target_area} (resolved: {line_scope})")
    print(f"Minimum voltage  : {minimum_voltage:.1f} kV")
    print(f"Excluded by v    : {excluded_by_voltage}")
    print(f"Analyzed lines   : {len(target_lines)}")
    print(f"Threshold load   : {threshold:.3f}")
    if method == "dual":
        print(f"Binding-hour tol : {DUAL_TOL:.1e} EUR/MWh")
        if rent_per_line is not None:
            print(f"Total rent       : {rent_per_line.sum() / 1e6:.2f} M EUR")
    if method in {"n_minus_1", "redispatch_trigger"}:
        print(f"Threshold N-1    : {threshold_n1:.3f}")
    total_flag_hours = int(flags.values.sum())
    lines_with_any = int((flags.sum(axis=0) > 0).sum())
    print(f"Congested line-h : {total_flag_hours}")
    print(f"Lines with >=1h  : {lines_with_any} of {len(target_lines)}")
    if method == "loading":
        smax_max = (
            float(n.lines["s_max_pu"].reindex(target_lines).fillna(1.0).max())
            if "s_max_pu" in n.lines.columns else 1.0
        )
        if smax_max < threshold:
            print(
                f"  [ARTEFACT] s_max_pu={smax_max:.2f} < threshold={threshold:.2f} — "
                "the LOPF never exceeds s_max_pu, so this count is uncalibrated for "
                "this network configuration. Use method=dual for shadow-price congestion flags."
            )
    print(f"Saved congestion outputs in: {resolved_output_dir}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main() -> None:
    args = parse_args()
    requested_lines = normalize_requested_lines(args.line, args.lines)
    run_congestion_postprocess(
        network=args.network,
        output_dir=args.output_dir,
        threshold=args.threshold,
        minimum_voltage=args.minimum_voltage,
        requested_lines=requested_lines,
        method=args.method,
        threshold_n1=args.threshold_n1,
        target_area=args.target_area,
        custom_lines=args.custom_lines,
        skip_load_map=args.skip_load_map,
    )


if __name__ == "__main__":
    main()
