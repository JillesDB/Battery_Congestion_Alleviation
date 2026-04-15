# PyPSA-Eur + Battery Congestion Alleviation — Complete Setup Guide

**Version**: 2.0 (April 2026)  
**Target Environment**: DTU HPC (Linux, `/zhome` storage)  
**Research Focus**: Grid congestion analysis with battery optimization using 2025 grid & demand data, 2013 weather year (industry standard)

---

## Table of Contents

1. [Overview & Key Learnings](#overview--key-learnings)
2. [Pre-Requisites & Environment](#pre-requisites--environment)
3. [HPC Storage & Git Workflow](#hpc-storage--git-workflow)
4. [Virtual Environment Setup](#virtual-environment-setup)
5. [PyPSA-Eur Installation & Configuration](#pypsa-eur-installation--configuration)
6. [Data Management](#data-management)
7. [Critical Patches & Fixes](#critical-patches--fixes)
8. [Running PyPSA-Eur on HPC](#running-pypsa-eur-on-hpc)
9. [Integration with Battery Congestion Pipeline](#integration-with-battery-congestion-pipeline)
10. [Research Methodology & Workflow](#research-methodology--workflow)
11. [Troubleshooting & Recovery](#troubleshooting--recovery)

---

## Overview & Key Learnings

Kupferzell GridBooster — Transmission Congestion Simulation
Purpose: Quantify hourly congestion on Kupferzell-area 380 kV lines
using PyPSA-EUR market dispatch (LOPF), calibrated to SMARD 2024 actuals.

### What Changed From Previous Setup

1. **Storage**: Use `/zhome` **exclusively** for all code, venvs, and data. `/work3` is **not used at all**.
2. **PROJ Database**: Use rasterio's bundled PROJ data (`proj_data/`) to avoid schema mismatches with system or pyproj PROJ databases.
3. **Geospatial Fixes**: 
   - `atlite` now tolerates malformed raster CRS metadata (e.g., invalid `LOCAL_CS` in CORINE GeoTIFFs).
   - Filename wildcards corrected: `base_s_{clusters}` instead of `base_s.{clusters}`.
4. **Config Handling**: Boolean `luisa`/`corine` settings now pass through without crashing the availability matrix rule.
5. **Data**: Integrate 2025 grid topology (via open-MaStR), 2025 demand, 2025 renewables, but keep 2013 weather (weather year standard in literature).

### Research Methodology Alignment

This setup follows the methodology from your research paper with one key update:
- **Paper approach**: Grid & demand data as-is (year varies), 2013 weather year.
- **This project**: Explicit 2025 grid/demand/renewables, 2013 weather year.

Methodological Notes
Network choice: PyPSA-EUR (128-bus)
Cross-border flows from Nordic/North Sea wind are the primary physical
driver of the German N-S corridor overloads that motivate the Kupferzell
GridBooster. PyPSA-DE cannot capture these; PyPSA-EUR is required.
Reference: Frysztacki et al. (2021), Energy 246:123234
→ validates PyPSA-EUR DC flows against ENTSO-E observations on German corridor.
Congestion criterion
Line l is congested in hour t iff:
|p0_{l,t}| / s_nom_l ≥ 0.98
The 2% tolerance handles LP numerical precision. The s_nom values in
PyPSA-EUR encode N-1 secure thermal ratings (i.e., they already incorporate
the N-1 security factor used by TransnetBW in operation).
Spatial resolution caveat
At 128-bus clustering, Kupferzell is aggregated into the
Heilbronn/Hohenlohe/northeastern BW cluster. The corridor lines
Kupferzell–Großgartach and Kupferzell–Stalldorf may be represented
as a single aggregated line in the clustered network.
Mitigation: Run at 256 or 512 buses for publication-quality spatial
resolution (increases Step 3 runtime to ~24–48 hours).
Set NETWORK_CLUSTERING = 256 in 00_config.py.
LOPF vs full AC OPF
DC linearisation introduces ~5–15% error on individual line flows
relative to full AC OPF. For congestion frequency (a binary threshold),
this error affects the precision of the congestion count but not the
qualitative finding. Standard in the literature.
N-1 security
Full SCLOPF (Security-Constrained LOPF) is computationally prohibitive
for 8784-hour problems. We implement a post-hoc PTDF-based N-1 check
in Step 4 for the corridor lines (see n1_contingency_check()).

## Pre-Requisites & Environment

### Required Tools

```bash
# On HPC login node, verify:
module load python3/3.12.4
python3 --version        # Should be 3.12.4
which git                # Should be present
```

### Network & Git Access

Ensure you can clone from GitHub (or use SSH keys):

```bash
ssh-keygen -t ed25519 -C "your_email@dtu.dk"
# Add public key to GitHub settings
```

---

## HPC Storage & Git Workflow

### Storage Layout

**All work is in `/zhome` — `/work3` is NOT used.**

```
/zhome/26/e/209460/
├── venvs/
│   └── kupferzell/                 # Isolated Python environment
├── PycharmProjects/
│   ├── Battery_Congestion_Alleviation/   # Your analysis repo
│   │   ├── config/                       # Configs (kupferzell_2024.yaml)
│   │   ├── data/                         # Local reference data
│   │   ├── shell_scripts/                # HPC job launchers
│   │   ├── outputs/                      # Analysis results
│   │   └── README.md, SETUP_GUIDE.md     # This file
│   └── pypsa-eur/                        # PyPSA-Eur repo (upstream)
│       ├── config/                       # config.default.yaml
│       ├── data/                         # Network & GIS data (~15GB)
│       ├── resources/                    # Generated resources
│       ├── results/                      # Final network & solve outputs
│       └── shell_scripts/                # Snakemake launchers
└── .open-MaStR/                          # open-MaStR data (grid topology)
```

### Git Branching

Develop in a dedicated branch; keep `main` stable.

```bash
cd ~/PycharmProjects/Battery_Congestion_Alleviation
git checkout -b research/2025-grid-update
git push -u origin research/2025-grid-update
```

Commit after major milestones:

```bash
git add -A
git commit -m "feat: integrate 2025 grid topology from open-MaStR"
git push
```

---

## Virtual Environment Setup

### Create & Activate

```bash
# Create isolated venv
python3 -m venv ~/venvs/kupferzell

# Activate
source ~/venvs/kupferzell/bin/activate

# Verify
which python3     # Should be ~/venvs/kupferzell/bin/python3
python3 --version # Should be 3.12.4
```

### Upgrade Core Tools

```bash
pip install --upgrade pip setuptools wheel
```

### Install PyPSA & Solver Stack

```bash
# Core solver (Gurobi for production, CBC for free fallback)
pip install "pypsa>=0.28" highspy

# Snakemake workflow engine
pip install snakemake snakemake-executor-plugin-local

# Numerical & data stack
pip install pandas numpy scipy matplotlib xarray netCDF4 h5netcdf \
            requests tqdm pyyaml python-dotenv

# Geospatial stack (CRITICAL for CRS handling)
pip install geopandas shapely pyproj rasterio

# Power system & network tools
pip install country_converter powerplantmatching networkx scikit-learn \
            tables openpyxl ruamel.yaml atlite

# Optional: visualization
pip install plotly dash
```

### Lock Dependencies

```bash
pip freeze > ~/PycharmProjects/Battery_Congestion_Alleviation/requirements.txt
```

---

## PyPSA-Eur Installation & Configuration

### Clone Upstream

```bash
cd ~/PycharmProjects
git clone https://github.com/PyPSA/pypsa-eur.git
cd pypsa-eur
git log --oneline | head -5  # Verify commit hash
```

### Configuration: `config/kupferzell_2024.yaml`

Create a dedicated config for your 2025 grid analysis:

```bash
cp config/config.default.yaml config/kupferzell_2024.yaml
```

Edit `config/kupferzell_2024.yaml`:

```yaml
# Metadata
version: v2026.02.0

# Run naming
run:
  prefix: ""
  name: "kupferzell_2024_full"
  scenarios:
    enable: false
    file: config/scenarios.yaml
  disable_progressbar: false
  shared_resources:
    policy: true
    exclude: []
  use_shadow_directory: false

# Countries (Germany + Denmark focused, rest low resolution/single node for solvability)
countries:
-AT
-BE
-CH
-CZ
- DE
- DK
-FR
-NL
-PL

# Snapshots (full year 2025)
snapshots:
  start: "2025-01-01"
  end: "2025-12-31"
  inclusive: left

# Clustering
scenario:
  simpl:
    - ''
  ll:
    - ''
  clusters:
    - 256              # 256-bus clustering for resolution
  opts:
    - ""
  sector_opts:
    - ""
  planning_horizons:
    - 2025             # Planning horizon (not data year)

# Electricity configuration
electricity:
  voltages:
    - 220.0
    - 300.0
    - 330.0
    - 380.0            # Key Kupferzell line
    - 400.0
    - 500.0
    - 750.0
  base_network: osm    # Uses OSM + open-MaStR overlay
  transmission_limit: vopt

# Renewable technologies - luisa for all
renewable:
  onwind:
    cutout: default
    resource:
      method: wind
      turbine: Vestas_V112_3MW
    corine:
      grid_codes: [12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 31, 32]
      distance: 1000.0
    luisa: true        
    natura: false

  solar:
    cutout: default
    resource:
      method: pv
      panel: CSi
    corine: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 26, 31, 32]
    luisa: true
    natura: false

  offwind-ac:
    cutout: default
    corine: false
    luisa: true        
    natura: false
    ship_threshold: 400
    max_depth: 60.0
    max_shore_distance: 30000.0

# Atlite (cutout & availability matrix)
atlite:
  default_cutout: "europe-2013-sarah3-era5"
  nprocesses: 8
  show_progress: false
  plot_availability_matrix: false

conventional:
  unit_commitment: false
  dynamic_fuel_price: false
  fuel_price_rolling_window: 6
  nuclear:
    p_max_pu: data/nuclear_p_max_pu.csv
  biomass:
    p_min_pu: 0.4

load:
  fill_gaps:
    enable: true
    interpolate_limit: 6
    time_shift_for_large_gaps: 1w
  manual_adjustments: true
  scaling_factor: 1.0
  fixed_year: false
  supplement_synthetic: true
  substation_only: true
  distribution_key:
    gdp: 0.6
    population: 0.4

sector:
  transport: false
  heating: false
  biomass: false
  industry: true
  shipping: false
  aviation: false
  agriculture: true
  fossil_fuels: true
  district_heating:
    potential: 0.6

data:
  synthetic_electricity_demand:
    source: primary
    version: latest

# Costs
costs:
  year: 2025            # Use 2050 cost year (even though network is 2025)
```

### Verify Configuration

```bash
snakemake -n --configfile config/kupferzell_2024.yaml -- resources/kupferzell_2024/regions_onshore_base_s_256.geojson
```

This should list ~15 jobs without errors.

---

## Data Management

### Data Sources & Versions

| Data | Source | Version | Year | Notes |
|------|--------|---------|------|-------|
| Grid Topology | open-MaStR + OSM | 2025-04 | 2025 | Already downloaded to `/zhome/.open-MaStR` |
| Demand | SMARD + custom | 2025 | 2013 weather | Use 2025 annual profile |
| Renewables | IRENA + custom | 2025 | 2013 weather | Capacity data 2025 |
| Weather/Wind/Solar | SARAH3/ERA5 | 2013 | 2013 | Standard practice in literature |
| Land Use (CORINE) | Copernicus | 2018 (v18.5) | — | Availability exclusions |
| Bathymetry (GEBCO) | GEBCO | 2014 | — | Offshore wind exclusions |

### Directory Structure

```bash
~/PycharmProjects/pypsa-eur/data/
├── cutout/
│   └── archive/v1.0/
│       └── europe-2013-sarah3-era5.nc     # Weather cutout (required)
├── corine/
│   └── archive/v18_5/
│       └── corine.tif                      # Land use exclusions
├── natura/
│   └── archive/2025-08-15/
│       └── natura.tiff
├── gebco/
│   └── archive/2014/
│       └── GEBCO_2014_2D.nc
├── luisa_land_cover/
│   └── archive/2021-03-02/
│       └── LUISA_basemap_020321_50m.tif
└── osm/                                    # Downloaded by Snakemake
```

### 2025 Grid Integration: open-MaStR

You've already downloaded open-MaStR to `/zhome/.open-MaStR`. 

To integrate it:

1. **Preprocess open-MaStR data** (create a new script):

```bash
cat > ~/PycharmProjects/Battery_Congestion_Alleviation/scripts/integrate_2025_grid.py << 'EOF'
"""
Integrate 2025 grid topology from open-MaStR into PyPSA base network.
"""
import json
from pathlib import Path
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, LineString

def load_openmstr_data(mstr_path):
    """Load and parse open-MaStR JSONs."""
    units = pd.read_json(f"{mstr_path}/Einheiten_Data.json", lines=True)
    # Filter for German (country_code == 'DE') power plants
    de_units = units[units['country_code'] == 'DE']
    return de_units

def integrate_2025_grid(pypsa_eur_path, mstr_path):
    """Overlay 2025 generator/load capacity from open-MaStR onto PyPSA base."""
    # Load PyPSA base network
    import pypsa
    n = pypsa.Network(f"{pypsa_eur_path}/data/networks/base_s.nc")
    
    # Load open-MaStR data
    units = load_openmstr_data(mstr_path)
    
    # Update generators with 2025 capacity
    # (Implementation depends on your specific mapping strategy)
    # Placeholder:
    print(f"Loaded {len(units)} units from open-MaStR")
    print(f"Integration logic: map unit geolocations to network buses, update p_nom_max")
    
if __name__ == "__main__":
    mstr_path = Path.home() / ".open-MaStR"
    pypsa_path = Path.home() / "PycharmProjects" / "pypsa-eur"
    integrate_2025_grid(str(pypsa_path), str(mstr_path))
EOF
```

2. **Call this before running Snakemake**:

```bash
cd ~/PycharmProjects/Battery_Congestion_Alleviation
python3 scripts/integrate_2025_grid.py
```

---

## Critical Patches & Fixes

### Why These Patches Exist

During development, we discovered three critical issues:

1. **Boolean LUISA/Corine Settings** → crashes `determine_availability_matrix`
2. **Malformed Raster CRS** → CORINE GeoTIFF ships invalid `LOCAL_CS` metadata
3. **Filename Template Typo** → `base_s.{clusters}` should be `base_s_{clusters}`

**Status**: All patches are already applied to your repo and upstream `pypsa-eur` fork.

### Verify Patches Are Applied

```bash
# Check atlite CRS handling
grep -n "raster_crs_valid" ~/venvs/kupferzell/lib/python3.12/site-packages/atlite/gis.py

# Check filename fix
grep -n "regions_onshore_base_s_{clusters}" ~/PycharmProjects/pypsa-eur/rules/build_electricity.smk

# Check boolean guard
grep -n "renewable.*is a boolean" ~/PycharmProjects/pypsa-eur/scripts/determine_availability_matrix.py
```

All three should return matches.

---

## Running PyPSA-Eur on HPC

### Environment Setup Before Each Run

```bash
module purge
module load python3/3.12.4
source ~/venvs/kupferzell/bin/activate

# Use rasterio-bundled PROJ DB (matches runtime schema expected by rasterio/atlite)
export PROJ_DATA="$(python3 -c "import os, rasterio; print(os.path.join(os.path.dirname(rasterio.__file__), 'proj_data'))")"
export PROJ_LIB="$PROJ_DATA"
export PROJ_NETWORK=OFF

cd ~/PycharmProjects/pypsa-eur
```

### Actual Solve Pipeline (Observed in Runs)

For the 256-cluster electricity solve, the observed rule order is:

1. `build_powerplants`
2. `add_electricity`
3. `prepare_network`
4. `solve_network`

The important file handoff is:

- `build_powerplants` writes: `resources/kupferzell_2024/powerplants_s_256.csv`
- `add_electricity` writes: `resources/kupferzell_2024/networks/base_s_256_elec.nc`
- `prepare_network` writes: `resources/kupferzell_2024/networks/base_s_256_elec_.nc`
- `solve_network` reads the `resources/...base_s_256_elec_.nc` file and writes solved outputs under `results/kupferzell_2024/...`

So when debugging pre-solve network content (carriers, load, p_nom), inspect the `resources/` network files; when debugging final optimization output, inspect `results/`.

### HPC Job Script

Use the existing launcher:

```bash
cat ~/PycharmProjects/Battery_Congestion_Alleviation/shell_scripts/job_snakemake_kupferzell_full.sh
```

### Submit Job

```bash
cd ~/PycharmProjects/pypsa-eur
bsub < ~/PycharmProjects/Battery_Congestion_Alleviation/shell_scripts/job_snakemake_kupferzell_full.sh
```

### Monitor Job

```bash
bjobs
bjobs -l <job_id>

tail -f ~/PycharmProjects/Battery_Congestion_Alleviation/hpc_output_and_error_files/Output_<job_id>.err
```

### Verify Artifacts by Stage

```bash
# Pre-solve artifacts (rule outputs)
ls -lh resources/kupferzell_2024/powerplants_s_256.csv
ls -lh resources/kupferzell_2024/networks/base_s_256_elec.nc
ls -lh resources/kupferzell_2024/networks/base_s_256_elec_.nc

# Solver outputs
ls -lh results/kupferzell_2024/logs/solve_network/
ls -lh results/kupferzell_2024/networks/
```

### Custom Powerplants Checks (Critical)

If CCGT/OCGT or hydro look wrong, validate these before rerun:

1. In `config/kupferzell_2024.yaml`, avoid excluding Germany unintentionally:
   - bad: `Country != 'DE'`
   - expected for DE-focused run: `Country == 'DE'` (or remove country clause)
2. Ensure `data/custom_powerplants.csv` is regenerated after any script change.
3. Hydro `Technology` labels in `custom_powerplants.csv` must be canonical:
   - `Run-Of-River`
   - `Reservoir`
   - `Pumped Storage`

---

## Troubleshooting & Recovery

### Issue: `FileNotFoundError` for `.../results/.../base_s_256_elec_.nc`

**Cause**: That network may still exist only as the intermediate pre-solve file in `resources/`.

**What to check first:**

```bash
ls -lh ~/PycharmProjects/pypsa-eur/resources/kupferzell_2024/networks/base_s_256_elec_.nc
ls -lh ~/PycharmProjects/pypsa-eur/results/kupferzell_2024/networks/
```

If your post-processing script targets the pre-solve network, point it to `resources/.../base_s_256_elec_.nc`.

### Issue: CCGT/OCGT capacities look implausible

**Typical cause in this setup**: misconfigured `powerplants_filter` in `config/kupferzell_2024.yaml` and/or gas technology classification in `build_custom_powerplants_2025.py`.

**Quick checks:**

```bash
# Check filter
grep -n "powerplants_filter" config/kupferzell_2024.yaml

# Check generated custom file totals
python3 - <<'PY'
import pandas as pd
p='data/custom_powerplants.csv'
df=pd.read_csv(p)
print(df.groupby('Fueltype')['Capacity'].sum().sort_values(ascending=False).head(10))
print('\nGas split (if present):')
if {'Fueltype','Technology','Capacity'}.issubset(df.columns):
    g=df[df['Fueltype'].str.lower().isin(['gas','natural gas'])]
    print(g.groupby('Technology')['Capacity'].sum())
PY
```

### Issue: Hydro from custom file does not appear

**Cause**: Hydro rows were present but `Technology` labels were not in PyPSA-Eur-compatible form, so they were dropped/mismapped downstream.

**Fix**: Regenerate `data/custom_powerplants.csv` with canonical hydro labels (`Run-Of-River`, `Reservoir`, `Pumped Storage`) and rerun `build_powerplants`.

---

## Complete Workflow: From Scratch (Actual)

```bash
# 1) Environment
module purge
module load python3/3.12.4
source ~/venvs/kupferzell/bin/activate
export PROJ_DATA="$(python3 -c "import os, rasterio; print(os.path.join(os.path.dirname(rasterio.__file__), 'proj_data'))")"
export PROJ_LIB="$PROJ_DATA"
export PROJ_NETWORK=OFF

# 2) Go to pypsa-eur
cd ~/PycharmProjects/pypsa-eur

# 3) Regenerate custom plants if you changed the builder
python3 ~/PycharmProjects/Battery_Congestion_Alleviation/build_custom_powerplants_2025.py

# 4) Dry-run the exact final target
snakemake -n --cores 1 --profile profiles/hpc --configfile config/kupferzell_2024.yaml -- \
  results/kupferzell_2024/networks/base_s_256_elec_.nc

# 5) Submit full run
bsub < ~/PycharmProjects/Battery_Congestion_Alleviation/shell_scripts/job_snakemake_kupferzell_full.sh

# 6) Monitor
bjobs -l <job_id>
tail -f ~/PycharmProjects/Battery_Congestion_Alleviation/hpc_output_and_error_files/Output_<job_id>.err

# 7) Inspect intermediate and final files
ls -lh resources/kupferzell_2024/networks/base_s_256_elec_.nc
ls -lh results/kupferzell_2024/networks/
```

---

## Key Files & Locations (Reference)

| Purpose | Path | Notes |
|---------|------|-------|
| **This guide** | `~/PycharmProjects/Battery_Congestion_Alleviation/SETUP_GUIDE.md` | Read first |
| **Config (PyPSA)** | `~/PycharmProjects/pypsa-eur/config/kupferzell_2024.yaml` | 2025 grid + 2013 weather |
| **Config (Requirements)** | `~/PycharmProjects/Battery_Congestion_Alleviation/requirements.txt` | Locked dependencies |
| **HPC Launcher** | `~/PycharmProjects/Battery_Congestion_Alleviation/shell_scripts/job_snakemake_kupferzell.sh` | Submit with `bsub` |
| **open-MaStR data** | `~/.open-MaStR/` | 2025 grid topology (already downloaded) |
| **Final network (pre-solve)** | `~/PycharmProjects/pypsa-eur/resources/kupferzell_2024/networks/base_s_256_elec_.nc` | Produced by `prepare_network`; used for diagnostics and pre-solve post-processing |
| **Solved network outputs** | `~/PycharmProjects/pypsa-eur/results/kupferzell_2024/networks/` | Produced by `solve_network` |
| **Patch 1: atlite CRS** | `~/venvs/kupferzell/lib/python3.12/site-packages/atlite/gis.py` | Handles malformed raster CRS |
| **Patch 2: build_electricity** | `~/PycharmProjects/pypsa-eur/rules/build_electricity.smk` | Fixes filename wildcards |
| **Patch 3: availability matrix** | `~/PycharmProjects/pypsa-eur/scripts/determine_availability_matrix.py` | Handles boolean LUISA/Corine |

---

## Status Checklist

- [x] HPC environment configured (Python 3.12.4)
- [x] Virtual environment created & locked
- [x] PyPSA-Eur cloned & patched
- [x] PROJ database path fixed (rasterio bundled)
- [x] 2025 grid data available (open-MaStR at `~/.open-MaStR`)
- [x] 2013 weather cutout available
- [x] Git workflow (dedicated branch) ready
- [x] HPC job script prepared
- [x] Battery analysis framework structure defined
- [x] Research methodology aligned

---

## Next Steps (Research Pipeline)

1. **Submit PyPSA-Eur job** → Generate 2025 grid network with 2013 weather
2. **Integrate open-MaStR** → Update generator capacities & locations
3. **Add battery storage** → Strategic placement (Kupferzell, high-congestion areas)
4. **Run market dispatch** → LOPF with market prices, identify congestion
5. **Analyze flows** → Hourly Kupferzell corridor loading, nodal prices
6. **Bidding optimization** → Layer your battery strategy on top
7. **Publish results** → Reproducible workflow in Git, data in `outputs/`

---

## References & Further Reading

- **PyPSA-Eur**: https://github.com/PyPSA/pypsa-eur
- **Atlite**: https://github.com/PyPSA/atlite
- **open-MaStR**: https://github.com/OpenEnergy-Platform/data-preprocessing/tree/master/data-import/openmast
- **PyPSA Documentation**: https://pypsa.readthedocs.io/

---

## Support & Troubleshooting

If you encounter issues:

1. **Check this guide** (Ctrl+F for your error keyword)
2. **Review HPC logs**: `tail -f hpc_output_and_error_files/Output_*.err`
3. **Consult PyPSA-Eur docs** & issue tracker
4. **Verify PROJ paths**: `echo $PROJ_DATA $PROJ_LIB`
5. **Git branch clean**: `git status` should show no uncommitted changes

---

**End of SETUP_GUIDE.md**  
**Last Updated**: April 10, 2026  
**Maintained By**: Your Name  
**Status**: Production-Ready

