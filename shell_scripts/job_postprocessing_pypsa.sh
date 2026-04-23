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

# ------------------------------
# Fixed project paths
# ------------------------------
RUN_CONFIG="kupferzell_2024_simple"
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
PYPSA_EUR_DIR="/zhome/26/e/209460/PycharmProjects/pypsa-eur"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"

# ------------------------------
# User-configurable arguments
# ------------------------------
MODE="${1:-all}"                       # all | pypsa-validation | congestion | orchestrator
NETWORK_PATH="${2:-$PYPSA_EUR_DIR/results/$RUN_CONFIG/networks/base_s_256_elec_.nc}"
RESULTS_ROOT="${3:-$PROJECT_DIR/results}"
POWERPLANTS_CSV="${4:-}"
THRESHOLD="${5:-0.98}"
CONGESTION_OUTPUT_ROOT="${6:-$RESULTS_ROOT}"
CONGESTION_METHOD="${7:-redispatch_trigger}"
CONGESTION_TARGET_AREA="${8:-corridor}"

# ------------------------------
# Infer scenario context from network path
# ------------------------------
RUN_FOLDER=""
if [[ "$NETWORK_PATH" =~ /results/([^/]+)/networks/ ]]; then
  RUN_FOLDER="${BASH_REMATCH[1]}"
fi

SCENARIO="default"
if [[ -n "$RUN_FOLDER" ]]; then
  if [[ "$RUN_FOLDER" == *"simple"* ]]; then
    SCENARIO="kupferzell_simple"
  elif [[ "$RUN_FOLDER" == *"full"* ]]; then
    SCENARIO="kupferzell_full"
  fi
fi

VALIDATION_OUTPUT_DIR="$RESULTS_ROOT/$SCENARIO/pypsa-validation"
CONGESTION_OUTPUT_DIR="$CONGESTION_OUTPUT_ROOT/$SCENARIO/congestion_occurrence"

if [[ -z "$POWERPLANTS_CSV" ]]; then
  if [[ -n "$RUN_FOLDER" && -f "$PYPSA_EUR_DIR/resources/$RUN_FOLDER/powerplants_s_256.csv" ]]; then
    POWERPLANTS_CSV="$PYPSA_EUR_DIR/resources/$RUN_FOLDER/powerplants_s_256.csv"
  elif [[ -f "$PYPSA_EUR_DIR/resources/powerplants_s_256.csv" ]]; then
    POWERPLANTS_CSV="$PYPSA_EUR_DIR/resources/powerplants_s_256.csv"
  else
    POWERPLANTS_CSV="$PYPSA_EUR_DIR/resources/kupferzell_2024_simple/powerplants_s_256.csv"
  fi
fi

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
mkdir -p "$RESULTS_ROOT"
mkdir -p "$CONGESTION_OUTPUT_ROOT"
mkdir -p "$VALIDATION_OUTPUT_DIR"
mkdir -p "$CONGESTION_OUTPUT_DIR"

echo "=== POSTPROCESS JOB INFO ==="
echo "Host: $(hostname)"
echo "Date: $(date)"
echo "Python: $(which python3)"
echo "Mode: $MODE"
echo "Network: $NETWORK_PATH"
echo "Run folder (inferred): ${RUN_FOLDER:-default}"
echo "Scenario: $SCENARIO"
echo "Validation output dir: $VALIDATION_OUTPUT_DIR"
echo "Congestion output dir: $CONGESTION_OUTPUT_DIR"
echo "Powerplants CSV: $POWERPLANTS_CSV"
echo "Threshold: $THRESHOLD"
echo "Congestion method: $CONGESTION_METHOD"
echo "Congestion target area: $CONGESTION_TARGET_AREA"

if [[ ! -f "$NETWORK_PATH" ]]; then
  echo "ERROR: Network file does not exist: $NETWORK_PATH" >&2
  exit 1
fi

if [[ "$MODE" != "congestion" && ! -f "$POWERPLANTS_CSV" ]]; then
  echo "ERROR: Powerplants CSV does not exist: $POWERPLANTS_CSV" >&2
  echo "Hint: pass arg 4 explicitly or ensure resources/<run>/powerplants_s_256.csv exists." >&2
  exit 1
fi

# ------------------------------
# Run selected workflow
# ------------------------------
case "$MODE" in
  pypsa-validation)
    python3 "$VALIDATION_SCRIPT" \
      --network "$NETWORK_PATH" \
      --output-dir "$RESULTS_ROOT" \
      --powerplants-csv "$POWERPLANTS_CSV"
    ;;

  congestion)
    python3 "$CONGESTION_SCRIPT" \
      --network "$NETWORK_PATH" \
      --output-dir "$CONGESTION_OUTPUT_ROOT" \
      --threshold "$THRESHOLD" \
      --method "$CONGESTION_METHOD" \
      --target-area "$CONGESTION_TARGET_AREA"
    ;;

  orchestrator)
    python3 "$ORCHESTRATOR_SCRIPT" \
      --mode postprocess \
      --solved-network "$NETWORK_PATH" \
      --output-dir "$RESULTS_ROOT" \
      --powerplants-csv "$POWERPLANTS_CSV" \
      --congestion-threshold "$THRESHOLD"
    ;;

  all)
    python3 "$VALIDATION_SCRIPT" \
      --network "$NETWORK_PATH" \
      --output-dir "$RESULTS_ROOT" \
      --powerplants-csv "$POWERPLANTS_CSV"

    python3 "$CONGESTION_SCRIPT" \
      --network "$NETWORK_PATH" \
      --output-dir "$CONGESTION_OUTPUT_ROOT" \
      --threshold "$THRESHOLD" \
      --method "$CONGESTION_METHOD" \
      --target-area "$CONGESTION_TARGET_AREA"
    ;;

  *)
    echo "ERROR: Invalid MODE '$MODE'. Use: all | pypsa-validation | congestion | orchestrator" >&2
    exit 2
    ;;
esac

echo "Post-processing completed successfully."
echo "Validation outputs: $VALIDATION_OUTPUT_DIR"
echo "Congestion outputs: $CONGESTION_OUTPUT_DIR"

