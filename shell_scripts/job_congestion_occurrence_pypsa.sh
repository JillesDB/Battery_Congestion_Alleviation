#!/usr/bin/env bash
#BSUB -q man
#BSUB -J pypsa_congestion_count
#BSUB -n 8
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=8GB]"
#BSUB -M 8GB
#BSUB -W 08:00
#BSUB -o hpc_output_and_error_files/Output_Congestion_count_%J.out
#BSUB -e hpc_output_and_error_files/Output_Congestion_count_%J.err

set -euo pipefail

# ------------------------------
# Default run configuration
# ------------------------------
RUN_CONFIG="kupferzell_2024_simple"
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
PYPSA_EUR_DIR="/zhome/26/e/209460/PycharmProjects/pypsa-eur"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"
CONGESTION_SCRIPT="$PROJECT_DIR/congestion_occurence_pypsa.py"

# ------------------------------
# User-configurable arguments
# ------------------------------
NETWORK_PATH="${1:-$PYPSA_EUR_DIR/results/$RUN_CONFIG/networks/base_s_256_elec_.nc}"
OUTPUT_ROOT="${2:-$PROJECT_DIR/results}"
THRESHOLD="${3:-0.98}"
# Position 4: Minimum voltage [kV] (optional — default 0 disables filtering)
# Backward compatibility: if arg 4 is non-numeric, it is treated as line ids.
MINIMUM_VOLTAGE_RAW="${4:-0}"

# Position 5: Comma-separated line ids. Empty = default Kupferzell-near lines.
REQUESTED_LINES="${5:-}"
# Position 6: Congestion detection method.
METHOD="${6:-shadow_price}"
# Position 7: Target area.
TARGET_AREA="${7:-corridor}"
# Position 8: Shadow-price threshold (used when METHOD=shadow_price).
SHADOW_PRICE_THRESHOLD="${8:-1e-6}"

# ------------------------------
# Environment setup
# ------------------------------
module purge || true
module load python3/3.12.4 || true

source "$VENV_ACTIVATE"
cd "$PROJECT_DIR"

mkdir -p hpc_output_and_error_files
mkdir -p "$OUTPUT_ROOT"

echo "=== CONGESTION COUNT JOB INFO ==="
echo "Host: $(hostname)"
echo "Date: $(date)"
echo "Python: $(which python3)"
echo "Network: $NETWORK_PATH"
echo "Output root: $OUTPUT_ROOT"
echo "Threshold: $THRESHOLD"
echo "Method: $METHOD"
echo "Target area: $TARGET_AREA"
echo "Shadow-price threshold: $SHADOW_PRICE_THRESHOLD"

if [[ "$MINIMUM_VOLTAGE_RAW" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  MINIMUM_VOLTAGE="$MINIMUM_VOLTAGE_RAW"
else
  MINIMUM_VOLTAGE="0"
  if [[ -n "$MINIMUM_VOLTAGE_RAW" ]]; then
    REQUESTED_LINES="${REQUESTED_LINES:-$MINIMUM_VOLTAGE_RAW}"
  fi
fi

echo "Minimum voltage: $MINIMUM_VOLTAGE"
echo "Requested lines: ${REQUESTED_LINES:-<default kupferzell>}"

if [[ ! -f "$NETWORK_PATH" ]]; then
  echo "ERROR: Network file does not exist: $NETWORK_PATH" >&2
  exit 1
fi

CMD=(
  python3 "$CONGESTION_SCRIPT"
  --network "$NETWORK_PATH"
  --output-dir "$OUTPUT_ROOT"
  --threshold "$THRESHOLD"
  --minimum-voltage "$MINIMUM_VOLTAGE"
  --method "$METHOD"
  --target-area "$TARGET_AREA"
  --shadow-price-threshold "$SHADOW_PRICE_THRESHOLD"
)

if [[ -n "$REQUESTED_LINES" ]]; then
  CMD+=(--lines "$REQUESTED_LINES")
fi

"${CMD[@]}"

echo "Congestion count completed successfully."

