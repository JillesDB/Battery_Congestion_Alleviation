#!/usr/bin/env bash
### General options
### -- specify queue --
#BSUB -q man
### -- set the job Name --
#BSUB -J PyPSA_Snakemake_Run
### -- ask for number of cores (default: 1) --
#BSUB -n 16
### -- specify that the cores must be on the same host --
#BSUB -R "span[hosts=1]"
### -- specify that we need 8GB of memory per core/slot --
#BSUB -R "rusage[mem=8GB]"
### -- specify memory limit --
#BSUB -M 8GB
### -- set walltime limit: hh:mm --
#BSUB -W 48:00
### -- set the email address --
#BSUB -u jcdbl@dtu.dk
### -- Specify the output and error file. %J is the job-id --
#BSUB -o hpc_output_and_error_files/Output_%J.out
#BSUB -e hpc_output_and_error_files/Output_%J.err

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
# 5. Create logs directory if it doesn't exist
mkdir -p hpc_output_and_error_files

# 6. Debug Info
echo "=== JOB INFO ===" && hostname && date
echo "Python: $(which python3)"
echo "PROJ_DATA: $PROJ_DATA"
echo "Cores visible: $(nproc)"

# 7. Execute Snakemake
# Build only the kupferzell network target; Snakemake will fetch/generate
# prerequisites (cutouts, regions, availability matrices) as needed.
snakemake \
  --cores 16 \
  --configfile config/kupferzell_2024_full.yaml \
  --rerun-incomplete \
  --rerun-triggers mtime \
  --latency-wait 120 \
  -- \
  results/kupferzell_2024_full/networks/base_s_256_elec_.nc
