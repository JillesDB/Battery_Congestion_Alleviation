#!/usr/bin/env bash
#BSUB -q man
#BSUB -J pypsa_alleviation_full_simple
#BSUB -n 8
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=16GB]"
#BSUB -M 16GB
#BSUB -W 12:00
#BSUB -o hpc_output_and_error_files/Output_Congestion_Cost_alleviation_full_%J.out
#BSUB -e hpc_output_and_error_files/Output_Congestion_Cost_alleviation_full_%J.err

set -euo pipefail
unset GRB_LICENSE_FILE
export GRB_LICENSE_FILE=$HOME/gurobi/gurobi.lic
# ══════════════════════════════════════════════════════════════════════════════
# CONGESTION COST ALLEVIATION — GridBooster battery (Kupferzell, 250 MW)
# Scenario: kupferzell_full  |  Method: simple
#
# Prerequisite: job_congestion_occurrence_pypsa_full.sh must have
# completed successfully before submitting this job.
# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────────
# │  TOGGLES  — the only lines you need to edit before submitting
# ├─────────────────────────────────────────────────────────────────────────────
SCENARIO="full"               # simple | full              ← match occurrence job
CONGESTION_METHOD="dual"      # dual | loading | ...       ← match occurrence job
ALLEVIATION_METHOD="optimal_alleviation"   # simple | one_line | optimal_alleviation
# └─────────────────────────────────────────────────────────────────────────────

# ── Battery / cost parameters ─────────────────────────────────────────────────
BATTERY_MW="250"
ALPHA="1.0"
COST_YEAR="mean"              # 2022 | 2023 | 2024 | 2025 | mean

# ── Less-commonly changed parameters ──────────────────────────────────────────
SIM_YEAR="2025"

# ── Fixed project paths ───────────────────────────────────────────────────────
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
PYPSA_EUR_DIR="/zhome/26/e/209460/PycharmProjects/pypsa-eur"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"
ALLEVIATION_SCRIPT="${PROJECT_DIR}/congestion_cost_alleviation.py"
WORKFLOW_SCRIPT="${PROJECT_DIR}/research_workflow.py"

# ── Derived paths (auto-set from toggles — do not edit) ───────────────────────
RESULTS_ROOT="${PROJECT_DIR}/results"
OCC_DIR="${RESULTS_ROOT}/kupferzell_${SCENARIO}/congestion_occurrence"
MU_CSV="${OCC_DIR}/congestion_corridor_${CONGESTION_METHOD}_${SIM_YEAR}_mu_upper.csv"
SNOM_CSV="${OCC_DIR}/corridor_s_nom_${SIM_YEAR}.csv"
F_BASE_CSV="${OCC_DIR}/corridor_f_base_abs_mw_${SIM_YEAR}.csv"
SOLVED_NET="${PYPSA_EUR_DIR}/results/kupferzell_2024_${SCENARIO}/networks/base_s_256_elec_.nc"
OUT_DIR="${RESULTS_ROOT}/kupferzell_${SCENARIO}/congestion_alleviation/${ALLEVIATION_METHOD}"
BOOST_SOLVE_DIR="${RESULTS_ROOT}/kupferzell_${SCENARIO}/boost_solves"

# ── Environment setup ─────────────────────────────────────────────────────────
module purge || true
module load python3/3.12.4 || true

source "${VENV_ACTIVATE}"
cd "${PROJECT_DIR}"

mkdir -p hpc_output_and_error_files
mkdir -p "${OUT_DIR}"

echo "════════════════════════════════════════════════════════════════════════════"
echo "  CONGESTION COST ALLEVIATION  — Kupferzell GridBooster ${BATTERY_MW} MW"
echo "════════════════════════════════════════════════════════════════════════════"
echo "Host              : $(hostname)"
echo "Date              : $(date)"
echo "Python            : $(which python3)"
echo "Scenario          : ${SCENARIO}"
echo "Congestion method : ${CONGESTION_METHOD}"
echo "Alleviation mode  : ${ALLEVIATION_METHOD}"
echo "Battery MW        : ${BATTERY_MW}"
echo "Alpha             : ${ALPHA}"
echo "Cost year         : ${COST_YEAR}"
echo "OCC_DIR           : ${OCC_DIR}"
echo "Output dir        : ${OUT_DIR}"
echo "════════════════════════════════════════════════════════════════════════════"
echo ""

# ── Input validation ──────────────────────────────────────────────────────────
if [[ ! -f "${MU_CSV}" ]]; then
    echo "ERROR: mu_upper CSV not found: ${MU_CSV}" >&2
    echo "       Run job_congestion_occurrence_pypsa_kupferzell_full.sh first" >&2
    echo "       with SCENARIO=${SCENARIO} and CONGESTION_METHOD=${CONGESTION_METHOD}." >&2
    exit 1
fi

if [[ ! -f "${SOLVED_NET}" ]]; then
    echo "ERROR: Solved network not found: ${SOLVED_NET}" >&2
    exit 1
fi

# Generate s_nom / f_base inline if the occurrence supplement was skipped.
if [[ ! -f "${SNOM_CSV}" || ! -f "${F_BASE_CSV}" ]]; then
    echo "WARNING: corridor_s_nom or corridor_f_base_abs_mw CSV missing."
    echo "         Generating inline from network. Re-running the occurrence job is preferred."
    python3 - "${SOLVED_NET}" "${OCC_DIR}" "${MU_CSV}" "${SIM_YEAR}" <<'PY'
import sys
from pathlib import Path
import pandas as pd, pypsa

network_path, occ_dir, mu_csv_path, sim_year = (
    sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4])
n  = pypsa.Network(network_path)
mu = pd.read_csv(mu_csv_path, index_col=0, parse_dates=True)
corridor_lines = mu.columns.tolist()

snom_path = occ_dir / f"corridor_s_nom_{sim_year}.csv"
if not snom_path.exists():
    n.lines["s_nom"].reindex(corridor_lines).rename("s_nom_mw").to_csv(snom_path, header=True)
    print(f"[fallback-saved] {snom_path.name}")

fbase_path = occ_dir / f"corridor_f_base_abs_mw_{sim_year}.csv"
if not fbase_path.exists():
    n.lines_t.p0.abs().reindex(columns=corridor_lines, fill_value=0.0).to_csv(fbase_path)
    print(f"[fallback-saved] {fbase_path.name}")
PY
fi

# ══════════════════════════════════════════════════════════════════════════════
# METHOD A — SIMPLE
# ══════════════════════════════════════════════════════════════════════════════
echo "[METHOD A — SIMPLE]"
echo "  Every hour where any corridor line has |μ| > tol: deploy full α×P_bat MW."
echo "  No additional LOPF re-solve needed."
echo ""

python3 "${ALLEVIATION_SCRIPT}" \
    --run-mode             simple \
    --mu-base-csv          "${MU_CSV}" \
    --s-nom-csv            "${SNOM_CSV}" \
    --output-dir           "${OUT_DIR}" \
    --battery-mw           "${BATTERY_MW}" \
    --alpha                "${ALPHA}" \
    --redispatch-cost-year "${COST_YEAR}"

echo ""
echo "[METHOD A — SIMPLE] completed."
echo "KPIs: ${OUT_DIR}/alleviation_kpi_battery${BATTERY_MW}mw_alpha$(printf '%.2f' ${ALPHA})_simple.csv"

# ══════════════════════════════════════════════════════════════════════════════
# MERGE STEP — refresh the 3-series merged hourly CSV
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "[MERGE] Refreshing 3-series merged alleviation CSV …"

python3 - "${PROJECT_DIR}" "${SCENARIO}" "${SIM_YEAR}" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from congestion_cost_alleviation import merge_alleviation_revenues
merge_alleviation_revenues(scenario=sys.argv[2], year=int(sys.argv[3]))
PY

echo "[MERGE] Done."

echo ""
echo "════════════════════════════════════════════════════════════════════════════"
echo "Job completed successfully."
echo "════════════════════════════════════════════════════════════════════════════"
