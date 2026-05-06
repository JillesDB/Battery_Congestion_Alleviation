#!/usr/bin/env bash
#BSUB -q hpc
#BSUB -J battery_alpha_assessment
#BSUB -n 1
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=4GB]"
#BSUB -M 4GB
#BSUB -W 1:00
#BSUB -o hpc_output_and_error_files/Output_Battery_Alpha_Assessment_%J.out
#BSUB -e hpc_output_and_error_files/Output_Battery_Alpha_Assessment_%J.err

set -euo pipefail
unset GRB_LICENSE_FILE
export GRB_LICENSE_FILE=$HOME/gurobi/gurobi.lic

# ┌─────────────────────────────────────────────────────────────────────────────
# │  TOGGLES — Edit before submitting
# ├─────────────────────────────────────────────────────────────────────────────
SCENARIO="kupferzell_full"                   # kupferzell_simple | kupferzell_full
ALLOCATION_METHOD="optimal_revenue"          # temporal | tso_priority | optimal_revenue
ALLEVIATION_METHOD="dynamic_multiple_lines"  # flat_one_line | dynamic_one_line | dynamic_multiple_lines
SIM_YEAR="2025"
# └─────────────────────────────────────────────────────────────────────────────

# ── Scratch: /work3 (DTU HPC persistent scratch, outside /zhome) ──────────────
# LSF sets $LSB_JOBID at runtime; the fallback ensures local test runs work.
JOB_ID="${LSB_JOBID:-local_$$}"
SCRATCH_ROOT="/work3/$(id -u)/battery_alpha_assessment_${JOB_ID}"
mkdir -p "${SCRATCH_ROOT}"

# ── Fixed project paths ───────────────────────────────────────────────────────
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"
NPV_SCRIPT="${PROJECT_DIR}/battery_profitability.py"
RESULTS_ROOT="${PROJECT_DIR}/results"

# ── Environment setup ─────────────────────────────────────────────────────────
module purge || true
module load python3/3.12.4 || true

source "${VENV_ACTIVATE}"
cd "${PROJECT_DIR}"
mkdir -p hpc_output_and_error_files

# Map alleviation method → merchant method tag (same convention as other jobs)
case "${ALLEVIATION_METHOD}" in
    flat_one_line)               MERCHANT_METHOD_TAG="flat_one_line" ;;
    dynamic_one_line)            MERCHANT_METHOD_TAG="dynamic_one_line" ;;
    dynamic_multiple_lines)      MERCHANT_METHOD_TAG="dynamic_multiple_lines" ;;
    simple)                      MERCHANT_METHOD_TAG="flat_one_line" ;;
    one_line)                    MERCHANT_METHOD_TAG="dynamic_one_line" ;;
    optimal|optimal_alleviation) MERCHANT_METHOD_TAG="dynamic_multiple_lines" ;;
    *) echo "ERROR: Unknown ALLEVIATION_METHOD=${ALLEVIATION_METHOD}" >&2; exit 1 ;;
esac

echo "════════════════════════════════════════════════════════════════════════════"
echo "  BATTERY ALPHA PROFITABILITY ASSESSMENT  — Kupferzell GridBooster"
echo "════════════════════════════════════════════════════════════════════════════"
echo "Host                 : $(hostname)"
echo "Date                 : $(date)"
echo "Python               : $(which python3)"
echo "Scenario             : ${SCENARIO}"
echo "Allocation method    : ${ALLOCATION_METHOD}"
echo "Alleviation method   : ${ALLEVIATION_METHOD}"
echo "Merchant tag         : ${MERCHANT_METHOD_TAG}"
echo "Simulation year      : ${SIM_YEAR}"
echo "Alpha range          : 0.1 → 1.0 (step 0.1)"
echo "Scratch dir          : ${SCRATCH_ROOT}"
echo "════════════════════════════════════════════════════════════════════════════"
echo ""

python3 "${NPV_SCRIPT}" \
    --scenario             "${SCENARIO}" \
    --allocation-method    "${ALLOCATION_METHOD}" \
    --merchant-method-tag  "${MERCHANT_METHOD_TAG}" \
    --year                 "${SIM_YEAR}" \
    --results-root         "${RESULTS_ROOT}" \
    --mode                 alpha_assessment \
    --scratch-root         "${SCRATCH_ROOT}"

# ── Clean up scratch (intermediate per-alpha CSVs no longer needed) ───────────
echo ""
echo "Removing scratch directory: ${SCRATCH_ROOT}"
rm -rf "${SCRATCH_ROOT}"

echo ""
echo "════════════════════════════════════════════════════════════════════════════"
echo "Job completed successfully."
echo "Outputs:"
echo "  ${RESULTS_ROOT}/${SCENARIO}/6_npv_calculation/"
echo "    alpha_assessment_${ALLOCATION_METHOD}_${MERCHANT_METHOD_TAG}_${SIM_YEAR}.csv"
echo "    alpha_assessment_${ALLOCATION_METHOD}_${MERCHANT_METHOD_TAG}_${SIM_YEAR}.png"
echo "════════════════════════════════════════════════════════════════════════════"
