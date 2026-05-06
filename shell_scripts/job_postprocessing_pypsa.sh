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
export ENTSOE_API_TOKEN="c2f8b11c-ec54-45ad-9223-f1f4d24bb427"
# ══════════════════════════════════════════════════════════════════════════════
# PYPSA POST-PROCESSING — validation + congestion extraction
#
# Set the TOGGLES below; everything else is auto-derived.
# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────────
# │  TOGGLES  — the only lines you need to edit before submitting
# ├─────────────────────────────────────────────────────────────────────────────
SCENARIO="kupferzell_full"             # kupferzell_simple | kupferzell_full
MODE="pypsa-validation"       # pypsa-validation | congestion | orchestrator | all
                              # Default is pypsa-validation: job_congestion_occurrence_pypsa.sh
                              # is the single source of truth for congestion_occurrence/ files.
TARGET_AREA="custom_lines"
#CUSTOM_LINES_full="262,350,328,179,334,269,341,312,270,178,310,176,94,277,95,276,79,80,267,316,177,311"
CUSTOM_LINES="262,350,328,179,334,269,341,312,270,178,310,176,79,80,267,177,311"
#CUSTOM_LINES="82,289,244,245,246,147"
# Validation reference data source (applies to pypsa-validation and all modes):
#   eurostat — Eurostat annual balances only; no API key needed.
#   entsoe   — ENTSO-E hourly actuals only; requires ENTSOE_API_TOKEN env var.
#   both     — Eurostat + ENTSO-E; also produces three-way comparison chart.
VALIDATION_SOURCE="both"
# └─────────────────────────────────────────────────────────────────────────────

# ── Less-commonly changed parameters ──────────────────────────────────────────
SIM_YEAR="2025"
THRESHOLD="0.98"
THRESHOLD_N1="1.00"
METHOD="dual"
MINIMUM_VOLTAGE="220"      # minimum line voltage [kV] for zoomed map; keep in sync with occurrence job

# ── Fixed project paths ───────────────────────────────────────────────────────
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
PYPSA_EUR_DIR="/zhome/26/e/209460/PycharmProjects/pypsa-eur"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"

VALIDATION_SCRIPT="${PROJECT_DIR}/run_validation_pypsa.py"
CONGESTION_SCRIPT="${PROJECT_DIR}/congestion_occurence_pypsa.py"
ORCHESTRATOR_SCRIPT="${PROJECT_DIR}/research_workflow.py"

# ── Derived paths (auto-set from toggles — do not edit) ───────────────────────
PYPSA_SCENARIO="${SCENARIO#kupferzell_}"
NETWORK_PATH="${PYPSA_EUR_DIR}/results/kupferzell_2024_${PYPSA_SCENARIO}/networks/base_s_256_elec_.nc"
RESULTS_ROOT="${PROJECT_DIR}/results"
VALIDATION_OUTPUT_DIR="${RESULTS_ROOT}/${SCENARIO}/1_pypsa_validation"
CONGESTION_OUTPUT_DIR="${RESULTS_ROOT}/${SCENARIO}/2_congestion_occurrence"

POWERPLANTS_CSV=""
RUN_FOLDER="kupferzell_2024_${PYPSA_SCENARIO}"
if [[ -f "${PYPSA_EUR_DIR}/resources/${RUN_FOLDER}/powerplants_s_256.csv" ]]; then
  POWERPLANTS_CSV="${PYPSA_EUR_DIR}/resources/${RUN_FOLDER}/powerplants_s_256.csv"
elif [[ -f "${PYPSA_EUR_DIR}/resources/powerplants_s_256.csv" ]]; then
  POWERPLANTS_CSV="${PYPSA_EUR_DIR}/resources/powerplants_s_256.csv"
fi

# ── ENTSO-E API key (only needed when VALIDATION_SOURCE is "entsoe" or "both") ─
# Export here so the Python process inherits it; the key itself lives in your
# environment — never hard-code it in this file.
# To set it once per session: export ENTSOE_API_TOKEN="<your-key>"
# To set it permanently:      add the line above to ~/.bashrc or ~/.profile

# ── Environment setup ─────────────────────────────────────────────────────────
module purge || true
module load python3/3.12.4 || true

source "${VENV_ACTIVATE}"
cd "${PROJECT_DIR}"

LOCKDIR="${CONGESTION_OUTPUT_DIR:-${OCC_DIR}}/.lockdir"
mkdir -p "$(dirname "${LOCKDIR}")"
for _ in {1..600}; do
    if mkdir "${LOCKDIR}" 2>/dev/null; then break; fi
    sleep 1
done
trap 'rmdir "${LOCKDIR}" 2>/dev/null || true' EXIT

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
echo "Validation source : ${VALIDATION_SOURCE}"
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
        --network             "${NETWORK_PATH}" \
        --output-dir          "${RESULTS_ROOT}" \
        --powerplants-csv     "${POWERPLANTS_CSV}" \
        --validation-source   "${VALIDATION_SOURCE}"
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
        --network             "${NETWORK_PATH}" \
        --output-dir          "${RESULTS_ROOT}" \
        --powerplants-csv     "${POWERPLANTS_CSV}" \
        --validation-source   "${VALIDATION_SOURCE}"

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

# ── Kupferzell zoomed network map (pypsa-validation and all modes) ────────────
# Produces the same figure as job_congestion_occurrence_pypsa.sh and saves it
# to the pypsa-validation folder so the identical annotated map is available
# alongside the other validation outputs without re-running the occurrence job.
if [[ "${MODE}" == "pypsa-validation" || "${MODE}" == "all" ]]; then
    echo ""
    echo "════════════════════════════════════════════════════════════════════════════"
    echo "  ZOOMED NETWORK MAP"
    echo "════════════════════════════════════════════════════════════════════════════"
    FIGURE_PREFIX="congestion_${TARGET_AREA}_${METHOD}_${SIM_YEAR}"
    python3 - "${NETWORK_PATH}" "${VALIDATION_OUTPUT_DIR}" \
               "${FIGURE_PREFIX}" "${CUSTOM_LINES:-}" \
               "${MINIMUM_VOLTAGE}" "${PROJECT_DIR}" <<'PY'
import sys
from pathlib import Path
import pandas as pd
import pypsa
sys.path.insert(0, sys.argv[6])
from plotting import plot_kupferzell_zoomed_network_map

network_path  = sys.argv[1]
output_dir    = Path(sys.argv[2])
figure_prefix = sys.argv[3]
custom_lines  = sys.argv[4]
min_voltage   = float(sys.argv[5])

print(f"Loading network: {network_path}")
n = pypsa.Network(network_path)

kup_ids = (
    pd.Index([x.strip() for x in custom_lines.split(",") if x.strip()])
    if custom_lines.strip() else pd.Index([])
)
print(f"Highlighted lines: {len(kup_ids)}")

output_dir.mkdir(parents=True, exist_ok=True)
out = output_dir / f"figure_kupferzell_zoomed_network_map.png"
plot_kupferzell_zoomed_network_map(
    buses=n.buses,
    lines=n.lines,
    output_path=str(out),
    kupferzell_line_ids=kup_ids,
    minimum_voltage=min_voltage,
)
print(f"[saved] {out}")
PY
fi

echo ""
echo "════════════════════════════════════════════════════════════════════════════"
echo "Post-processing completed successfully."
echo "  Validation outputs : ${VALIDATION_OUTPUT_DIR}"
echo "  Congestion outputs : ${CONGESTION_OUTPUT_DIR}"
echo "════════════════════════════════════════════════════════════════════════════"
