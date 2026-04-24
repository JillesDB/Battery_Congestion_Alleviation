#!/usr/bin/env bash
#BSUB -q man
#BSUB -J kupf_simple_shadow_cf
#BSUB -n 16
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=8GB]"
#BSUB -M 8GB
#BSUB -W 24:00
#BSUB -o hpc_output_and_error_files/Output_Kupf_Simple_Shadow_CF_%J.out
#BSUB -e hpc_output_and_error_files/Output_Kupf_Simple_Shadow_CF_%J.err

set -euo pipefail

PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
DEFAULT_CONFIG="$PROJECT_DIR/counterfactual_configs/kupferzell_simple_shadow_counterfactual_jan2025.yaml"
CONFIG_PATH="${1:-$DEFAULT_CONFIG}"

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "ERROR: Counterfactual config not found: $CONFIG_PATH" >&2
  exit 1
fi

module purge || true
module load python3/3.12.4 || true

eval "$(python3 - "$CONFIG_PATH" <<'PY'
import shlex
import sys
import yaml

cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
keys = [
    "project_dir",
    "pypsa_eur_dir",
    "venv_activate",
    "scenario_name",
    "pypsa_config_file",
    "input_network",
    "output_root",
    "expected_snapshot_start",
    "expected_snapshot_count",
    "expected_snapshot_end_exclusive",
    "battery_mw",
    "alpha",
    "target_area",
    "method",
    "threshold_loading",
    "threshold_n1",
    "minimum_voltage_kv",
    "solve_threads_per_case",
]
for key in keys:
    value = cfg[key]
    print(f"{key.upper()}={shlex.quote(str(value))}")
PY
)"

source "$VENV_ACTIVATE"
cd "$PROJECT_DIR"

mkdir -p hpc_output_and_error_files
mkdir -p "$OUTPUT_ROOT"

BASE_SOLVE_DIR="$OUTPUT_ROOT/solved_base"
BOOST_SOLVE_DIR="$OUTPUT_ROOT/solved_boost"
BASE_POST_ROOT="$OUTPUT_ROOT/postprocess_base"
BOOST_POST_ROOT="$OUTPUT_ROOT/postprocess_boost"
ALLEVIATION_DIR="$OUTPUT_ROOT/congestion_alleviation"

mkdir -p "$BASE_SOLVE_DIR" "$BOOST_SOLVE_DIR" "$BASE_POST_ROOT" "$BOOST_POST_ROOT" "$ALLEVIATION_DIR"

echo "=== SHADOW-PRICE COUNTERFACTUAL JOB INFO ==="
echo "Host                  : $(hostname)"
echo "Date                  : $(date)"
echo "Python                : $(which python3)"
echo "Config                : $CONFIG_PATH"
echo "Scenario              : $SCENARIO_NAME"
echo "PyPSA config          : $PYPSA_CONFIG_FILE"
echo "Input network         : $INPUT_NETWORK"
echo "Output root           : $OUTPUT_ROOT"
echo "Battery MW            : $BATTERY_MW"
echo "Alpha                 : $ALPHA"
echo "Target area           : $TARGET_AREA"
echo "Method                : $METHOD"
echo "Threshold load        : $THRESHOLD_LOADING"
echo "Threshold N-1         : $THRESHOLD_N1"
echo "Minimum voltage [kV]  : $MINIMUM_VOLTAGE_KV"
echo "Threads per solve     : $SOLVE_THREADS_PER_CASE"

python3 - "$CONFIG_PATH" <<'PY'
import sys
from pathlib import Path

import pypsa
import yaml

cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
pypsa_cfg = yaml.safe_load(open(cfg["pypsa_config_file"], encoding="utf-8"))

snapshots = pypsa_cfg["snapshots"]
assert snapshots["start"] == cfg["expected_snapshot_start"].split()[0], snapshots
assert snapshots["end"] == cfg["expected_snapshot_end_exclusive"].split()[0], snapshots
assert snapshots["inclusive"] == "left", snapshots
assert float(pypsa_cfg["lines"]["s_max_pu"]) == 0.7, pypsa_cfg["lines"]["s_max_pu"]
assert pypsa_cfg["solving"]["options"]["assign_all_duals"] is True

network_path = Path(cfg["input_network"])
assert network_path.exists(), network_path
n = pypsa.Network(network_path)
assert len(n.snapshots) == int(cfg["expected_snapshot_count"]), len(n.snapshots)
assert str(n.snapshots[0]) == cfg["expected_snapshot_start"], str(n.snapshots[0])
assert str(n.snapshots[-1] + (n.snapshots[1] - n.snapshots[0])) == cfg["expected_snapshot_end_exclusive"]

print("Config checks passed:")
print(f"  snapshots : {snapshots['start']} -> {snapshots['end']} ({snapshots['inclusive']})")
print(f"  s_max_pu  : {pypsa_cfg['lines']['s_max_pu']}")
print(f"  duals     : assign_all_duals={pypsa_cfg['solving']['options']['assign_all_duals']}")
print(f"  network   : {network_path}")
print(f"  hours     : {len(n.snapshots)}")
PY

echo "Starting parallel baseline and boosted shadow-price solves..."

python3 "$PROJECT_DIR/research_workflow.py" \
  --mode solve \
  --input-network "$INPUT_NETWORK" \
  --output-dir "$BASE_SOLVE_DIR" \
  --battery-mw 0 \
  --alpha "$ALPHA" \
  --target-area "$TARGET_AREA" &
PID_BASE=$!

python3 "$PROJECT_DIR/research_workflow.py" \
  --mode solve \
  --input-network "$INPUT_NETWORK" \
  --output-dir "$BOOST_SOLVE_DIR" \
  --battery-mw "$BATTERY_MW" \
  --alpha "$ALPHA" \
  --target-area "$TARGET_AREA" &
PID_BOOST=$!

wait "$PID_BASE"
wait "$PID_BOOST"

BASE_TAG="network_2025_base.nc"
BOOST_TAG=$(python3 - <<PY
battery = float("$BATTERY_MW")
alpha = float("$ALPHA")
print(f"network_2025_boost_mw{int(battery)}_a{alpha:.2f}.nc")
PY
)

BASE_NC="$BASE_SOLVE_DIR/$BASE_TAG"
BOOST_NC="$BOOST_SOLVE_DIR/$BOOST_TAG"

python3 - "$BASE_NC" "$BOOST_NC" "$TARGET_AREA" <<'PY'
import sys

import pypsa

from congestion_occurence_pypsa import select_target_lines

base_nc, boost_nc, target_area = sys.argv[1:4]

for label, path in [("base", base_nc), ("boost", boost_nc)]:
    n = pypsa.Network(path)
    mu_upper = getattr(n.lines_t, "mu_upper", None)
    if mu_upper is None or len(mu_upper) == 0:
        raise RuntimeError(f"{label}: mu_upper missing in {path}")
    monitored, scope, _ = select_target_lines(
        n,
        target_area=target_area,
        requested_lines=[],
        minimum_voltage=220.0,
    )
    median_pu = float(n.lines.loc[monitored, "s_max_pu"].median()) if len(monitored) else float("nan")
    max_pu = float(n.lines["s_max_pu"].max())
    print(f"{label}: mu_upper columns={mu_upper.shape[1]}, monitored={len(monitored)}, scope={scope}")
    print(f"{label}: median monitored s_max_pu={median_pu:.3f}, max network s_max_pu={max_pu:.3f}")
PY

echo "Starting parallel congestion occurrence postprocessing..."

python3 "$PROJECT_DIR/congestion_occurence_pypsa.py" \
  --network "$BASE_NC" \
  --output-dir "$BASE_POST_ROOT" \
  --threshold "$THRESHOLD_LOADING" \
  --threshold-n1 "$THRESHOLD_N1" \
  --minimum-voltage "$MINIMUM_VOLTAGE_KV" \
  --method "$METHOD" \
  --target-area "$TARGET_AREA" &
PID_OCC_BASE=$!

python3 "$PROJECT_DIR/congestion_occurence_pypsa.py" \
  --network "$BOOST_NC" \
  --output-dir "$BOOST_POST_ROOT" \
  --threshold "$THRESHOLD_LOADING" \
  --threshold-n1 "$THRESHOLD_N1" \
  --minimum-voltage "$MINIMUM_VOLTAGE_KV" \
  --method "$METHOD" \
  --target-area "$TARGET_AREA" &
PID_OCC_BOOST=$!

wait "$PID_OCC_BASE"
wait "$PID_OCC_BOOST"

MU_BASE=$(find "$BASE_POST_ROOT" -path '*congestion_occurrence/*_mu_upper.csv' | head -n 1)
MU_BOOST=$(find "$BOOST_POST_ROOT" -path '*congestion_occurrence/*_mu_upper.csv' | head -n 1)
RENT_BASE=$(find "$BASE_POST_ROOT" -path '*congestion_occurrence/*_congestion_rent_eur_per_line.csv' | head -n 1)
RENT_BOOST=$(find "$BOOST_POST_ROOT" -path '*congestion_occurrence/*_congestion_rent_eur_per_line.csv' | head -n 1)

if [[ -z "$MU_BASE" || -z "$MU_BOOST" || -z "$RENT_BASE" || -z "$RENT_BOOST" ]]; then
  echo "ERROR: Missing dual postprocessing outputs after occurrence step." >&2
  exit 1
fi

echo "Dual occurrence outputs located:"
echo "  MU_BASE   : $MU_BASE"
echo "  MU_BOOST  : $MU_BOOST"
echo "  RENT_BASE : $RENT_BASE"
echo "  RENT_BOOST: $RENT_BOOST"

python3 "$PROJECT_DIR/congestion_cost_alleviation.py" \
  --source dual \
  --mu-base-csv "$MU_BASE" \
  --mu-boost-csv "$MU_BOOST" \
  --network "$BOOST_NC" \
  --output-dir "$ALLEVIATION_DIR" \
  --battery-mw "$BATTERY_MW" \
  --alpha "$ALPHA"

python3 - "$RENT_BASE" "$RENT_BOOST" "$ALLEVIATION_DIR" <<'PY'
import sys
from pathlib import Path

import pandas as pd

rent_base_path = Path(sys.argv[1])
rent_boost_path = Path(sys.argv[2])
alleviation_dir = Path(sys.argv[3])

rent_base = pd.read_csv(rent_base_path, index_col=0).iloc[:, 0]
rent_boost = pd.read_csv(rent_boost_path, index_col=0).iloc[:, 0]
common = rent_base.index.intersection(rent_boost.index)
delta = (rent_base.reindex(common) - rent_boost.reindex(common)).sort_values(ascending=False)

csvs = sorted(alleviation_dir.glob("*.csv"))
if not csvs:
    raise RuntimeError(f"No alleviation CSV written to {alleviation_dir}")

latest = max(csvs, key=lambda p: p.stat().st_mtime)
df = pd.read_csv(latest)
required = {"line_id", "rent_base_eur", "rent_boost_eur", "saving_eur"}
missing = required.difference(df.columns)
if missing:
    raise RuntimeError(f"Alleviation output missing columns: {sorted(missing)}")

print("Final checks passed:")
print(f"  alleviation csv : {latest}")
print(f"  total saving    : {df['saving_eur'].sum() / 1e6:.2f} M EUR")
if not delta.empty:
    top_line = delta.index[0]
    print(f"  top rent drop   : {top_line} -> {delta.iloc[0] / 1e6:.2f} M EUR")
PY

echo "Shadow-price counterfactual workflow completed successfully."
