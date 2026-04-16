#!/usr/bin/env bash
#BSUB -q man
#BSUB -J pypsa_postprocess
#BSUB -n 8
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=8GB]"
#BSUB -M 8GB
#BSUB -W 08:00
#BSUB -o hpc_output_and_error_files/postprocess_%J.out
#BSUB -e hpc_output_and_error_files/postprocess_%J.err

set -euo pipefail

# ------------------------------
# User-configurable arguments
# ------------------------------
MODE="${1:-all}"                       # all | validation | congestion | orchestrator
NETWORK_PATH="${2:-/zhome/26/e/209460/PycharmProjects/pypsa-eur/results/kupferzell_2024_full/networks/base_s_256_elec_.nc}"
OUTPUT_DIR="${3:-/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation/outputs/postprocess_kupferzell_2024_full}"
POWERPLANTS_CSV="${4:-/zhome/26/e/209460/PycharmProjects/pypsa-eur/resources/kupferzell_2024_full/powerplants_s_256.csv}"
THRESHOLD="${5:-0.98}"

# ------------------------------
# Paths
# ------------------------------
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"

VALIDATION_SCRIPT="$PROJECT_DIR/run_validation_pypsa.py"
CONGESTION_SCRIPT="$PROJECT_DIR/congestion_occurence_pypsa.py"
ORCHESTRATOR_SCRIPT="$PROJECT_DIR/research_workflow.py"

# ------------------------------
# Environment setup
# ------------------------------
module purge || true
module load python3/3.12.4 || true

source "$VENV_ACTIVATE"
cd "$PROJECT_DIR"

mkdir -p hpc_output_and_error_files
mkdir -p "$OUTPUT_DIR"

echo "=== POSTPROCESS JOB INFO ==="
echo "Host: $(hostname)"
echo "Date: $(date)"
echo "Python: $(which python3)"
echo "Mode: $MODE"
echo "Network: $NETWORK_PATH"
echo "Output: $OUTPUT_DIR"
echo "Powerplants CSV: $POWERPLANTS_CSV"
echo "Threshold: $THRESHOLD"

if [[ ! -f "$NETWORK_PATH" ]]; then
  echo "ERROR: Network file does not exist: $NETWORK_PATH" >&2
  exit 1
fi

# ------------------------------
# Run selected workflow
# ------------------------------
case "$MODE" in
  validation)
    python3 "$VALIDATION_SCRIPT" \
      --network "$NETWORK_PATH" \
      --output-dir "$OUTPUT_DIR" \
      --powerplants-csv "$POWERPLANTS_CSV"
    ;;

  congestion)
    python3 "$CONGESTION_SCRIPT" \
      --network "$NETWORK_PATH" \
      --output-dir "$OUTPUT_DIR" \
      --threshold "$THRESHOLD"
    ;;

  orchestrator)
    python3 "$ORCHESTRATOR_SCRIPT" \
      --mode postprocess \
      --solved-network "$NETWORK_PATH" \
      --output-dir "$OUTPUT_DIR" \
      --powerplants-csv "$POWERPLANTS_CSV" \
      --congestion-threshold "$THRESHOLD"
    ;;

  all)
    python3 "$VALIDATION_SCRIPT" \
      --network "$NETWORK_PATH" \
      --output-dir "$OUTPUT_DIR" \
      --powerplants-csv "$POWERPLANTS_CSV"

    python3 "$CONGESTION_SCRIPT" \
      --network "$NETWORK_PATH" \
      --output-dir "$OUTPUT_DIR" \
      --threshold "$THRESHOLD"
    ;;

  *)
    echo "ERROR: Invalid MODE '$MODE'. Use: all | validation | congestion | orchestrator" >&2
    exit 2
    ;;
esac

echo "Post-processing completed successfully. Outputs in: $OUTPUT_DIR"
