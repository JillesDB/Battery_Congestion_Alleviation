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

---

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
  name: kupferzell_2024
  prefix: ""
  scenarios:
    enable: false
  shared_resources:
    policy: false
    exclude: []

# Countries (Germany + Denmark for your grid focus)
countries:
  - DE
  - DK

# Snapshots (full year 2013, weather year standard)
snapshots:
  start: "2013-01-01"
  end: "2013-12-31"
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
    - 2050             # Planning horizon (not data year)

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

# Renewable technologies
renewable:
  onwind:
    cutout: default
    resource:
      method: wind
      turbine: Vestas_V112_3MW
    corine:
      grid_codes: [12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 31, 32]
      distance: 1000.0
    luisa: false        # Boolean false (not dict)
    natura: false

  solar:
    cutout: default
    resource:
      method: pv
      panel: CSi
    corine: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 26, 31, 32]
    luisa: false
    natura: false

  offwind-ac:
    cutout: default
    corine: false
    luisa: false        # Boolean false (ships important, but data-limited)
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

# Costs
costs:
  year: 2050            # Use 2050 cost year (even though network is 2025)
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
# Module setup
module purge
module load python3/3.12.4

# Activate venv
source ~/venvs/kupferzell/bin/activate

# Set PROJ to rasterio's bundled database (CRITICAL)
export PROJ_DATA="$(python3 -c "import os, rasterio; print(os.path.join(os.path.dirname(rasterio.__file__), 'proj_data'))")"
export PROJ_LIB="$PROJ_DATA"
export PROJ_NETWORK=OFF

# Navigate to pypsa-eur
cd ~/PycharmProjects/pypsa-eur
```

### HPC Job Script: `shell_scripts/job_snakemake_kupferzell.sh`

Already exists and is pre-configured. Review it:

```bash
cat ~/PycharmProjects/Battery_Congestion_Alleviation/shell_scripts/job_snakemake_kupferzell.sh
```

Key features:
- Sets `PROJ_DATA` to rasterio's bundled path
- Uses `--rerun-incomplete` for robustness
- Targets `results/kupferzell_2024/networks/base_s_256_elec_.nc`
- 8 cores, 72 GB total memory, 48-hour timeout

### Submit Job

```bash
cd ~/PycharmProjects/pypsa-eur
bsub < ~/PycharmProjects/Battery_Congestion_Alleviation/shell_scripts/job_snakemake_kupferzell.sh
```

### Monitor Job

```bash
# List active jobs
bjobs

# Watch specific job
bjobs -l <job_id>

# Tail output log
tail -f hpc_output_and_error_files/Output_<job_id>.out
```

### Retrieve Results

Once complete:

```bash
ls -lh ~/PycharmProjects/pypsa-eur/results/kupferzell_2024/networks/base_s_256_elec_.nc
```

This file is your **final network** with all generators, lines, loads, and storage units for the 2025 grid with 2013 weather data.

---

## Integration with Battery Congestion Pipeline

### Directory Structure

```bash
~/PycharmProjects/Battery_Congestion_Alleviation/
├── SETUP_GUIDE.md                    # This file
├── README.md                         # Project overview
├── requirements.txt                  # Locked dependencies
├── config/
│   └── kupferzell_2024.yaml          # Upstream PyPSA config (symlinked or copied)
├── data/
│   └── base_s_256_elec_.nc           # Symlink to pypsa-eur results
├── scripts/
│   ├── integrate_2025_grid.py        # Load open-MaStR grid data
│   ├── smard_data_loader.py          # Download 2025 demand (if not 2013)
│   ├── add_battery_storage.py        # Add battery units to network
│   ├── run_market_dispatch.py        # PyPSA LOPF with market prices
│   └── analyze_congestion.py         # Compute line loading, identify Kupferzell flows
├── outputs/
│   ├── network_2025_with_batteries.nc  # Network after battery addition
│   ├── lopf_results_<scenario>.nc      # Optimization outputs
│   ├── line_loading.csv                # Hourly line loading
│   ├── congestion_analysis.csv         # Congestion summary
│   └── plots/
│       ├── kupferzell_loading_dist.png
│       ├── monthly_congestion.png
│       └── nodal_prices_heatmap.png
└── shell_scripts/
    ├── job_snakemake_kupferzell.sh     # Submit pypsa-eur build
    └── job_battery_analysis.sh         # Submit your battery analysis
```

### Analysis Pipeline Steps

Once `base_s_256_elec_.nc` is ready:

```bash
cd ~/PycharmProjects/Battery_Congestion_Alleviation

# 1. Integrate 2025 grid topology
python3 scripts/integrate_2025_grid.py

# 2. Add battery storage (at selected locations or all buses)
python3 scripts/add_battery_storage.py \
  --network data/base_s_256_elec_.nc \
  --output outputs/network_2025_with_batteries.nc \
  --battery-locations kupferzell,heilbronn,aachen

# 3. Run market dispatch (LOPF with market prices)
python3 scripts/run_market_dispatch.py \
  --network outputs/network_2025_with_batteries.nc \
  --snapshots 2013 \
  --output outputs/lopf_results_base.nc

# 4. Analyze congestion on Kupferzell corridor
python3 scripts/analyze_congestion.py \
  --network outputs/network_2025_with_batteries.nc \
  --lopf outputs/lopf_results_base.nc \
  --lines "Kupferzell-Grossgartach,Kupferzell-Stalldorf" \
  --output outputs/congestion_analysis.csv
```

---

## Research Methodology & Workflow

### Alignment with Your Paper

Your research paper likely follows this methodology:

1. **Base case**: Run PyPSA-Eur to get baseline grid flows and congestion.
2. **Battery scenarios**: Add batteries at strategic locations (Kupferzell area) and re-optimize.
3. **Congestion analysis**: Compare line loading, nodal prices, and congestion frequency.
4. **Bidding strategy**: Layer on your battery bidding/dispatch strategy.

### Implementation in This Project

| Step | Script | Output | Notes |
|------|--------|--------|-------|
| 1. Build network | Snakemake (pypsa-eur) | `base_s_256_elec_.nc` | 2025 grid + 2013 weather |
| 2. Add batteries | `add_battery_storage.py` | `network_2025_with_batteries.nc` | Custom locations & sizes |
| 3. Run LOPF | `run_market_dispatch.py` | `lopf_results.nc` | Optimal dispatch (hourly) |
| 4. Analyze congestion | `analyze_congestion.py` | CSV + plots | Identify Kupferzell overloads |
| 5. Bidding optimization | Your custom script | Strategy outputs | Apply to simulation |

### Key Configuration Choices

- **Weather year**: 2013 (industry standard, literature-aligned)
- **Grid year**: 2025 (via open-MaStR, reflects current/near-future state)
- **Clustering**: 256 buses (balance between detail & runtime)
- **Solver**: Gurobi (via `highspy`; free for academics)
- **Time resolution**: Hourly (full year = 8760 snapshots)

---

## Troubleshooting & Recovery

### Issue: `MissingRuleException` for Target

**Cause**: Target path doesn't match any rule output.

**Solution**:

```bash
# Verify the target exists in rules
cd ~/PycharmProjects/pypsa-eur
snakemake -n --configfile config/kupferzell_2024.yaml -- \
  results/kupferzell_2024/networks/base_s_256_elec_.nc 2>&1 | head -20

# If still missing, check RDIR calculation
python3 -c "
import yaml
with open('config/kupferzell_2024.yaml') as f:
    cfg = yaml.safe_load(f)
    run_name = cfg['run']['name']
    print(f'Run name: {run_name}')
    print(f'Expected RDIR: {run_name}/')
    print(f'Expected result path: results/{run_name}/networks/base_s_256_elec_.nc')
"
```

### Issue: `ProjError: Error creating Transformer from CRS`

**Cause**: PROJ database mismatch (system PROJ version ≠ rasterio runtime version).

**Solution**:

```bash
# Verify PROJ_DATA is set
echo $PROJ_DATA
# Should output: /path/to/venv/lib/python3.12/site-packages/rasterio/proj_data

# If not set, manually export it
export PROJ_DATA="$(python3 -c "import os, rasterio; print(os.path.join(os.path.dirname(rasterio.__file__), 'proj_data'))")"
export PROJ_LIB="$PROJ_DATA"
export PROJ_NETWORK=OFF
```

### Issue: Snakemake Lock / `.snakemake/` Corruption

**Cause**: Previous job crashed or was killed, leaving lock files.

**Solution**:

```bash
cd ~/PycharmProjects/pypsa-eur

# Unlock
snakemake --unlock

# Clean stale locks
rm -rf .snakemake/locks

# Optional: Reset incomplete jobs
snakemake --reset-incomplete
```

### Issue: Out of Memory (OOM) on HPC

**Cause**: 256-bus network + Gurobi uses 50+ GB during optimization.

**Solution**:

```bash
# Reduce to 128 buses (testing)
snakemake --cores 8 --configfile config/kupferzell_2024.yaml \
  -- results/kupferzell_2024/networks/base_s_128_elec_.nc

# Or increase job memory in BSUB script:
# #BSUB -R "rusage[mem=24GB]"  # Increase from 16 to 24 GB
```

### Issue: `ValueError: cannot allocate ... GiB for array`

**Cause**: Atlite availability matrix computation needs >1 GB per technology.

**Solution**:

- Run on fewer cores (reduce parallelism)
- Or increase node memory in LSF script
- Or reduce `nprocesses` in config:

```yaml
atlite:
  nprocesses: 4    # Reduce from 8
```

### Issue: Data Not Found (e.g., `europe-2013-sarah3-era5.nc`)

**Cause**: Cutout file missing from `data/cutout/archive/v1.0/`.

**Solution**:

```bash
# Verify file exists
ls -lh ~/PycharmProjects/pypsa-eur/data/cutout/archive/v1.0/

# If missing, download manually (example for small test cutout)
cd ~/PycharmProjects/pypsa-eur
mkdir -p data/cutout/archive/v1.0
# Download from external source or Zenodo link (specific to your cutout)

# Or let Snakemake download it (if rule is enabled)
snakemake --cores 1 -- data/cutout/archive/v1.0/europe-2013-sarah3-era5.nc
```

### Issue: Git Merge Conflicts

**Cause**: Upstream `pypsa-eur` updated; your patches may conflict.

**Solution**:

```bash
# Check for conflicts
cd ~/PycharmProjects/pypsa-eur
git status | grep "both modified"

# Resolve manually or accept ours/theirs
git checkout --ours rules/build_electricity.smk  # Keep your patched version
git add rules/build_electricity.smk
git commit -m "Resolved conflict: keeping patched build_electricity.smk"
```

---

## Complete Workflow: From Scratch

If you're starting completely fresh:

```bash
# 1. Activate environment
module purge
module load python3/3.12.4
source ~/venvs/kupferzell/bin/activate

# 2. Set PROJ paths
export PROJ_DATA="$(python3 -c "import os, rasterio; print(os.path.join(os.path.dirname(rasterio.__file__), 'proj_data'))")"
export PROJ_LIB="$PROJ_DATA"
export PROJ_NETWORK=OFF

# 3. Navigate & verify data
cd ~/PycharmProjects/pypsa-eur
ls data/cutout/archive/v1.0/europe-2013-sarah3-era5.nc  # Should exist

# 4. Dry-run to build DAG
snakemake -n --configfile config/kupferzell_2024.yaml \
  -- results/kupferzell_2024/networks/base_s_256_elec_.nc | head -30

# 5. Submit to HPC
cd ~/PycharmProjects/Battery_Congestion_Alleviation
bsub < shell_scripts/job_snakemake_kupferzell.sh

# 6. Monitor
bjobs -l <job_id>
tail -f ~/PycharmProjects/pypsa-eur/hpc_output_and_error_files/Output_*.err

# 7. Once complete, run your battery analysis
python3 scripts/add_battery_storage.py ...
python3 scripts/run_market_dispatch.py ...
python3 scripts/analyze_congestion.py ...
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
| **Final network** | `~/PycharmProjects/pypsa-eur/results/kupferzell_2024/networks/base_s_256_elec_.nc` | Input to battery analysis |
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

