#!/usr/bin/env bash
#BSUB -q man
#BSUB -J pypsa_congestion_cost_alleviation
#BSUB -n 8
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=8GB]"
#BSUB -M 8GB
#BSUB -W 08:00
#BSUB -o hpc_output_and_error_files/Output_Congestion_cost_alleviation_%J.out
#BSUB -e hpc_output_and_error_files/Output_Congestion_cost_alleviation_%J.err

set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# GridBooster Congestion Cost Alleviation Calculator
# HPC Job Script — DTU compute cluster
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# Fixed project paths
# ──────────────────────────────────────────────────────────────────────────────
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
RESULTS_ROOT="${PROJECT_DIR}/results"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"
SCRIPT="${PROJECT_DIR}/congestion_cost_alleviation.py"

# ──────────────────────────────────────────────────────────────────────────────
# User-configurable arguments
# ──────────────────────────────────────────────────────────────────────────────
# Position 1: Input CSV file path (optional — auto-discovery if omitted)
INPUT_CSV="${1:-}"

# Position 2: Output directory (optional — defaults to a sibling congestion_alleviation folder)
OUTPUT_DIR="${2:-}"

# Position 3: Battery capacity [MW] (optional — default 250)
BATTERY_MW="${3:-250}"

# Position 4: Virtual-transmission multiplier α ∈ (0,1] (optional — default 1.0)
ALPHA="${4:-1.0}"

# Position 5: Congestion threshold [pu, e.g., 0.98] (optional — default 0.98)
THRESHOLD="${5:-0.98}"

# Position 6: Cost mode {"unit_cost" | "total_monthly"} (optional — default unit_cost)
COST_MODE="${6:-unit_cost}"

# Position 7: Run sensitivity analysis? {"yes" | "no"} (optional — default no)
RUN_SENSITIVITY="${7:-no}"

# Position 8: Redispatch-cost source year {2022|2023|2024|2025|mean} (optional — default mean)
REDISPATCH_COST_YEAR="${8:-mean}"

# ──────────────────────────────────────────────────────────────────────────────
# Environment setup
# ──────────────────────────────────────────────────────────────────────────────
module purge || true
module load python3/3.12.4 || true

source "$VENV_ACTIVATE"
cd "$PROJECT_DIR"

mkdir -p hpc_output_and_error_files

# ──────────────────────────────────────────────────────────────────────────────
# Print job information
# ──────────────────────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════════════════════════"
echo "  CONGESTION COST ALLEVIATION JOB"
echo "════════════════════════════════════════════════════════════════════════════"
echo "Host                   : $(hostname)"
echo "Date                   : $(date)"
echo "Python executable      : $(which python3)"
echo ""
echo "Parameters:"
echo "  Input CSV            : ${INPUT_CSV:-<auto-discover from results/>}"
echo "  Output directory     : ${OUTPUT_DIR:-<auto-resolve to congestion_alleviation sibling>}"
echo "  Battery capacity     : ${BATTERY_MW} MW"
echo "  Virtual-transmission : α = ${ALPHA}"
echo "  Congestion threshold : ${THRESHOLD}"
echo "  Cost mode            : ${COST_MODE}"
echo "  Sensitivity analysis : ${RUN_SENSITIVITY}"
echo "  Redispatch cost year : ${REDISPATCH_COST_YEAR}"
echo ""

# ──────────────────────────────────────────────────────────────────────────────
# Build command
# ──────────────────────────────────────────────────────────────────────────────
CMD=(
  python3 "$SCRIPT"
  --results-dir "$RESULTS_ROOT"
  --battery-mw "$BATTERY_MW"
  --alpha "$ALPHA"
  --threshold "$THRESHOLD"
  --cost-mode "$COST_MODE"
  --redispatch-cost-year "$REDISPATCH_COST_YEAR"
)

# Optional: input CSV (if provided)
if [[ -n "$INPUT_CSV" ]]; then
  if [[ ! -f "$INPUT_CSV" ]]; then
    echo "ERROR: Input CSV does not exist: $INPUT_CSV" >&2
    exit 1
  fi
  CMD+=(--input-csv "$INPUT_CSV")
fi

# Optional: output directory (if provided)
if [[ -n "$OUTPUT_DIR" ]]; then
  mkdir -p "$OUTPUT_DIR"
  CMD+=(--output-dir "$OUTPUT_DIR")
fi

# Optional: sensitivity analysis
if [[ "$RUN_SENSITIVITY" == "yes" || "$RUN_SENSITIVITY" == "true" || "$RUN_SENSITIVITY" == "1" ]]; then
  CMD+=(--sensitivity)
fi

# ──────────────────────────────────────────────────────────────────────────────
# Run the calculation
# ──────────────────────────────────────────────────────────────────────────────
echo "Executing:"
echo "  ${CMD[@]}"
echo ""
echo "════════════════════════════════════════════════════════════════════════════"
echo ""

"${CMD[@]}"

echo ""
echo "════════════════════════════════════════════════════════════════════════════"
echo "Congestion cost alleviation calculation completed successfully."
echo "════════════════════════════════════════════════════════════════════════════"



