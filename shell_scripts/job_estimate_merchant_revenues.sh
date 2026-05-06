#!/usr/bin/env bash
#BSUB -q hpc
#BSUB -J merchant_revenues
#BSUB -n 4
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=8GB]"
#BSUB -M 8GB
#BSUB -W 4:00
#BSUB -o hpc_output_and_error_files/Output_Merchant_Revenues_%J.out
#BSUB -e hpc_output_and_error_files/Output_Merchant_Revenues_%J.err

set -euo pipefail
unset GRB_LICENSE_FILE
export GRB_LICENSE_FILE=$HOME/gurobi/gurobi.lic
# ══════════════════════════════════════════════════════════════════════════════
# DAY-AHEAD SPOT-MARKET MERCHANT REVENUES — GridBooster (Kupferzell, 250 MW)
#
# Solves 365 daily 24h LPs (perfect-foresight, price-taker) for:
#   1. UNCONSTRAINED  — battery free to optimise revenue.
#   2. TSO-CONSTRAINED — TSO hours (from merged alleviation CSV) lock
#                        SoC = E_nom and disable charge/discharge.
#                        One run per alleviation_method.
#
# Prerequisites:
#   - data/germany_hourly_spot_prices.csv (Time_CET column, price column)
#   - For tso_constrained: alleviation_revenues_merged_{year}.csv must
#     already exist for the chosen scenario (produced as the merge step
#     at the end of job_congestion_alleviation.sh).
#
# Solver: scipy HiGHS (license-free). No Gurobi needed for these LPs.
# Runtime: ~5 minutes per regime per scenario (4 LPs × 365 days).
# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────────
# │  TOGGLES
# ├─────────────────────────────────────────────────────────────────────────────
SCENARIO="kupferzell_full"             # kupferzell_simple | kupferzell_full
SIM_YEAR="2025"

# Which TSO-constrained variants to compute. Leave all three for full coverage;
# comment lines out to skip. The unconstrained run is always executed once.
ALLEVIATION_METHODS=( "flat_one_line" "dynamic_one_line" "dynamic_multiple_lines" )
# └─────────────────────────────────────────────────────────────────────────────

# ── Fixed project paths ──────────────────────────────────────────────────────
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"
MERCHANT_SCRIPT="${PROJECT_DIR}/4_merchant_revenues.py"
PRICES_CSV="${PROJECT_DIR}/data/germany_hourly_spot_prices.csv"
RESULTS_ROOT="${PROJECT_DIR}/results"
ALLEVIATION_MERGED="${RESULTS_ROOT}/${SCENARIO}/3_congestion_alleviation/alleviation_revenues_merged_${SIM_YEAR}.csv"

# ── Environment setup ────────────────────────────────────────────────────────
module purge || true
module load python3/3.12.4 || true

source "${VENV_ACTIVATE}"
cd "${PROJECT_DIR}"

mkdir -p hpc_output_and_error_files
mkdir -p "${RESULTS_ROOT}/${SCENARIO}/4_merchant_revenues"

echo "════════════════════════════════════════════════════════════════════════════"
echo "  MERCHANT REVENUES  — Kupferzell GridBooster ${SCENARIO}"
echo "════════════════════════════════════════════════════════════════════════════"
echo "Host              : $(hostname)"
echo "Date              : $(date)"
echo "Python            : $(which python3)"
echo "Scenario          : ${SCENARIO}"
echo "Simulation year   : ${SIM_YEAR}"
echo "Prices CSV        : ${PRICES_CSV}"
echo "Alleviation merge : ${ALLEVIATION_MERGED}"
echo "Methods (constr.) : ${ALLEVIATION_METHODS[*]}"
echo "════════════════════════════════════════════════════════════════════════════"
echo ""

# ── Input validation ─────────────────────────────────────────────────────────
if [[ ! -f "${PRICES_CSV}" ]]; then
    echo "ERROR: Spot price CSV not found: ${PRICES_CSV}" >&2
    exit 1
fi

# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — UNCONSTRAINED daily LPs
# ══════════════════════════════════════════════════════════════════════════════
echo "[STEP 1] UNCONSTRAINED merchant revenues"
echo "  365 independent 24h LPs, perfect foresight, daily cyclic SoC=E_nom."
echo ""

python3 "${MERCHANT_SCRIPT}" \
    --mode         unconstrained \
    --scenario     "${SCENARIO}" \
    --year         "${SIM_YEAR}" \
    --prices-csv   "${PRICES_CSV}" \
    --results-root "${RESULTS_ROOT}"

echo ""
echo "[STEP 1] UNCONSTRAINED complete."
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — TSO-CONSTRAINED daily LPs (one run per alleviation method)
# ══════════════════════════════════════════════════════════════════════════════
if [[ ! -f "${ALLEVIATION_MERGED}" ]]; then
    echo "WARNING: Merged alleviation CSV not found: ${ALLEVIATION_MERGED}" >&2
    echo "         Skipping TSO-constrained step. Run job_congestion_alleviation.sh" >&2
    echo "         for all three alleviation methods (and the merge step) first." >&2
    echo ""
    echo "Job completed (unconstrained only)."
    exit 0
fi

for METHOD in "${ALLEVIATION_METHODS[@]}"; do
    echo "[STEP 2] TSO-CONSTRAINED merchant revenues — alleviation_method=${METHOD}"

    python3 "${MERCHANT_SCRIPT}" \
        --mode               tso_constrained \
        --scenario           "${SCENARIO}" \
        --alleviation-method "${METHOD}" \
        --year               "${SIM_YEAR}" \
        --prices-csv         "${PRICES_CSV}" \
        --results-root       "${RESULTS_ROOT}" \
        --alleviation-csv    "${ALLEVIATION_MERGED}"

    echo "[STEP 2] TSO-CONSTRAINED complete (method=${METHOD})."
    echo ""
done

echo "════════════════════════════════════════════════════════════════════════════"
echo "Job completed successfully."
echo "Outputs:"
echo "  ${RESULTS_ROOT}/${SCENARIO}/4_merchant_revenues/"
echo "    dam_merchant_revenues_unconstrained_${SIM_YEAR}.csv"
for METHOD in "${ALLEVIATION_METHODS[@]}"; do
    M="${METHOD}"
    echo "    dam_merchant_revenues_tso_constrained_${M}_${SIM_YEAR}.csv"
done
echo "════════════════════════════════════════════════════════════════════════════"
