#!/usr/bin/env bash
#BSUB -q man
#BSUB -J kupf_full_shadow_cf
#BSUB -n 16
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=8GB]"
#BSUB -M 8GB
#BSUB -W 48:00
#BSUB -o hpc_output_and_error_files/Output_Kupf_Full_Shadow_CF_%J.out
#BSUB -e hpc_output_and_error_files/Output_Kupf_Full_Shadow_CF_%J.err

set -euo pipefail

PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
DEFAULT_CONFIG="$PROJECT_DIR/counterfactual_configs/kupferzell_full_shadow_counterfactual_2025.yaml"

exec "$PROJECT_DIR/shell_scripts/job_shadow_price_counterfactual_kupferzell_simple.sh" "$DEFAULT_CONFIG"
