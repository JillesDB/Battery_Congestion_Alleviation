"""
build_pypsa_network.py
======================
Downloads the PyPSA-EUR 128-bus electricity-only network and prepares it
for the Kupferzell congestion simulation.

Behaviour differs by RUN_MODE (set in pypsa_config.py):

  "historical_2025"
      - Generator capacities scaled to 2025 BNetzA installed values
        (CAPACITY_DATA_2025 in pypsa_config.py)
      - Load profiles replaced with SMARD 2025 observed demand
      - Renewable p_max_pu profiles replaced with SMARD 2025 actuals
        (spatially disaggregated using PyPSA-EUR regional weights)

  "pypsa_default"
      - Generator capacities left at PyPSA-EUR optimised values
      - PyPSA-EUR synthetic load and renewable profiles retained
      - SMARD demand used only to set snapshot index and absolute scale
        of total DE load (spatial distribution unchanged)

Run:
    python build_pypsa_network.py

Output:
    data/networks/elec_s_128_{RUN_MODE}_prepared.nc
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pypsa
import pandas as pd
import numpy as np
import requests
from pypsa_config import (
    NETWORK_FILE, PREPARED_FILE, NETWORK_DIR, RUN_MODE, SIM_YEAR,
    KUPFERZELL_LAT, KUPFERZELL_LON, CORRIDOR_RADIUS_DEG,
    CAPACITY_DATA_2025, CARRIER_MAP_2025, SMARD_COL_TO_CARRIER,
    HYDRO_ROR_FRACTION, OFFSHORE_AC_FRACTION,
    GENERATION_CSV, DEMAND_CSV, SOLVER,
)
from smard_data_loader import build_smard_bundle, validate_smard_bundle

# ── Zenodo network download ───────────────────────────────────────────────────
# PyPSA-EUR pre-built electricity-only network (128-bus, 2023 topology).
# If the DOI below resolves to a 404, check https://zenodo.org and search
# "PyPSA-EUR pre-built networks" for the current release DOI.
# FLAG: confirm this DOI matches the infrastructure year cited in Section 2.
ZENODO_URL = (
    "https://zenodo.org/record/8425038/files/"
    "elec_s_128_ec_lv1.0_Co2L0.nc?download=1"
)


def download_network() -> None:
    print(f"  Downloading PyPSA-EUR network from Zenodo ...")
    r = requests.get(ZENODO_URL, stream=True, timeout=180)
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))
    done = 0
    with open(NETWORK_FILE, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done/total*100:.1f}%", end="", flush=True)
    print(f"\n  ✓ Downloaded ({done/1e6:.0f} MB)")


def load_base_network() -> pypsa.Network:
    if not os.path.exists(NETWORK_FILE):
        print(f"  {NETWORK_FILE} not found. Downloading ...")
        try:
            download_network()
        except Exception as e:
            raise SystemExit(
                f"\nDownload failed: {e}\n\n"
                "Manual steps:\n"
                "  1. Visit https://zenodo.org/record/8425038\n"
                f"  2. Download the .nc file and place at:\n     {NETWORK_FILE}\n"
                "  3. Re-run this script."
            )
    n = pypsa.Network(NETWORK_FILE)
    print(f"  ✓ Loaded: {len(n.buses)} buses | {len(n.lines)} lines | "
          f"{len(n.generators)} generators")
    return n


# ── Sector-coupling and CO2 removal ──────────────────────────────────────────

def strip_to_electricity_only(n: pypsa.Network) -> pypsa.Network:
    """Remove any non-electricity components (safety check for elec_only build)."""
    elec_carriers = {
        "AC", "DC", "onwind", "offwind-ac", "offwind-dc",
        "solar", "solar rooftop", "ror", "hydro", "PHS",
        "biomass", "coal", "lignite", "CCGT", "OCGT",
        "nuclear", "oil", "gas", "load", "battery", "other",
    }
    non_elec = [c for c in n.carriers.index if c not in elec_carriers]
    for carrier in non_elec:
        mask = n.generators.carrier == carrier
        n.remove("Generator", n.generators[mask].index)
        print(f"    Removed {mask.sum()} {carrier} generators")
    if non_elec:
        print(f"  ✓ Stripped non-electricity carriers: {non_elec}")
    else:
        print("  ✓ No non-electricity carriers to strip")
    return n


def remove_co2_constraint(n: pypsa.Network) -> pypsa.Network:
    if "co2_limit" in n.global_constraints.index:
        n.remove("GlobalConstraint", "co2_limit")
        print("  ✓ CO2 global constraint removed (historical dispatch mode)")
    else:
        print("  ✓ No CO2 constraint present")
    return n


# ── Snapshot alignment ────────────────────────────────────────────────────────

def set_snapshots_from_smard(n: pypsa.Network,
                              smard_index: pd.DatetimeIndex
                              ) -> pypsa.Network:
    """Set network snapshots to the SMARD hourly UTC index."""
    n.set_snapshots(smard_index)
    n.snapshot_weightings[:] = 1.0   # each snapshot = 1 h
    print(f"  ✓ Snapshots: {len(n.snapshots)} hours "
          f"| {n.snapshots[0]} → {n.snapshots[-1]}")
    return n


# ── Capacity calibration (historical_2025 mode) ───────────────────────────────

def scale_de_capacities_to_2025(n: pypsa.Network) -> pypsa.Network:
    """
    Scale German generator p_nom to 2025 installed values from CAPACITY_DATA_2025.
    Non-German generators are left unchanged (PyPSA-EUR defaults).

    For each source category:
      target_MW is distributed proportionally to the existing p_nom
      of all DE generators of that carrier.

    For "Hydro", the total is split between "ror" and "hydro" carriers
    according to HYDRO_ROR_FRACTION.
    For "Wind Offshore", split between "offwind-ac" and "offwind-dc"
    according to OFFSHORE_AC_FRACTION.
    """
    de_buses = n.buses[n.buses.country == "DE"].index
    scaled = []

    for source_name, target_mw_total in CAPACITY_DATA_2025.items():
        carriers = CARRIER_MAP_2025[source_name]

        # Hydro: split between ror and hydro
        if source_name == "Hydro":
            splits = {
                "ror":   target_mw_total * HYDRO_ROR_FRACTION,
                "hydro": target_mw_total * (1 - HYDRO_ROR_FRACTION),
            }
        # Offshore: split between AC and DC
        elif source_name == "Wind Offshore":
            splits = {
                "offwind-ac": target_mw_total * OFFSHORE_AC_FRACTION,
                "offwind-dc": target_mw_total * (1 - OFFSHORE_AC_FRACTION),
            }
        else:
            splits = {c: target_mw_total / len(carriers) for c in carriers}

        for carrier, target_mw in splits.items():
            mask = (
                (n.generators.carrier == carrier) &
                (n.generators.bus.isin(de_buses))
            )
            de_gen = n.generators[mask]
            if de_gen.empty:
                continue
            current_total = de_gen["p_nom"].sum()
            if current_total < 1e-3:
                continue
            scale_factor = target_mw / current_total
            n.generators.loc[mask, "p_nom"] *= scale_factor
            scaled.append(
                f"    {source_name}→{carrier:<15s}: "
                f"{current_total/1e3:.1f} GW → {target_mw/1e3:.1f} GW "
                f"(×{scale_factor:.3f})"
            )

    print(f"  ✓ DE capacities scaled to 2025 ({len(scaled)} carrier groups):")
    for s in scaled:
        print(s)
    return n


# ── Load profile replacement ──────────────────────────────────────────────────

def attach_smard_load(n: pypsa.Network, demand: pd.Series) -> pypsa.Network:
    """
    Replace PyPSA-EUR synthetic load profiles with SMARD 2025 observed demand.

    Strategy: keep the spatial distribution (bus weights) from PyPSA-EUR,
    scale each bus's p_set so the national DE aggregate matches SMARD.
    This preserves the known load centre geography while anchoring the
    aggregate to observations.
    """
    de_buses = n.buses[n.buses.country == "DE"].index
    de_loads = n.loads[n.loads.bus.isin(de_buses)]
    if de_loads.empty:
        print("  [WARNING] No German loads found in network")
        return n

    demand_reindexed = demand.reindex(n.snapshots).interpolate()

    # Existing PyPSA-EUR p_set profiles (spatial weights)
    existing = n.loads_t.p_set.reindex(columns=de_loads.index, fill_value=0.0)
    total_existing = existing.sum(axis=1).replace(0, np.nan)

    for load_id in de_loads.index:
        weight = (existing[load_id] / total_existing).fillna(
            1.0 / len(de_loads)
        )
        n.loads_t.p_set[load_id] = (weight * demand_reindexed).fillna(0)

    total_after = n.loads_t.p_set[de_loads.index].sum(axis=1)
    print(f"  ✓ SMARD load attached to {len(de_loads)} DE buses")
    print(f"    Peak DE load in network: {total_after.max():.0f} MW")
    print(f"    SMARD peak:              {demand_reindexed.max():.0f} MW")
    return n


# ── Renewable profile replacement ─────────────────────────────────────────────

def attach_smard_renewable_profiles(n: pypsa.Network,
                                     generation: pd.DataFrame
                                     ) -> pypsa.Network:
    """
    Replace PyPSA-EUR capacity factor profiles for German renewables with
    SMARD 2025 actuals.

    Method: for each technology, compute the ratio:
        ratio_t = SMARD_national_MW_t / (Σ_i p_nom_i × p_max_pu_{i,t})
    then apply ratio_t to each individual bus profile, clipped to [0, 1].

    This preserves the regional distribution (north/south wind differences)
    from PyPSA-EUR while anchoring the national aggregate to observations.

    Only applied to variable renewable generators; dispatchable generators
    (coal, gas, lignite) have their dispatch determined by the LOPF.
    """
    de_buses = n.buses[n.buses.country == "DE"].index
    gen_reindexed = generation.reindex(n.snapshots).interpolate()

    vre_carriers = {
        "Biomass":       ["biomass"],
        "Hydropower":    ["ror"],
        "Wind offshore": ["offwind-ac", "offwind-dc"],
        "Wind onshore":  ["onwind"],
        "Photovoltaics": ["solar"],
    }

    updated = []
    for smard_col, carriers in vre_carriers.items():
        if smard_col not in gen_reindexed.columns:
            continue
        smard_national = gen_reindexed[smard_col]

        for carrier in carriers:
            mask = (
                (n.generators.carrier == carrier) &
                (n.generators.bus.isin(de_buses))
            )
            de_gen_ids = n.generators[mask].index
            if de_gen_ids.empty:
                continue

            # Generators that have p_max_pu time series
            de_with_ts = [g for g in de_gen_ids
                          if g in n.generators_t.p_max_pu.columns]
            if not de_with_ts:
                continue

            profiles = n.generators_t.p_max_pu[de_with_ts]
            p_noms   = n.generators.loc[de_with_ts, "p_nom"]
            model_gen = profiles.mul(p_noms, axis=1).sum(axis=1)

            # Scaling ratio (clip 0–3 to avoid exploding from near-zero)
            ratio = (smard_national / model_gen.replace(0, np.nan)
                     ).clip(0, 3).fillna(1.0)

            # Split offshore SMARD signal between AC and DC proportionally
            if carrier == "offwind-dc" and "Wind offshore" == smard_col:
                ratio = ratio * (1 - OFFSHORE_AC_FRACTION) / OFFSHORE_AC_FRACTION
            elif carrier == "offwind-ac" and "Wind offshore" == smard_col:
                pass   # full ratio applied to AC portion

            for gen_id in de_with_ts:
                if gen_id in n.generators_t.p_max_pu.columns:
                    n.generators_t.p_max_pu[gen_id] = (
                        (n.generators_t.p_max_pu[gen_id] * ratio)
                        .clip(0, 1)
                    )
            updated.append(f"{smard_col}→{carrier}")

    print(f"  ✓ SMARD renewable profiles attached ({', '.join(updated)})")
    return n


# ── Corridor identification ────────────────────────────────────────────────────

def report_kupferzell_corridor(n: pypsa.Network) -> None:
    """Print corridor buses and lines for verification."""
    buses = n.buses.copy()
    buses["dist"] = np.sqrt(
        (buses["y"] - KUPFERZELL_LAT) ** 2 +
        (buses["x"] - KUPFERZELL_LON) ** 2
    )
    cb = buses[buses["dist"] <= CORRIDOR_RADIUS_DEG]
    cl = n.lines[
        n.lines["bus0"].isin(cb.index) | n.lines["bus1"].isin(cb.index)
    ]

    print(f"\n  Kupferzell corridor ({CORRIDOR_RADIUS_DEG}° radius):")
    print(f"    Buses ({len(cb)}): {list(cb.index)}")
    print(f"    Lines ({len(cl)}):")
    for lid, row in cl.iterrows():
        print(f"      {lid}: {row['bus0']} → {row['bus1']}  "
              f"s_nom={row['s_nom']:.0f} MW")
    if cl.empty:
        print("    [WARNING] No corridor lines found. "
              "Consider increasing CORRIDOR_RADIUS_DEG.")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("build_pypsa_network.py")
    print(f"  RUN_MODE = {RUN_MODE}")
    print("=" * 60)

    if os.path.exists(PREPARED_FILE):
        print(f"\nPrepared network already exists: {PREPARED_FILE}")
        print("Loading for verification ...")
        n = pypsa.Network(PREPARED_FILE)
        report_kupferzell_corridor(n)
        raise SystemExit(
            "\nDelete the file above and re-run to rebuild from scratch."
        )

    # ── Load SMARD data ───────────────────────────────────────────────────────
    print("\n[1] Loading SMARD data ...")
    bundle = build_smard_bundle(GENERATION_CSV, DEMAND_CSV)
    validate_smard_bundle(bundle)
    generation = bundle["generation"]
    demand     = bundle["demand"]
    smard_index = bundle["index"]

    # ── Load base network ─────────────────────────────────────────────────────
    print("\n[2] Loading PyPSA-EUR base network ...")
    n = load_base_network()

    # ── Strip to electricity only ─────────────────────────────────────────────
    print("\n[3] Stripping to electricity-only ...")
    n = strip_to_electricity_only(n)

    # ── Remove CO2 constraint ─────────────────────────────────────────────────
    print("\n[4] Removing CO2 constraint ...")
    n = remove_co2_constraint(n)

    # ── Set snapshots ─────────────────────────────────────────────────────────
    print("\n[5] Setting hourly snapshots ...")
    n = set_snapshots_from_smard(n, smard_index)

    # ── Capacity calibration (RUN_MODE-dependent) ─────────────────────────────
    if RUN_MODE == "historical_2025":
        print("\n[6] Scaling DE capacities to 2025 BNetzA values ...")
        n = scale_de_capacities_to_2025(n)
    else:
        print("\n[6] Keeping PyPSA-EUR default capacities (pypsa_default mode)")

    # ── Load profiles ─────────────────────────────────────────────────────────
    print("\n[7] Attaching SMARD 2025 load profiles ...")
    n = attach_smard_load(n, demand)

    # ── Renewable profiles ────────────────────────────────────────────────────
    if RUN_MODE == "historical_2025":
        print("\n[8] Attaching SMARD 2025 renewable profiles ...")
        n = attach_smard_renewable_profiles(n, generation)
    else:
        print("\n[8] Keeping PyPSA-EUR default renewable profiles")

    # ── Save ──────────────────────────────────────────────────────────────────
    print(f"\n[9] Saving prepared network to {PREPARED_FILE} ...")
    n.export_to_netcdf(PREPARED_FILE)
    print(f"  ✓ Saved")

    # ── Corridor report ───────────────────────────────────────────────────────
    print("\n[10] Kupferzell corridor report ...")
    report_kupferzell_corridor(n)

    print(f"\n✓ build_pypsa_network.py complete  |  RUN_MODE={RUN_MODE}")