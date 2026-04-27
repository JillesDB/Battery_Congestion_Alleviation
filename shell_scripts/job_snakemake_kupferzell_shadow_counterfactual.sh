#!/usr/bin/env bash
### General options
#BSUB -q man
#BSUB -J PyPSA_Kupf_full_shadow_cf
#BSUB -n 16
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=8GB]"
#BSUB -M 8GB
#BSUB -W 48:00
#BSUB -u jcdbl@dtu.dk
#BSUB -o hpc_output_and_error_files/Output_PyPSA_Kupf_full_shadow_cf_%J.out
#BSUB -e hpc_output_and_error_files/Output_PyPSA_Kupf_full_shadow_cf_%J.err

set -euo pipefail

module purge
module load python3/3.12.4

source /zhome/26/e/209460/venvs/kupferzell/bin/activate

cd ~/PycharmProjects/pypsa-eur || exit 1

PROJ_DATA="$(python3 - <<'PY'
import os
import rasterio
print(os.path.join(os.path.dirname(rasterio.__file__), 'proj_data'))
PY
)"
export PROJ_DATA
export PROJ_LIB="$PROJ_DATA"
export PROJ_NETWORK=OFF
export PYTHONPATH="$PWD:$PYTHONPATH"
unset GRB_LICENSE_FILE
export GRB_LICENSE_FILE="$HOME/gurobi/gurobi.lic"

mkdir -p hpc_output_and_error_files

echo "=== JOB INFO ==="
hostname
date
echo "Python: $(which python3)"
echo "PROJ_DATA: $PROJ_DATA"
echo "Cores visible: $(nproc)"


snakemake \
  --cores 8 \
  --configfile config/kupferzell_2024_simple_shadow_counterfactual_jan2025.yaml \
  --rerun-triggers params mtime \
  --latency-wait 120 \
  -R solve_network \
  --nolock \
  --rerun-incomplete \
  results/kupferzell_2024_simple_shadow_counterfactual_jan2025/networks/base_s_256_elec_.nc

#snakemake \
#  --cores 16 \
#  --configfile config/kupferzell_2024_full_shadow_counterfactual_2025.yaml \
#  --rerun-incomplete \
#  --rerun-triggers mtime \
#  --forceall \
#  --latency-wait 120 \
#  -- \
#  results/kupferzell_2024_full_shadow_counterfactual_2025/networks/base_s_256_elec_.nc
