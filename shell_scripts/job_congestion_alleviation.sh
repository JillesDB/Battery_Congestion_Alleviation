#!/usr/bin/env bash
#BSUB -q man
#BSUB -J pypsa_congestion_cost_alleviation
#BSUB -n 8
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=16GB]"
#BSUB -M 16GB
#BSUB -W 12:00
#BSUB -o hpc_output_and_error_files/Output_Congestion_cost_alleviation_%J.out
#BSUB -e hpc_output_and_error_files/Output_Congestion_cost_alleviation_%J.err

set -euo pipefail

# ══════════════════════════════════════════════════════════════════════════════
# CONGESTION COST ALLEVIATION — GridBooster battery (Kupferzell, 250 MW)
# ══════════════════════════════════════════════════════════════════════════════
#
# PREREQUISITE
# ------------
# Run job_congestion_occurrence_pypsa.sh first (method=dual, target_area=corridor).
# This script reads its outputs from $OCC_DIR.
#
# METHOD SELECTION
# ----------------
# Three method blocks are written out below. UNCOMMENT exactly one block.
# The two other blocks must remain commented out.
#
#   METHOD A — SIMPLE
#       Binary deployment: every hour where any corridor line has μ > tol,
#       the full α×P_bat MW is deployed (distributed across simultaneously
#       congested lines by shadow-price weight). No additional LOPF needed.
#       Congestion metric: n_congested_lines per hour from mu_upper.
#       → Upper-frequency bound on avoided-cost hours.
#
#   METHOD B — ONE-LINE
#       Battery permanently committed to TARGET_LINE for the full year.
#       A single dedicated LOPF re-solve uprates TARGET_LINE's s_max_pu and
#       produces the counterfactual flow f_boost. Avoided volume per hour is
#       Δf = |f_boost| − |f_base|, masked to hours where TARGET_LINE binds.
#       Congestion metric: hours where TARGET_LINE has μ > tol AND Δf > 0.
#       → Lower bound (flexibility forfeited by fixing to one line).
#
#   METHOD C — OPTIMAL
#       Ex-post best assignment: one dedicated boost solve per corridor line.
#       Each hour, the battery is assigned to the line producing the largest
#       positive Δf across all per-line boost runs. Avoided volume is that
#       maximum Δf, masked to hours where any corridor line binds.
#       Congestion metric: hours where any line has μ > tol AND best Δf > 0.
#       → Upper bound on volume (the flexibility premium of dynamic commitment
#         = optimal total_volume_mwh − one_line total_volume_mwh is reportable).
#
# OUTPUT FILES — in $OUT_DIR/<method>/
# ----------------------------------------
#   alleviation_hourly_battery250mw_alpha1.00_simple.csv       (Method A)
#   alleviation_kpi_battery250mw_alpha1.00_simple.csv
#   alleviation_hourly_battery250mw_alpha1.00_one_line.csv     (Method B)
#   alleviation_kpi_battery250mw_alpha1.00_one_line.csv
#   alleviation_hourly_battery250mw_alpha1.00_optimal.csv      (Method C)
#   alleviation_kpi_battery250mw_alpha1.00_optimal.csv
# ══════════════════════════════════════════════════════════════════════════════

# ── Fixed project paths ───────────────────────────────────────────────────────
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
PYPSA_EUR_DIR="/zhome/26/e/209460/PycharmProjects/pypsa-eur"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"
ALLEVIATION_SCRIPT="${PROJECT_DIR}/congestion_cost_alleviation.py"
WORKFLOW_SCRIPT="${PROJECT_DIR}/research_workflow.py"

# ── Shared parameters — edit these before submitting ─────────────────────────
BATTERY_MW="250"
ALPHA="1.0"
COST_YEAR="mean"          # 2022 | 2023 | 2024 | 2025 | mean

# ── Input paths (produced by job_congestion_occurrence_pypsa.sh) ──────────────
RESULTS_ROOT="${PROJECT_DIR}/results"
OCC_DIR="${RESULTS_ROOT}/kupferzell_simple/congestion_occurrence"

# Shared inputs consumed by all three methods:
MU_CSV="${OCC_DIR}/congestion_corridor_dual_2025_mu_upper.csv"
SNOM_CSV="${OCC_DIR}/corridor_s_nom_2025.csv"

# Inputs consumed by one-line and optimal methods (Δf comparison):
F_BASE_CSV="${OCC_DIR}/corridor_f_base_abs_mw_2025.csv"

# Solved network — used as the starting point for boost LOPF re-solves:
SOLVED_NET="${PYPSA_EUR_DIR}/results/kupferzell_2024_simple/networks/base_s_256_elec_.nc"

# ── Output directories (one per method, so outputs never overwrite each other) ─
OUT_SIMPLE="${RESULTS_ROOT}/kupferzell_simple/congestion_alleviation/simple"
OUT_ONE_LINE="${RESULTS_ROOT}/kupferzell_simple/congestion_alleviation/one_line"
OUT_OPTIMAL="${RESULTS_ROOT}/kupferzell_simple/congestion_alleviation/optimal"
BOOST_SOLVE_DIR="${RESULTS_ROOT}/kupferzell_simple/boost_solves"

# ── ONE-LINE: set TARGET_LINE before submitting with Method B ─────────────────
# Inspect corridor_congestion_shadow_long_2025.csv to find the top candidate:
#   python3 -c "
#     import pandas as pd
#     df = pd.read_csv('${OCC_DIR}/corridor_congestion_shadow_long_2025.csv')
#     print(df.groupby('line_id')['mu_abs'].sum().sort_values(ascending=False).head(5))
#   "
TARGET_LINE="<set_from_occurrence_output>"   # e.g. "Line 5234"

# ── Environment setup ─────────────────────────────────────────────────────────
module purge || true
module load python3/3.12.4 || true

source "${VENV_ACTIVATE}"
cd "${PROJECT_DIR}"

mkdir -p hpc_output_and_error_files

echo "════════════════════════════════════════════════════════════════════════════"
echo "  CONGESTION COST ALLEVIATION  — Kupferzell GridBooster ${BATTERY_MW} MW"
echo "════════════════════════════════════════════════════════════════════════════"
echo "Host          : $(hostname)"
echo "Date          : $(date)"
echo "Python        : $(which python3)"
echo "Battery MW    : ${BATTERY_MW}"
echo "Alpha         : ${ALPHA}"
echo "Cost year     : ${COST_YEAR}"
echo "OCC_DIR       : ${OCC_DIR}"
echo ""

# ── Input validation ──────────────────────────────────────────────────────────
# mu_upper is mandatory — cannot be generated without the full occurrence run.
if [[ ! -f "${MU_CSV}" ]]; then
    echo "ERROR: mu_upper CSV not found: ${MU_CSV}" >&2
    echo "       Run job_congestion_occurrence_pypsa.sh first." >&2
    exit 1
fi

# s_nom and f_base are written by the Step 2 supplement in the occurrence job.
# If missing (occurrence job run without the supplement), generate them now.
if [[ ! -f "${SNOM_CSV}" || ! -f "${F_BASE_CSV}" ]]; then
    echo "WARNING: corridor_s_nom_2025.csv or corridor_f_base_abs_mw_2025.csv missing."
    echo "         Generating inline from network. Re-running the occurrence job is preferred."
    python3 - "${SOLVED_NET}" "${OCC_DIR}" "${MU_CSV}" <<'PY'
import sys
from pathlib import Path
import pandas as pd
import pypsa
network_path, occ_dir, mu_csv_path = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
SIM_YEAR = 2025
n   = pypsa.Network(network_path)
mu  = pd.read_csv(mu_csv_path, index_col=0, parse_dates=True)
corridor_lines = mu.columns.tolist()
snom_path = occ_dir / f"corridor_s_nom_{SIM_YEAR}.csv"
if not snom_path.exists():
    s_nom = n.lines["s_nom"].reindex(corridor_lines).rename("s_nom_mw")
    s_nom.to_csv(snom_path, header=True)
    print(f"[fallback-saved] {snom_path.name}")
fbase_path = occ_dir / f"corridor_f_base_abs_mw_{SIM_YEAR}.csv"
if not fbase_path.exists():
    f_base = n.lines_t.p0.abs().reindex(columns=corridor_lines, fill_value=0.0)
    f_base.to_csv(fbase_path)
    print(f"[fallback-saved] {fbase_path.name}")
PY
fi
if [[ ! -f "${SOLVED_NET}" ]]; then
    echo "ERROR: Solved network not found: ${SOLVED_NET}" >&2
    exit 1
fi


## ════════════════════════════════════════════════════════════════════════════════
## ▓▓▓  METHOD A — SIMPLE  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
##
## CONGESTION COUNT METRIC:
##   An hour t is counted as congested when any corridor line l has |μ_l,t| > tol.
##   The number of simultaneously congested lines (n_congested) is recorded but
##   does not reduce the battery deployment — full α×P_bat MW is applied to every
##   congested hour regardless of how many lines bind at once.
##
## VOLUME METRIC:
##   volume_avoided_mwh_t = α × P_bat × Δt   (for all congested hours t)
##   Distributed across simultaneously congested lines proportionally to |μ_l,t|.
##   Summing across all lines and all hours gives total_volume_mwh.
##
## NO additional LOPF solve needed. Reads only mu_upper.csv and s_nom.csv.
## ─────────────────────────────────────────────────────────────────────────────
#
#mkdir -p "${OUT_SIMPLE}"
#
#echo "[METHOD A — SIMPLE]"
#echo "  Inputs  : mu_upper, s_nom"
#echo "  Output  : ${OUT_SIMPLE}/"
#echo ""
#
#python3 "${ALLEVIATION_SCRIPT}" \
#    --run-mode         simple \
#    --mu-base-csv      "${MU_CSV}" \
#    --s-nom-csv        "${SNOM_CSV}" \
#    --output-dir       "${OUT_SIMPLE}" \
#    --battery-mw       "${BATTERY_MW}" \
#    --alpha            "${ALPHA}" \
#    --redispatch-cost-year "${COST_YEAR}"
#
#echo ""
#echo "[METHOD A — SIMPLE] completed. KPIs in ${OUT_SIMPLE}/alleviation_kpi_battery${BATTERY_MW}mw_alpha${ALPHA}_simple.csv"
#echo "════════════════════════════════════════════════════════════════════════════"

# ▓▓▓  END METHOD A  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
# ════════════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════════════
# ▓▓▓  METHOD B — ONE-LINE  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
#
 CONGESTION COUNT METRIC:
   An hour t contributes avoided volume only when:
     (i)  TARGET_LINE has |μ_t| > tol   (line is congested in base solve), AND
     (ii) Δf_t = |f_boost_t| − |f_base_t| > 0  (boost solve carries more flow).
   Hours where other lines bind but TARGET_LINE does not are NOT counted.
   This is the lower bound: flexibility forfeited by fixing to one line.

 VOLUME METRIC:
   volume_avoided_mwh_t = Δf_t × Δt   (for hours satisfying both conditions)
   Δf comes from a dedicated LOPF re-solve where TARGET_LINE's s_max_pu is
   raised by α×P_bat/s_nom (Step B1 below), and base flows from f_base.csv.

 REQUIRES:
   - TARGET_LINE set at top of this script.
   - F_BASE_CSV from occurrence step.
   - One additional LOPF re-solve (Step B1).
 ─────────────────────────────────────────────────────────────────────────────

 mkdir -p "${OUT_ONE_LINE}"
 mkdir -p "${BOOST_SOLVE_DIR}"

 # Validate one-line specific inputs
 if [[ "${TARGET_LINE}" == "<set_from_occurrence_output>" ]]; then
     echo "ERROR: Set TARGET_LINE at the top of this script before running Method B." >&2
     exit 1
 fi
 if [[ ! -f "${F_BASE_CSV}" ]]; then
     echo "ERROR: f_base CSV not found: ${F_BASE_CSV}" >&2
     echo "       Ensure congestion_occurence_pypsa.py was run with method=dual." >&2
     exit 1
 fi

 echo "[METHOD B — ONE-LINE]  Target line: ${TARGET_LINE}"
 echo ""

 # ── Step B1: boost LOPF re-solve — uprate TARGET_LINE only ──────────────────
 # Raises s_max_pu on TARGET_LINE by α×P_bat/s_nom, re-solves the full LOPF,
 # and saves line_flow_abs_mw_2025_boost_mw250_a1.00_line<ID>.csv.
 # Runtime: ~same as the original Snakemake solve (~20–60 min depending on host).
 echo "[METHOD B — Step B1] Boost LOPF re-solve on ${TARGET_LINE} ..."

 python3 "${WORKFLOW_SCRIPT}" \
     --mode          solve \
     --input-network "${SOLVED_NET}" \
     --output-dir    "${BOOST_SOLVE_DIR}" \
     --battery-mw    "${BATTERY_MW}" \
     --alpha         "${ALPHA}" \
     --target-area   corridor \
     --boost-lines   "${TARGET_LINE}"

 # Construct the boost flow CSV filename produced by research_workflow.py
 SAFE_ID="${TARGET_LINE// /_}"
 SAFE_ID="${SAFE_ID//\//-}"
 F_BOOST_SINGLE="${BOOST_SOLVE_DIR}/line_flow_abs_mw_2025_boost_mw${BATTERY_MW%.*}_a${ALPHA}_line${SAFE_ID}.csv"

 if [[ ! -f "${F_BOOST_SINGLE}" ]]; then
     echo "ERROR: Expected boost flow CSV not found: ${F_BOOST_SINGLE}" >&2
     exit 1
 fi
 echo "[METHOD B — Step B1] Boost solve complete. Flow CSV: ${F_BOOST_SINGLE}"
 echo ""

 # ── Step B2: one-line alleviation calculation ────────────────────────────────
 echo "[METHOD B — Step B2] One-line alleviation calculation ..."

 python3 "${ALLEVIATION_SCRIPT}" \
     --run-mode          one-line \
     --mu-base-csv       "${MU_CSV}" \
     --s-nom-csv         "${SNOM_CSV}" \
     --f-base-csv        "${F_BASE_CSV}" \
     --f-boost-csv       "${F_BOOST_SINGLE}" \
     --target-line       "${TARGET_LINE}" \
     --output-dir        "${OUT_ONE_LINE}" \
     --battery-mw        "${BATTERY_MW}" \
     --alpha             "${ALPHA}" \
     --redispatch-cost-year "${COST_YEAR}"

 echo ""
 echo "[METHOD B — ONE-LINE] completed. KPIs in ${OUT_ONE_LINE}/alleviation_kpi_battery${BATTERY_MW}mw_alpha${ALPHA}_one_line.csv"
 echo "════════════════════════════════════════════════════════════════════════════"

# ▓▓▓  END METHOD B  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
# ════════════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════════════
# ▓▓▓  METHOD C — OPTIMAL  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
#
# CONGESTION COUNT METRIC:
#   An hour t contributes avoided volume only when:
#     (i)  Any corridor line has |μ_t| > tol   (at least one line congested), AND
#     (ii) max_l Δf_{l,t} > 0  (at least one per-line boost produces positive Δf).
#   Each hour is assigned to the line producing the largest Δf (ex-post optimal).
#   This is the upper bound on avoided volume.
#
# VOLUME METRIC:
#   volume_avoided_mwh_t = max_l [Δf_{l,t}] × Δt   (best line per hour)
#   where Δf_{l,t} = |f_boost_l,t| − |f_base_t| from each line's dedicated solve.
#
# REQUIRES:
#   - F_BASE_CSV from occurrence step.
#   - One LOPF re-solve per corridor line (Step C1 loop — runtime scales linearly
#     with the number of corridor lines found in mu_upper.csv).
#   - A manifest JSON file listing {line_id, f_boost_single_csv} per line (Step C2).
# ─────────────────────────────────────────────────────────────────────────────

# mkdir -p "${OUT_OPTIMAL}"
# mkdir -p "${BOOST_SOLVE_DIR}"
#
# if [[ ! -f "${F_BASE_CSV}" ]]; then
#     echo "ERROR: f_base CSV not found: ${F_BASE_CSV}" >&2
#     echo "       Ensure congestion_occurence_pypsa.py was run with method=dual." >&2
#     exit 1
# fi
#
# echo "[METHOD C — OPTIMAL]"
# echo ""
#
# # ── Step C1: per-line boost LOPF re-solves ───────────────────────────────────
# # Extract corridor line ids from mu_upper CSV header, then loop one solve each.
# # Each solve raises that line's s_max_pu by α×P_bat/s_nom and saves its flow CSV.
# # NOTE: this loop runs N solves sequentially. If N is large (>10), consider
# #       splitting into separate bsub jobs and waiting for all to finish before C2.
# echo "[METHOD C — Step C1] Extracting corridor lines from mu_upper CSV ..."
#
# CORRIDOR_LINES=$(python3 - "${MU_CSV}" <<'PY'
# import sys, pandas as pd
# cols = pd.read_csv(sys.argv[1], index_col=0, nrows=0).columns.tolist()
# print(" ".join(cols))
# PY
# )
#
# N_LINES=$(echo "${CORRIDOR_LINES}" | wc -w)
# echo "[METHOD C — Step C1] Found ${N_LINES} corridor lines. Running ${N_LINES} boost solves ..."
# echo ""
#
# for LINE_ID in ${CORRIDOR_LINES}; do
#     echo "  [Solve] ${LINE_ID}"
#     python3 "${WORKFLOW_SCRIPT}" \
#         --mode          solve \
#         --input-network "${SOLVED_NET}" \
#         --output-dir    "${BOOST_SOLVE_DIR}" \
#         --battery-mw    "${BATTERY_MW}" \
#         --alpha         "${ALPHA}" \
#         --target-area   corridor \
#         --boost-lines   "${LINE_ID}"
#     echo "  [Done]  ${LINE_ID}"
# done
#
# echo ""
# echo "[METHOD C — Step C1] All ${N_LINES} boost solves complete."
# echo ""
#
# # ── Step C2: build boost manifest JSON ───────────────────────────────────────
# # Maps each line id to its per-line boost flow CSV path.
# # research_workflow.py names the file: line_flow_abs_mw_2025_boost_mw<N>_a<A>_line<ID>.csv
# MANIFEST_PATH="${BOOST_SOLVE_DIR}/boost_manifest_optimal.json"
#
# echo "[METHOD C — Step C2] Building boost manifest JSON ..."
#
# python3 - "${BOOST_SOLVE_DIR}" "${BATTERY_MW}" "${ALPHA}" "${MANIFEST_PATH}" <<'PY'
# import sys, json
# from pathlib import Path
#
# boost_dir   = Path(sys.argv[1])
# battery_mw  = int(float(sys.argv[2]))
# alpha       = float(sys.argv[3])
# manifest_path = Path(sys.argv[4])
#
# pattern = f"line_flow_abs_mw_2025_boost_mw{battery_mw}_a{alpha:.2f}_line*.csv"
# files   = sorted(boost_dir.glob(pattern))
#
# if not files:
#     raise RuntimeError(
#         f"No boost flow CSVs found matching {pattern} in {boost_dir}.\n"
#         "Check that Step C1 completed without errors."
#     )
#
# manifest = []
# for f in files:
#     # Filename: line_flow_abs_mw_2025_boost_mw250_a1.00_line<ID>.csv
#     # Strip prefix and suffix to recover the line id (may contain spaces/dashes)
#     prefix = f"line_flow_abs_mw_2025_boost_mw{battery_mw}_a{alpha:.2f}_line"
#     line_id_safe = f.stem[len(prefix):]
#     # Reverse the safe-id substitution applied in research_workflow.py
#     line_id = line_id_safe.replace("_", " ").replace("-", "/")
#     # Heuristic: if the original id was numeric, no substitution was applied
#     manifest.append({"line_id": line_id, "f_boost_single_csv": str(f)})
#
# manifest_path.write_text(json.dumps(manifest, indent=2))
# print(f"Manifest written: {manifest_path}")
# print(f"Entries: {len(manifest)}")
# for entry in manifest:
#     print(f"  {entry['line_id']} -> {Path(entry['f_boost_single_csv']).name}")
# PY
#
# if [[ ! -f "${MANIFEST_PATH}" ]]; then
#     echo "ERROR: Manifest JSON was not created: ${MANIFEST_PATH}" >&2
#     exit 1
# fi
# echo ""
#
# # ── Step C3: optimal alleviation calculation ─────────────────────────────────
# echo "[METHOD C — Step C3] Optimal alleviation calculation ..."
#
# python3 "${ALLEVIATION_SCRIPT}" \
#     --run-mode            optimal \
#     --mu-base-csv         "${MU_CSV}" \
#     --s-nom-csv           "${SNOM_CSV}" \
#     --f-base-csv          "${F_BASE_CSV}" \
#     --boost-manifest-json "${MANIFEST_PATH}" \
#     --output-dir          "${OUT_OPTIMAL}" \
#     --battery-mw          "${BATTERY_MW}" \
#     --alpha               "${ALPHA}" \
#     --redispatch-cost-year "${COST_YEAR}"
#
# echo ""
# echo "[METHOD C — OPTIMAL] completed. KPIs in ${OUT_OPTIMAL}/alleviation_kpi_battery${BATTERY_MW}mw_alpha${ALPHA}_optimal.csv"
# echo "════════════════════════════════════════════════════════════════════════════"

# ▓▓▓  END METHOD C  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
# ════════════════════════════════════════════════════════════════════════════════

echo ""
echo "Job completed successfully."