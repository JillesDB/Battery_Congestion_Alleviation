import pypsa
import pandas as pd
import numpy as np
from pathlib import Path

NETWORK_PATH = Path("data/base_s_128_elec_.nc")
SMARD_PATH   = Path("data/smard_generation_2019_2023.csv")  # your existing loader output

# ── 1. Load PyPSA-Eur network (topology only) ─────────────────────────────
n = pypsa.Network(NETWORK_PATH)

print(f"Buses:       {len(n.buses)}")
print(f"Lines:       {len(n.lines)}")
print(f"Generators:  {len(n.generators)}")

# ── 2. Strip synthetic capacity-expansion generators ──────────────────────
# Keep only conventional dispatchable units; remove variable renewables
# whose profiles came from the stub matrices (not real historical data)
synthetic_vres = n.generators.index[
    n.generators.carrier.isin(["onwind", "offwind-ac", "offwind-dc",
                               "solar", "solar-hsat"])
]
n.remove("Generator", synthetic_vres)
print(f"After stripping synthetic VRE: {len(n.generators)} generators remain")

# ── 3. Load SMARD historical actuals ──────────────────────────────────────
smard = pd.read_csv(SMARD_PATH, index_col=0, parse_dates=True)
# smard columns: e.g. ["wind_onshore_MW", "wind_offshore_MW", "solar_MW",
#                       "biomass_MW", "hydro_MW", "nuclear_MW", "load_MW"]

# Resample to hourly if needed
smard = smard.resample("h").mean()

# ── 4. Add historical VRE as fixed injection (p_max_pu time series) ───────
# Map SMARD national totals to PyPSA buses via installed capacity weights
# This uses the PyPSA-Eur bus-level installed capacity as spatial weights

# Example: distribute national wind onshore proportional to
# remaining conventional generator capacity per bus (crude proxy;
# replace with actual spatial distribution from MaStR if available)

de_buses = n.buses.index[n.buses.country == "DE"]

for carrier, smard_col in [
    ("onwind",   "wind_onshore_MW"),
    ("offwind",  "wind_offshore_MW"),
    ("solar",    "solar_MW"),
]:
    if smard_col not in smard.columns:
        continue

    # Uniform distribution across DE buses as starting point
    # (refine with MaStR capacity weights per bus in final version)
    n_de_buses = len(de_buses)
    p_nom_total = smard[smard_col].max()

    for bus in de_buses:
        gen_name = f"{bus} {carrier}_historical"
        p_nom    = p_nom_total / n_de_buses

        n.add(
            "Generator",
            gen_name,
            bus=bus,
            carrier=carrier,
            p_nom=p_nom,
            p_min_pu=0.0,
            p_max_pu=smard[smard_col] / smard[smard_col].max(),
            marginal_cost=0.0,
        )

# ── 5. Set study period ───────────────────────────────────────────────────
# Match your congestion event sample period from the stochastic model
START = "2021-01-01"
END   = "2021-12-31"

n.set_snapshots(pd.date_range(START, END, freq="h"))

# ── 6. Run linearised DC power flow ──────────────────────────────────────
n.lopf(
    pyomo=False,
    solver_name="highs",
    keep_shadowprices=True,
)

# ── 7. Extract line loadings on Kupferzell corridor ──────────────────────
# The corridor = lines connecting Baden-Württemberg load centres
# to the 380kV EHV backbone (Heilbronn, Rheinhafen, Altbach, Mannheim)
corridor_lines = n.lines[
    n.lines.bus0.str.contains("DE") &
    n.lines.bus1.str.contains("DE")
    ].index  # refine with actual line names from network inspection

loading = (
        n.lines_t.p0[corridor_lines].abs()
        / n.lines.s_nom[corridor_lines]
)

print("\nKupferzell corridor max loading (fraction of s_nom):")
print(loading.max().sort_values(ascending=False).head(10))
# ── 8. Compute avoided redispatch potential ───────────────────────────────
# Lines loaded > 90% of s_nom are congested
congested = loading > 0.90
congestion_hours = congested.sum()
print("\nCongestion hours per corridor line:")
print(congestion_hours.sort_values(ascending=False).head(10))

# Save for stochastic model integration
loading.to_csv("outputs/corridor_loading_2021.csv")
congested.to_csv("outputs/congestion_flags_2021.csv")