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
# User-configurable arguments
# ------------------------------
NETWORK_PATH="${1:-/zhome/26/e/209460/PycharmProjects/pypsa-eur/results/kupferzell_2024_simple/networks/base_s_256_elec_.nc}"
OUTPUT_ROOT="${2:-/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation/results}"
THRESHOLD="${3:-0.98}"
# Comma-separated line ids. Empty = default Kupferzell-near lines.
REQUESTED_LINES="${4:-}"

# ------------------------------
# Paths
# ------------------------------
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"
CONGESTION_SCRIPT="$PROJECT_DIR/congestion_occurence_pypsa.py"

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
)

if [[ -n "$REQUESTED_LINES" ]]; then
  CMD+=(--lines "$REQUESTED_LINES")
fi

"${CMD[@]}"

echo "Congestion count completed successfully."



