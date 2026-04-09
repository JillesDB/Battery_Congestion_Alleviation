"""
pypsa_config.py — Kupferzell GridBooster Congestion Simulation
==============================================================
Central configuration for all pipeline scripts.

RUN_MODE controls two distinct simulation configurations:

  "historical_2025"
      Generator capacities are set to observed 2025 installed values
      (BNetzA / SMARD sources, provided in capacity_data_2025 below).
      Generation dispatch time series are anchored to SMARD 2025 actuals
      (local CSV files). Load profile from SMARD demand CSV.
      Use this for: empirical congestion count, paper main results.

  "pypsa_default"
      Generator capacities and renewable profiles come from the
      PyPSA-EUR pre-built network without modification.
      Load profiles are scaled to match SMARD national total, but
      the spatial and temporal distribution follows PyPSA-EUR defaults.
      Use this for: sensitivity analysis, PyPSA-EUR baseline comparison.
"""

import os

# ── Run mode ──────────────────────────────────────────────────────────────────
# Set to "historical_2025" or "pypsa_default"
RUN_MODE = "historical_2025"

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(BASE_DIR, "data")
OUTPUT_DIR  = os.path.join(BASE_DIR, "outputs", RUN_MODE)
NETWORK_DIR = os.path.join(DATA_DIR, "networks")

for _d in [DATA_DIR, OUTPUT_DIR, NETWORK_DIR]:
    os.makedirs(_d, exist_ok=True)

# ── Local SMARD data files (provided externally) ──────────────────────────────
GENERATION_CSV = os.path.join(DATA_DIR, "Germany_generation_2025_quarterhour.csv")
DEMAND_CSV     = os.path.join(DATA_DIR, "Germany_demand_2025_quarterhour.csv")

# ── Simulation year ───────────────────────────────────────────────────────────
SIM_YEAR = 2025

# ── PyPSA-EUR network ─────────────────────────────────────────────────────────
# Pre-built 128-bus European electricity-only network.
# Download from Zenodo (see build_pypsa_network.py for instructions).
NETWORK_CLUSTERING = 128
NETWORK_FILE       = os.path.join(NETWORK_DIR, f"elec_s_{NETWORK_CLUSTERING}.nc")
PREPARED_FILE      = os.path.join(NETWORK_DIR,
                                   f"elec_s_{NETWORK_CLUSTERING}_{RUN_MODE}_prepared.nc")

# ── Kupferzell substation coordinates ────────────────────────────────────────
KUPFERZELL_LAT = 49.2333
KUPFERZELL_LON = 9.6833

# Corridor search radius (~80 km, captures Kupferzell–Großgartach–Stalldorf)
CORRIDOR_RADIUS_DEG = 0.8

# ── Congestion threshold ──────────────────────────────────────────────────────
# Line l congested in hour t  ⟺  |p0_{l,t}| / s_nom_l ≥ CONGESTION_THRESHOLD
CONGESTION_THRESHOLD = 0.98

# ── Solver ────────────────────────────────────────────────────────────────────
SOLVER = "gurobi"
SOLVER_OPTIONS = {
    "highs": {
        "time_limit": 7200,
        "presolve": "on",
        "parallel": "on",
    }
}

# ── Time resolution ───────────────────────────────────────────────────────────
# All simulations run at hourly resolution.
# Raw SMARD data is 15-min; resampled to 1h in the data loader.
FULL_YEAR  = True    # set False for a fast test run
TEST_WEEKS = 4       # weeks to simulate in test mode

# ── 2025 installed capacities (MW) — used in RUN_MODE = "historical_2025" ────
# Sources: BNetzA Bundesnetzagentur / SMARD Installed capacity report 2025.
# FLAG: verify against BNetzA Marktstammdatenregister Q1-2025 before submission.
#
# Mapping to PyPSA-EUR carrier names is handled in build_pypsa_network.py.
CAPACITY_DATA_2025 = {
    "Biomass":        9_055,
    "Lignite":       14_758,
    "CCGT":          23_799,
    "OCGT":           8_070,
    "Hard coal":      8_706,
    "Oil":            2_452,
    "Hydro":          5_889,   # run-of-river + reservoir (split below)
    "Pumped Storage": 9_894,
    "Solar":         77_016,
    "Waste":          1_832,
    "Wind Offshore":  9_215,
    "Wind Onshore":  65_405,
    "Other":         (8_307 + 970),   # other fossil + geothermal
}

# Hydro split: approximately 60% run-of-river, 40% reservoir for Germany
HYDRO_ROR_FRACTION = 0.60

# Offshore split: ~70% AC-connected, 30% DC-connected (HVDC links)
OFFSHORE_AC_FRACTION = 0.70

# PyPSA-EUR carrier name mapping from CAPACITY_DATA_2025 keys
# Values are lists to allow one source → multiple PyPSA carriers
CARRIER_MAP_2025 = {
    "Biomass":        ["biomass"],
    "Lignite":        ["lignite"],
    "CCGT":           ["CCGT"],
    "OCGT":           ["OCGT"],
    "Hard coal":      ["coal"],
    "Oil":            ["oil"],
    "Hydro":          ["ror", "hydro"],        # split by HYDRO_ROR_FRACTION
    "Pumped Storage": ["PHS"],
    "Solar":          ["solar"],
    "Waste":          ["biomass"],             # mapped to biomass carrier
    "Wind Offshore":  ["offwind-ac", "offwind-dc"],  # split by OFFSHORE_AC_FRACTION
    "Wind Onshore":   ["onwind"],
    "Other":          ["oil"],                 # residual; minor impact
}

# ── SMARD column → PyPSA carrier mapping (for profile attachment) ─────────────
SMARD_COL_TO_CARRIER = {
    "Biomass":                "biomass",
    "Hydropower":             "ror",
    "Wind offshore":          "offwind-ac",   # and offwind-dc (same profile)
    "Wind onshore":           "onwind",
    "Photovoltaics":          "solar",
    "Other renewable":        "biomass",      # approximate
    "Lignite":                "lignite",
    "Hard coal":              "coal",
    "Fossil gas":             "CCGT",         # split to OCGT below if needed
    "Hydro pumped storage":   "PHS",
    "Other conventional":     "oil",
}

if __name__ == "__main__":
    print("pypsa_config.py")
    print(f"  RUN_MODE    : {RUN_MODE}")
    print(f"  SIM_YEAR    : {SIM_YEAR}")
    print(f"  NETWORK     : {NETWORK_CLUSTERING}-bus PyPSA-EUR")
    print(f"  SOLVER      : {SOLVER}")
    print(f"  FULL_YEAR   : {FULL_YEAR}")
    print(f"  OUTPUT_DIR  : {OUTPUT_DIR}")
    total_cap = sum(CAPACITY_DATA_2025.values())
    print(f"\n  2025 total installed capacity: {total_cap/1e3:.1f} GW")
    for k, v in CAPACITY_DATA_2025.items():
        print(f"    {k:<20s}: {v/1e3:5.1f} GW")