#!/usr/bin/env bash
### General options
### -- specify queue --
#BSUB -q man
### -- set the job Name --
#BSUB -J PyPSA_Snakemake_Run
### -- ask for number of cores (default: 1) --
#BSUB -n 8
### -- specify that the cores must be on the same host --
#BSUB -R "span[hosts=1]"
### -- specify that we need 8GB of memory per core/slot --
#BSUB -R "rusage[mem=16GB]"
### -- specify memory limit --
#BSUB -M 16GB
### -- set walltime limit: hh:mm --
#BSUB -W 72:00
### -- set the email address --
#BSUB -u jcdbl@dtu.dk
### -- Specify the output and error file. %J is the job-id --
#BSUB -o hpc_output_and_error_files/Output_PyPSA_def_%J.out
#BSUB -e hpc_output_and_error_files/Output_PyPSA_def_%J.err

# 1. Standard Environment Cleanup
module purge
module load python3/3.12.4

# 2. Activate the correct venv (kupferzell)
source /zhome/26/e/209460/venvs/kupferzell/bin/activate

# 3. Navigate to the PyPSA factory folder (Sibling Directory)
cd ~/PycharmProjects/pypsa-eur || exit 1

# 4. Set PYTHONPATH and PROJ paths
# Use rasterio's bundled PROJ database (layout minor=6), which matches
# the rasterio runtime used by atlite in this environment.
PROJ_DATA="$(python3 - <<'PY'
import os
import rasterio
print(os.path.join(os.path.dirname(rasterio.__file__), 'proj_data'))
PY
)"
export PROJ_DATA
export PROJ_LIB="$PROJ_DATA"
export PROJ_NETWORK=OFF  # prevents pyproj trying to download datum grids
export PYTHONPATH="$PWD:$PYTHONPATH"
unset GRB_LICENSE_FILE
export GRB_LICENSE_FILE=$HOME/gurobi/gurobi.lic
export ENTSOE_API_TOKEN="c2f8b11c-ec54-45ad-9223-f1f4d24bb427"

# 5. Create logs directory if it doesn't exist
mkdir -p logs

# 6. Debug Info
echo "=== JOB INFO ==="
hostname
date
echo "Python: $(which python3)"
echo "PROJ_DATA: $PROJ_DATA"

# 7. Execute Snakemake
# Using --profile if you have one, or --cores explicitly
snakemake \
  --cores 8 \
  --configfile /zhome/26/e/209460/PycharmProjects/pypsa-eur/config/config.default.yaml \
  --rerun-incomplete \
  --rerun-triggers mtime params \
  --nolock \
  results/networks/base_s_256_elec_.nc
#snakemake --cores 8 resources/cutouts/central-europe-2025-era5.nc --configfile config/config.default.yaml
#  --forceall \