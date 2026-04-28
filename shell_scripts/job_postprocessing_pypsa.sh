#!/usr/bin/env bash
#BSUB -q man
#BSUB -J pypsa_postprocess
#BSUB -n 8
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=8GB]"
#BSUB -M 8GB
#BSUB -W 08:00
#BSUB -o hpc_output_and_error_files/Output_Postprocessing_%J.out
#BSUB -e hpc_output_and_error_files/Output_Postprocessing_%J.err

set -euo pipefail

# ══════════════════════════════════════════════════════════════════════════════
# PYPSA POST-PROCESSING — validation + congestion extraction
#
# Set the TOGGLES below; everything else is auto-derived.
# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────────
# │  TOGGLES  — the only lines you need to edit before submitting
# ├─────────────────────────────────────────────────────────────────────────────
SCENARIO="full"             # simple | full
MODE="pypsa-validation"       # pypsa-validation | congestion | orchestrator | all
                              # Default is pypsa-validation: job_congestion_occurrence_pypsa.sh
                              # is the single source of truth for congestion_occurrence/ files.
TARGET_AREA="custom_lines"
CUSTOM_LINES="262,350,328,179,334,269,341,312,270,178,310,176,94,277,95,276,79,80,267,316,177,311"
# └─────────────────────────────────────────────────────────────────────────────

# ── Less-commonly changed parameters ──────────────────────────────────────────
THRESHOLD="0.98"
THRESHOLD_N1="1.00"
METHOD="dual"

# ── Fixed project paths ───────────────────────────────────────────────────────
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
PYPSA_EUR_DIR="/zhome/26/e/209460/PycharmProjects/pypsa-eur"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"

VALIDATION_SCRIPT="${PROJECT_DIR}/run_validation_pypsa.py"
CONGESTION_SCRIPT="${PROJECT_DIR}/congestion_occurence_pypsa.py"
ORCHESTRATOR_SCRIPT="${PROJECT_DIR}/research_workflow.py"

# ── Derived paths (auto-set from toggles — do not edit) ───────────────────────
NETWORK_PATH="${PYPSA_EUR_DIR}/results/kupferzell_2024_${SCENARIO}/networks/base_s_256_elec_.nc"
RESULTS_ROOT="${PROJECT_DIR}/results"
VALIDATION_OUTPUT_DIR="${RESULTS_ROOT}/kupferzell_${SCENARIO}/pypsa-validation"
CONGESTION_OUTPUT_DIR="${RESULTS_ROOT}/kupferzell_${SCENARIO}/congestion_occurrence"

POWERPLANTS_CSV=""
RUN_FOLDER="kupferzell_2024_${SCENARIO}"
if [[ -f "${PYPSA_EUR_DIR}/resources/${RUN_FOLDER}/powerplants_s_256.csv" ]]; then
  POWERPLANTS_CSV="${PYPSA_EUR_DIR}/resources/${RUN_FOLDER}/powerplants_s_256.csv"
elif [[ -f "${PYPSA_EUR_DIR}/resources/powerplants_s_256.csv" ]]; then
  POWERPLANTS_CSV="${PYPSA_EUR_DIR}/resources/powerplants_s_256.csv"
fi

# ── Environment setup ─────────────────────────────────────────────────────────
module purge || true
module load python3/3.12.4 || true

source "${VENV_ACTIVATE}"
cd "${PROJECT_DIR}"

LOCK="${CONGESTION_OUTPUT_DIR}/.lock"
mkdir -p "${CONGESTION_OUTPUT_DIR}"
exec 9>"${LOCK}"
flock 9
trap 'flock -u 9; rm -f "${LOCK}"' EXIT

mkdir -p hpc_output_and_error_files
mkdir -p "${RESULTS_ROOT}"
mkdir -p "${VALIDATION_OUTPUT_DIR}"
mkdir -p "${CONGESTION_OUTPUT_DIR}"

echo "════════════════════════════════════════════════════════════════════════════"
echo "  PYPSA POST-PROCESSING"
echo "════════════════════════════════════════════════════════════════════════════"
echo "Host              : $(hostname)"
echo "Date              : $(date)"
echo "Python            : $(which python3)"
echo "Scenario          : ${SCENARIO}"
echo "Mode              : ${MODE}"
echo "Network           : ${NETWORK_PATH}"
echo "Validation dir    : ${VALIDATION_OUTPUT_DIR}"
echo "Congestion dir    : ${CONGESTION_OUTPUT_DIR}"
echo "Powerplants CSV   : ${POWERPLANTS_CSV}"
echo "Threshold         : ${THRESHOLD}  (N-1: ${THRESHOLD_N1})"
echo "Method            : ${METHOD}"
echo "Target area       : ${TARGET_AREA}"
echo "════════════════════════════════════════════════════════════════════════════"

# ── Input validation ──────────────────────────────────────────────────────────
if [[ ! -f "${NETWORK_PATH}" ]]; then
    echo "ERROR: Network file not found: ${NETWORK_PATH}" >&2
    exit 1
fi

if [[ "${MODE}" != "congestion" && -z "${POWERPLANTS_CSV}" ]]; then
    echo "ERROR: Powerplants CSV not found." >&2
    echo "       Expected: ${PYPSA_EUR_DIR}/resources/${RUN_FOLDER}/powerplants_s_256.csv" >&2
    exit 1
fi

# ── Run selected workflow ─────────────────────────────────────────────────────
case "${MODE}" in
  pypsa-validation)
    python3 "${VALIDATION_SCRIPT}" \
        --network        "${NETWORK_PATH}" \
        --output-dir     "${RESULTS_ROOT}" \
        --powerplants-csv "${POWERPLANTS_CSV}"
    ;;

  congestion)
    python3 "${CONGESTION_SCRIPT}" \
        --network        "${NETWORK_PATH}" \
        --output-dir     "${RESULTS_ROOT}" \
        --threshold      "${THRESHOLD}" \
        --threshold-n1   "${THRESHOLD_N1}" \
        --method         "${METHOD}" \
        --target-area    "${TARGET_AREA}" \
        $([[ -n "${CUSTOM_LINES}" ]] && echo "--custom-lines ${CUSTOM_LINES}")
    ;;

  orchestrator)
    python3 "${ORCHESTRATOR_SCRIPT}" \
        --mode                postprocess \
        --solved-network      "${NETWORK_PATH}" \
        --output-dir          "${RESULTS_ROOT}" \
        --powerplants-csv     "${POWERPLANTS_CSV}" \
        --congestion-threshold "${THRESHOLD}" \
        --target-area         "${TARGET_AREA}"
    ;;

  all)
    python3 "${VALIDATION_SCRIPT}" \
        --network        "${NETWORK_PATH}" \
        --output-dir     "${RESULTS_ROOT}" \
        --powerplants-csv "${POWERPLANTS_CSV}"

    python3 "${CONGESTION_SCRIPT}" \
        --network        "${NETWORK_PATH}" \
        --output-dir     "${RESULTS_ROOT}" \
        --threshold      "${THRESHOLD}" \
        --threshold-n1   "${THRESHOLD_N1}" \
        --method         "${METHOD}" \
        --target-area    "${TARGET_AREA}" \
        $([[ -n "${CUSTOM_LINES}" ]] && echo "--custom-lines ${CUSTOM_LINES}")
    ;;

  *)
    echo "ERROR: Invalid MODE '${MODE}'. Use: all | pypsa-validation | congestion | orchestrator" >&2
    exit 2
    ;;
esac

echo ""
echo "════════════════════════════════════════════════════════════════════════════"
echo "Post-processing completed successfully."
echo "  Validation outputs : ${VALIDATION_OUTPUT_DIR}"
echo "  Congestion outputs : ${CONGESTION_OUTPUT_DIR}"
echo "════════════════════════════════════════════════════════════════════════════"
