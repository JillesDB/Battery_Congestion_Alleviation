# PyPSA-Eur + Battery Congestion Alleviation — Complete Setup Guide

**Version**: 2.0 (April 2026)  
**Target Environment**: DTU HPC (Linux, `/zhome` storage)  
**Research Focus**: Grid congestion analysis with battery optimization using 2025 grid & demand data, 2013 weather year (industry standard)

---

## Table of Contents

1. [Overview & Key Learnings](#overview--key-learnings)
2. [Research Aims & Study Scope](#research-aims--study-scope)
3. [Pre-Requisites & Environment](#pre-requisites--environment)
4. [HPC Storage & Git Workflow](#hpc-storage--git-workflow)
5. [Virtual Environment Setup](#virtual-environment-setup)
6. [PyPSA-Eur Installation & Configuration](#pypsa-eur-installation--configuration)
7. [Data Management](#data-management)
8. [Critical Patches & Fixes](#critical-patches--fixes)
9. [Running PyPSA-Eur on HPC](#running-pypsa-eur-on-hpc)
10. [Integration with Battery Congestion Pipeline](#integration-with-battery-congestion-pipeline)
11. [Research Methodology & Workflow](#research-methodology--workflow)
12. [Troubleshooting & Recovery](#troubleshooting--recovery)

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

## Research Aims & Study Scope

### Primary Research Aim

Quantify **hourly congestion occurrence** on Kupferzell-area 380 kV corridor lines with PyPSA-Eur dispatch and calibrate the analysis framework to SMARD-era operating conditions.

### Methodological Scope (Implemented)

- **Model family**: PyPSA-Eur (cross-border physics preserved)
- **Weather year**: 2013 (literature-standard weather year)
- **Infrastructure/economic context**: 2025 (planning horizon, renewable capacity assumptions, cost year)
- **Grid representation for congestion counting**: fixed existing transmission (`electricity.transmission_limit: v1.0`)
- **Spatial strategy**: heterogeneous clustering at 256 buses with DE/DK focus and single-node-style neighbors via zero focus weights
- **Operational paradigm**: overnight foresight, linear dispatch-style solve (`linearized_unit_commitment: false`)

### Congestion Metric Used in Downstream Analysis

A line is flagged congested in hour `t` if:

`abs(p0_l,t) / s_nom_l >= 0.98`

This keeps a 2% tolerance for LP numerical precision while preserving a strict corridor-overload criterion.

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
│   │   ├── config/                       # Configs (kupferzell_2024_simple/full.yaml)
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

### Configuration Files Used in This Project

Use two project configs in `pypsa-eur/config/`:

- `kupferzell_2024_full.yaml` -> publication/full-study run (full 2013 year)
- `kupferzell_2024_simple.yaml` -> fast validation run (Jan 2013 subset)

Create them from defaults once:

```bash
cd ~/PycharmProjects/pypsa-eur
cp config/config.default.yaml config/kupferzell_2024_full.yaml
cp config/config.default.yaml config/kupferzell_2024_simple.yaml
```

### Finalized Full-Study Settings (`kupferzell_2024_full.yaml`)

The following settings are now fixed for the congestion study and should be treated as canonical:

- `run.name: kupferzell_2024_full`
- `run.shared_resources.policy: false` (avoid stale-resource cross-run leakage)
- `foresight: overnight`
- `scenario.clusters: [256]`
- `scenario.planning_horizons: [2025]`
- `snapshots: 2013-01-01 .. 2013-12-31` (inclusive left)
- `electricity.transmission_limit: v1.0` (existing grid only)
- `electricity.custom_powerplants: false`
- `electricity.powerplants_filter: (DateOut >= 2026 or DateOut != DateOut) and (DateIn < 2025 or DateIn != DateIn)`
- `clustering.focus_weights`:
  - `DE: 0.85`
  - `DK: 0.15`
  - `AT/BE/CH/CZ/FR/NL/PL: 0.0`
- `costs.year: 2025`
- `energy.energy_totals_year: 2024`
- `conventional.biomass.p_min_pu: 0.4`
- `solving.options.load_shedding.enable: false`
- `solving.options.linearized_unit_commitment: false`
- `solving.options.custom_extra_functionality: data/custom_extra_functionality.py`
- `atlite.default_cutout: europe-2013-sarah3-era5`
- `atlite.cutouts.*.prepare_kwargs.tmpdir: /zhome/26/e/209460/PycharmProjects/pypsa-eur/cutouts_tmp/`

### Finalized Validation Settings (`kupferzell_2024_simple.yaml`)

`kupferzell_2024_simple.yaml` is aligned to the same methodology, but intentionally reduced runtime:

- same core assumptions as full (`foresight`, `transmission_limit`, `focus_weights`, costs, powerplants, solver style)
- `run.name: kupferzell_2024_simple`
- `snapshots: 2013-01-01 .. 2013-01-31`

This file is used to test methodological changes before promoting them into full-year execution.

### Verify Configuration

```bash
cd ~/PycharmProjects/pypsa-eur
snakemake -n --configfile config/kupferzell_2024_simple.yaml -- resources/kupferzell_2024_simple/regions_onshore_base_s_256.geojson
snakemake -n --configfile config/kupferzell_2024_full.yaml -- resources/kupferzell_2024_full/regions_onshore_base_s_256.geojson
```

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

- `build_powerplants` writes: `resources/<run_name>/powerplants_s_256.csv`
- `add_electricity` writes: `resources/<run_name>/networks/base_s_256_elec.nc`
- `prepare_network` writes: `resources/<run_name>/networks/base_s_256_elec_.nc`
- `solve_network` writes solved outputs under `results/<run_name>/...`

Use `<run_name> = kupferzell_2024_simple` or `<run_name> = kupferzell_2024_full`.

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
# Simple run artifacts
ls -lh resources/kupferzell_2024_simple/powerplants_s_256.csv
ls -lh resources/kupferzell_2024_simple/networks/base_s_256_elec_.nc
ls -lh results/kupferzell_2024_simple/networks/

# Full run artifacts
ls -lh resources/kupferzell_2024_full/powerplants_s_256.csv
ls -lh resources/kupferzell_2024_full/networks/base_s_256_elec_.nc
ls -lh results/kupferzell_2024_full/networks/
```

### Custom Powerplants Checks (Critical)

If CCGT/OCGT or hydro look wrong, validate these before rerun:

1. In `config/kupferzell_2024_full.yaml` (and mirror in `config/kupferzell_2024_simple.yaml`), avoid excluding Germany unintentionally:
   - bad: `Country != 'DE'`
   - expected for DE-focused run: include Germany in the powerplant pool (or leave country clause out)
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

**Typical cause in this setup**: misconfigured `powerplants_filter` in `config/kupferzell_2024_full.yaml` (or `config/kupferzell_2024_simple.yaml`) and/or gas technology classification in `build_custom_powerplants_2025.py`.

**Quick checks:**

```bash
# Check filter
grep -n "powerplants_filter" config/kupferzell_2024_full.yaml
grep -n "powerplants_filter" config/kupferzell_2024_simple.yaml

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
module purge
module load python3/3.12.4
source ~/venvs/kupferzell/bin/activate
export PROJ_DATA="$(python3 -c "import os, rasterio; print(os.path.join(os.path.dirname(rasterio.__file__), 'proj_data'))")"
export PROJ_LIB="$PROJ_DATA"
export PROJ_NETWORK=OFF

cd ~/PycharmProjects/pypsa-eur
mkdir -p cutouts_tmp

# Fast validation run (simple)
snakemake -n --cores 1 --profile profiles/hpc --configfile config/kupferzell_2024_simple.yaml -- \
  results/kupferzell_2024_simple/networks/base_s_256_elec_.nc

# Full study run
snakemake -n --cores 1 --profile profiles/hpc --configfile config/kupferzell_2024_full.yaml -- \
  results/kupferzell_2024_full/networks/base_s_256_elec_.nc
```

---

## Key Files & Locations (Reference)

| Purpose | Path | Notes |
|---------|------|-------|
| **This guide** | `~/PycharmProjects/Battery_Congestion_Alleviation/SETUP_GUIDE.md` | Research protocol and setup |
| **Simple config** | `~/PycharmProjects/pypsa-eur/config/kupferzell_2024_simple.yaml` | Fast pre-integration validation |
| **Full config** | `~/PycharmProjects/pypsa-eur/config/kupferzell_2024_full.yaml` | Canonical full-study run |
| **open-MaStR data** | `~/.open-MaStR/` | 2025 grid topology source |
| **Simple pre-solve network** | `~/PycharmProjects/pypsa-eur/resources/kupferzell_2024_simple/networks/base_s_256_elec_.nc` | Diagnostics/test analysis |
| **Full pre-solve network** | `~/PycharmProjects/pypsa-eur/resources/kupferzell_2024_full/networks/base_s_256_elec_.nc` | Main study diagnostics |
| **Simple solved outputs** | `~/PycharmProjects/pypsa-eur/results/kupferzell_2024_simple/networks/` | Test-run solve outputs |
| **Full solved outputs** | `~/PycharmProjects/pypsa-eur/results/kupferzell_2024_full/networks/` | Publication-run solve outputs |

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
**Last Updated**: April 15, 2026  
**Maintained By**: Your Name  
**Status**: Production-Ready
