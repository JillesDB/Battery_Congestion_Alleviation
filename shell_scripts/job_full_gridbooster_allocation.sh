#!/usr/bin/env bash
#BSUB -q hpc
#BSUB -J full_income_estimation
#BSUB -n 2
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=4GB]"
#BSUB -M 4GB
#BSUB -W 1:00
#BSUB -o hpc_output_and_error_files/Output_Full_Income_Estimation_%J.out
#BSUB -e hpc_output_and_error_files/Output_Full_Income_Estimation_%J.err

set -euo pipefail
unset GRB_LICENSE_FILE
export GRB_LICENSE_FILE=$HOME/gurobi/gurobi.lic
# ══════════════════════════════════════════════════════════════════════════════
# FULL INCOME ESTIMATION — combines TSO congestion-relief + merchant revenue
# under one (allocation_method × alleviation_method) combination per submission.
#
# Allocation methods (TSO vs merchant decision rule):
#   temporal        — months pre-assigned via TEMPORAL_ALLOCATION JSON.
#   tso_priority    — every congested hour → TSO; remainder → constrained merchant.
#   optimal_revenue — daily comparison; pick whichever yields more.
#
# Alleviation method only chooses which congestion-relief stream is consumed
# (simple | one_line | optimal_alleviation). The merchant CSV that gets
# loaded is auto-derived:
#   * temporal       → unconstrained merchant CSV
#   * tso_priority   → tso_constrained_<method> merchant CSV
#   * optimal_revenue → BOTH (compares them daily)
#
# Prerequisites:
#   1. job_congestion_alleviation.sh has been run for ALL three alleviation
#      methods (so alleviation_revenues_merged_{year}.csv exists).
#   2. job_estimate_merchant_revenues.sh has been run for the same scenario
#      (so the required merchant CSV(s) exist).
# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────────
# │  TOGGLES — the only lines you need to edit before submitting
# ├─────────────────────────────────────────────────────────────────────────────
SCENARIO="simple"                 # simple | full
ALLOCATION_METHOD="optimal_revenue"   # temporal | tso_priority | optimal_revenue
ALLEVIATION_METHOD="optimal_alleviation"  # simple | one_line | optimal_alleviation
SIM_YEAR="2025"

# Used only when ALLOCATION_METHOD=temporal. Each month must appear in exactly
# one list. Edit the split as needed for the paper's seasonal scenarios.
TEMPORAL_ALLOCATION='{"tso":["nov","dec","jan","feb","mar"],"merchant":["apr","may","jun","jul","aug","sep","oct"]}'
# └─────────────────────────────────────────────────────────────────────────────

# ── Fixed project paths ──────────────────────────────────────────────────────
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"
ALLOCATION_SCRIPT="${PROJECT_DIR}/gridbooster_allocation_model.py"
RESULTS_ROOT="${PROJECT_DIR}/results"

# ── Environment setup ────────────────────────────────────────────────────────
module purge || true
module load python3/3.12.4 || true

source "${VENV_ACTIVATE}"
cd "${PROJECT_DIR}"

mkdir -p hpc_output_and_error_files
mkdir -p "${RESULTS_ROOT}/final_allocation/kupferzell_${SCENARIO}"

# Map alleviation method to the file-stem used by merchant_revenues.py
case "${ALLEVIATION_METHOD}" in
    simple)              MERCHANT_METHOD_TAG="simple" ;;
    one_line)            MERCHANT_METHOD_TAG="one_line" ;;
    optimal|optimal_alleviation) MERCHANT_METHOD_TAG="optimal" ;;
    *) echo "ERROR: Unknown ALLEVIATION_METHOD=${ALLEVIATION_METHOD}" >&2; exit 1 ;;
esac

ALLEVIATION_MERGED="${RESULTS_ROOT}/kupferzell_${SCENARIO}/congestion_alleviation/alleviation_revenues_merged_${SIM_YEAR}.csv"
MERCHANT_UNCON="${RESULTS_ROOT}/merchant_revenues/kupferzell_${SCENARIO}/dam_merchant_revenues_unconstrained_${SIM_YEAR}.csv"
MERCHANT_CON="${RESULTS_ROOT}/merchant_revenues/kupferzell_${SCENARIO}/dam_merchant_revenues_tso_constrained_${MERCHANT_METHOD_TAG}_${SIM_YEAR}.csv"

echo "════════════════════════════════════════════════════════════════════════════"
echo "  FULL INCOME ESTIMATION  — Kupferzell GridBooster"
echo "════════════════════════════════════════════════════════════════════════════"
echo "Host                 : $(hostname)"
echo "Date                 : $(date)"
echo "Python               : $(which python3)"
echo "Scenario             : ${SCENARIO}"
echo "Allocation method    : ${ALLOCATION_METHOD}"
echo "Alleviation method   : ${ALLEVIATION_METHOD}"
echo "Simulation year      : ${SIM_YEAR}"
[[ "${ALLOCATION_METHOD}" == "temporal" ]] && \
    echo "Temporal allocation  : ${TEMPORAL_ALLOCATION}"
echo "Alleviation merge    : ${ALLEVIATION_MERGED}"
echo "Merchant (uncon.)    : ${MERCHANT_UNCON}"
echo "Merchant (constr.)   : ${MERCHANT_CON}"
echo "════════════════════════════════════════════════════════════════════════════"
echo ""

# ── Input validation ─────────────────────────────────────────────────────────
if [[ ! -f "${ALLEVIATION_MERGED}" ]]; then
    echo "ERROR: Merged alleviation CSV not found: ${ALLEVIATION_MERGED}" >&2
    echo "       Run job_congestion_alleviation.sh for all three methods first." >&2
    exit 1
fi

case "${ALLOCATION_METHOD}" in
    temporal)
        if [[ ! -f "${MERCHANT_UNCON}" ]]; then
            echo "ERROR: Unconstrained merchant CSV not found: ${MERCHANT_UNCON}" >&2
            echo "       Run job_estimate_merchant_revenues.sh first." >&2
            exit 1
        fi
        ;;
    tso_priority)
        if [[ ! -f "${MERCHANT_CON}" ]]; then
            echo "ERROR: Constrained merchant CSV not found: ${MERCHANT_CON}" >&2
            echo "       Run job_estimate_merchant_revenues.sh with the same" >&2
            echo "       SCENARIO and ALLEVIATION_METHODS first." >&2
            exit 1
        fi
        ;;
    optimal_revenue)
        for f in "${MERCHANT_UNCON}" "${MERCHANT_CON}"; do
            if [[ ! -f "${f}" ]]; then
                echo "ERROR: Required merchant CSV not found: ${f}" >&2
                echo "       optimal_revenue needs both unconstrained and " >&2
                echo "       tso_constrained merchant runs." >&2
                exit 1
            fi
        done
        ;;
    *)
        echo "ERROR: Unknown ALLOCATION_METHOD=${ALLOCATION_METHOD}" >&2
        echo "       Choose: temporal | tso_priority | optimal_revenue" >&2
        exit 1
        ;;
esac

# ══════════════════════════════════════════════════════════════════════════════
# RUN ALLOCATION MODEL
# ══════════════════════════════════════════════════════════════════════════════
if [[ "${ALLOCATION_METHOD}" == "temporal" ]]; then
    python3 "${ALLOCATION_SCRIPT}" \
        --scenario           "${SCENARIO}" \
        --allocation-method  "${ALLOCATION_METHOD}" \
        --alleviation-method "${ALLEVIATION_METHOD}" \
        --year               "${SIM_YEAR}" \
        --results-root       "${RESULTS_ROOT}" \
        --temporal-allocation "${TEMPORAL_ALLOCATION}"
else
    python3 "${ALLOCATION_SCRIPT}" \
        --scenario           "${SCENARIO}" \
        --allocation-method  "${ALLOCATION_METHOD}" \
        --alleviation-method "${ALLEVIATION_METHOD}" \
        --year               "${SIM_YEAR}" \
        --results-root       "${RESULTS_ROOT}"
fi

echo ""
echo "════════════════════════════════════════════════════════════════════════════"
echo "Job completed successfully."
echo "Outputs:"
echo "  ${RESULTS_ROOT}/final_allocation/kupferzell_${SCENARIO}/"
echo "    allocation_${ALLOCATION_METHOD}_${MERCHANT_METHOD_TAG}_${SIM_YEAR}.csv"
echo "    allocation_${ALLOCATION_METHOD}_${MERCHANT_METHOD_TAG}_${SIM_YEAR}_kpi.csv"
echo "════════════════════════════════════════════════════════════════════════════"