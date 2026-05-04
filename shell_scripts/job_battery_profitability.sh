#!/usr/bin/env bash
#BSUB -q hpc
#BSUB -J battery_profitability
#BSUB -n 1
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=2GB]"
#BSUB -M 2GB
#BSUB -W 0:30
#BSUB -o hpc_output_and_error_files/Output_Battery_Profitability_%J.out
#BSUB -e hpc_output_and_error_files/Output_Battery_Profitability_%J.err

set -euo pipefail
unset GRB_LICENSE_FILE
export GRB_LICENSE_FILE=$HOME/gurobi/gurobi.lic

# ┌─────────────────────────────────────────────────────────────────────────────
# │  TOGGLES — Edit before submitting
# ├─────────────────────────────────────────────────────────────────────────────
SCENARIO="kupferzell_full"                 # kupferzell_simple | kupferzell_full
ALLOCATION_METHOD="optimal_revenue"           # temporal | tso_priority | optimal_revenue
ALLEVIATION_METHOD="dynamic_multiple_lines"  # flat_one_line | dynamic_one_line | dynamic_multiple_lines
SIM_YEAR="2025"
# └─────────────────────────────────────────────────────────────────────────────

# ── Fixed project paths[cite: 7] ─────────────────────────────────────────────
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"
NPV_SCRIPT="${PROJECT_DIR}/battery_profitability.py"
RESULTS_ROOT="${PROJECT_DIR}/results"

# ── Environment setup[cite: 7] ───────────────────────────────────────────────
module purge || true
module load python3/3.12.4 || true

source "${VENV_ACTIVATE}"
cd "${PROJECT_DIR}"

mkdir -p hpc_output_and_error_files

# Map alleviation method to the file-stem used by merchant_revenues.py[cite: 7]
case "${ALLEVIATION_METHOD}" in
    flat_one_line)             MERCHANT_METHOD_TAG="flat_one_line" ;;
    dynamic_one_line)          MERCHANT_METHOD_TAG="dynamic_one_line" ;;
    dynamic_multiple_lines)    MERCHANT_METHOD_TAG="dynamic_multiple_lines" ;;
    simple)                    MERCHANT_METHOD_TAG="flat_one_line" ;;
    one_line)                  MERCHANT_METHOD_TAG="dynamic_one_line" ;;
    optimal|optimal_alleviation) MERCHANT_METHOD_TAG="dynamic_multiple_lines" ;;
    *) echo "ERROR: Unknown ALLEVIATION_METHOD=${ALLEVIATION_METHOD}" >&2; exit 1 ;;
esac

echo "════════════════════════════════════════════════════════════════════════════"
echo "  BATTERY PROFITABILITY (NPV)  — Kupferzell GridBooster"
echo "════════════════════════════════════════════════════════════════════════════"
echo "Host                 : $(hostname)"
echo "Scenario             : ${SCENARIO}"
echo "Allocation method    : ${ALLOCATION_METHOD}"
echo "Alleviation method   : ${ALLEVIATION_METHOD}"
echo "Merchant tag         : ${MERCHANT_METHOD_TAG}"
echo "Simulation year      : ${SIM_YEAR}"
echo "════════════════════════════════════════════════════════════════════════════"
echo ""

# Execute python script
python3 "${NPV_SCRIPT}" \
    --scenario "${SCENARIO}" \
    --allocation-method "${ALLOCATION_METHOD}" \
    --merchant-method-tag "${MERCHANT_METHOD_TAG}" \
    --year "${SIM_YEAR}" \
    --results-root "${RESULTS_ROOT}"

echo ""
echo "════════════════════════════════════════════════════════════════════════════"
echo "Job completed successfully."
echo "════════════════════════════════════════════════════════════════════════════"