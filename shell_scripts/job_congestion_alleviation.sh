#!/usr/bin/env bash
#BSUB -q man
#BSUB -J pypsa_congestion_alleviation
#BSUB -n 8
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=16GB]"
#BSUB -M 16GB
#BSUB -W 12:00
#BSUB -o hpc_output_and_error_files/Output_Congestion_Cost_alleviation_%J.out
#BSUB -e hpc_output_and_error_files/Output_Congestion_Cost_alleviation_%J.err

set -euo pipefail
unset GRB_LICENSE_FILE
export GRB_LICENSE_FILE=$HOME/gurobi/gurobi.lic
# ══════════════════════════════════════════════════════════════════════════════
# CONGESTION COST ALLEVIATION — GridBooster battery (Kupferzell, 250 MW)
#
# Prerequisite: run job_congestion_occurrence_pypsa.sh first, using the same
# SCENARIO and CONGESTION_METHOD as set below.
#
# Three alleviation methods:
#   simple   — binary deployment every congested hour; no extra LOPF solve.
#              Upper-frequency bound on avoided-cost hours.
#   one_line — battery committed to TARGET_LINE for the full year; one LOPF
#              re-solve. Lower bound on avoided volume.
#   optimal  — one LOPF re-solve per corridor line; best Δf assigned per hour.
#              Upper bound on avoided volume.
# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────────
# │  TOGGLES  — the only lines you need to edit before submitting
# ├─────────────────────────────────────────────────────────────────────────────
SCENARIO="full"             # simple | full              ← match occurrence job
CONGESTION_METHOD="dual"      # dual | loading | ...       ← match occurrence job
ALLEVIATION_METHOD="optimal_alleviation"     # simple | one_line | optimal_alleviation
TARGET_AREA="custom_lines"  # kupferzell_node | kupferzell_corridor | kupferzell_brochure_line_selection | custom_lines | all  ← match occurrence job

# Optional: pass custom lines instead of using target_area.
# Format: comma-separated IDs or JSON array string, e.g. "111,222,333" or '{"111","222"}'
# When set, custom_lines overrides target_area selection.
CUSTOM_LINES="262,350,328,179,334,269,341,312,270,178,310,176,94,277,95,276,79,80,267,316,177,311"            # e.g. "Line 5234, Line 5235" — leave empty to use target_area

# Optional: override the auto-selected target line for ALLEVIATION_METHOD=one_line.
# When empty (default), the line with the most congested hours (|mu|>0.1 EUR/MWh)
# is auto-selected directly from the mu_upper CSV.
TARGET_LINE=""                # e.g. "Line 5234" — leave empty for auto-selection
# └─────────────────────────────────────────────────────────────────────────────

# ── Battery / cost parameters ─────────────────────────────────────────────────
BATTERY_MW="250"
ALPHA="1.0"
COST_YEAR="2025"              # 2022 | 2023 | 2024 | 2025 | mean

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
echo "Target area       : ${TARGET_AREA}"
echo "Custom lines      : ${CUSTOM_LINES:-<none, using target_area>}"
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
    echo "       Run job_congestion_occurrence_pypsa.sh first" >&2
    echo "       with SCENARIO=${SCENARIO}, CONGESTION_METHOD=${CONGESTION_METHOD}," >&2
    echo "       and TARGET_AREA=${TARGET_AREA}." >&2
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
if [[ "${ALLEVIATION_METHOD}" == "simple" ]]; then

    echo "[METHOD A — SIMPLE]"
    echo "  Single most-congested line (by congested hours): full α×P_bat MW each of its congested hours."
    echo "  No additional LOPF re-solve needed."
    echo ""

    python3 "${ALLEVIATION_SCRIPT}" \
        --run-mode             simple \
        --mu-base-csv          "${MU_CSV}" \
        --s-nom-csv            "${SNOM_CSV}" \
        --network              "${SOLVED_NET}" \
        --output-dir           "${OUT_DIR}" \
        --battery-mw           "${BATTERY_MW}" \
        --alpha                "${ALPHA}" \
        --redispatch-cost-year "${COST_YEAR}" \
        --target-area          "${TARGET_AREA}" \
        $(if [[ -n "${CUSTOM_LINES}" ]]; then echo "--custom-lines" "${CUSTOM_LINES}"; fi)

    echo ""
    echo "[METHOD A — SIMPLE] completed."
    echo "KPIs: ${OUT_DIR}/alleviation_kpi_battery${BATTERY_MW}mw_alpha$(printf '%.2f' ${ALPHA})_simple.csv"

# ══════════════════════════════════════════════════════════════════════════════
# METHOD B — ONE-LINE
# ══════════════════════════════════════════════════════════════════════════════
elif [[ "${ALLEVIATION_METHOD}" == "one_line" ]]; then

    ALPHA_FMT=$(printf '%.2f' "${ALPHA}")
    # Auto-derive TARGET_LINE using MWh-relief ranking from pre-computed boost CSVs.
    if [[ -z "${TARGET_LINE}" ]]; then
        if [[ ! -f "${MU_CSV}" ]]; then
            echo "ERROR: TARGET_LINE is unset and mu_upper CSV not found: ${MU_CSV}" >&2
            echo "       Run job_congestion_occurrence_pypsa.sh first." >&2
            exit 1
        fi
        if [[ ! -f "${F_BASE_CSV}" ]]; then
            echo "ERROR: TARGET_LINE is unset and f_base CSV not found: ${F_BASE_CSV}" >&2
            echo "       Run job_congestion_occurrence_pypsa.sh first (Step 2 generates this file)." >&2
            exit 1
        fi
        TARGET_LINE=$(python3 - "${MU_CSV}" "${F_BASE_CSV}" "${BOOST_SOLVE_DIR}" "${BATTERY_MW}" "${ALPHA_FMT}" "${SIM_YEAR}" "${PROJECT_DIR}" <<'PY'
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, sys.argv[7])
from congestion_cost_alleviation import _select_target_line_by_mwh_relief, DUAL_TOL

mu_wide    = pd.read_csv(sys.argv[1], index_col=0, parse_dates=True)
f_base     = pd.read_csv(sys.argv[2], index_col=0, parse_dates=True)
boost_dir  = Path(sys.argv[3])
battery_mw = float(sys.argv[4])
alpha      = float(sys.argv[5])
sim_year   = int(sys.argv[6])

target_line, _relief = _select_target_line_by_mwh_relief(
    mu_wide=mu_wide,
    f_base_wide=f_base,
    boost_dir=boost_dir,
    battery_mw=battery_mw,
    alpha=alpha,
    dual_tol=DUAL_TOL,
    scenario_year=sim_year,
)
print(target_line)
PY
        )
        echo "[METHOD B] TARGET_LINE auto-selected (max MWh relief): ${TARGET_LINE}"
    fi
    mkdir -p "${BOOST_SOLVE_DIR}"

    echo "[METHOD B — ONE-LINE]  Target line: ${TARGET_LINE}"
    echo "  Battery permanently committed to TARGET_LINE for the full year."
    echo "  Avoided volume = Δf = |f_boost| − |f_base| on congested hours."
    echo ""

    # Step B1: boost LOPF re-solve — uprate TARGET_LINE by α×P_bat/s_nom
    # Construct expected output path first so we can skip the solve if already done.
    SAFE_ID="${TARGET_LINE// /_}"
    SAFE_ID="${SAFE_ID//\//-}"
    F_BOOST_SINGLE="${BOOST_SOLVE_DIR}/line_flow_abs_mw_${SIM_YEAR}_boost_mw${BATTERY_MW%.*}_a${ALPHA_FMT}_line${SAFE_ID}.csv"

    if [[ -f "${F_BOOST_SINGLE}" ]]; then
        echo "[Step B1] Boost flow CSV already exists — skipping re-solve."
        echo "          ${F_BOOST_SINGLE}"
    else
        echo "[Step B1] Boost LOPF re-solve on '${TARGET_LINE}' …"
        python3 "${WORKFLOW_SCRIPT}" \
            --mode          solve \
            --input-network "${SOLVED_NET}" \
            --output-dir    "${BOOST_SOLVE_DIR}" \
            --battery-mw    "${BATTERY_MW}" \
            --alpha         "${ALPHA}" \
            --target-area   "${TARGET_AREA}" \
            $(if [[ -n "${CUSTOM_LINES}" ]]; then echo "--custom-lines" "${CUSTOM_LINES}"; fi) \
            --boost-lines   "${TARGET_LINE}"

        if [[ ! -f "${F_BOOST_SINGLE}" ]]; then
            echo "ERROR: Expected boost flow CSV not found after solve: ${F_BOOST_SINGLE}" >&2
            exit 1
        fi
        echo "[Step B1] Complete. Flow CSV: ${F_BOOST_SINGLE}"
    fi
    echo ""

    # Step B2: one-line alleviation calculation
    echo "[Step B2] One-line alleviation calculation …"

    python3 "${ALLEVIATION_SCRIPT}" \
        --run-mode             one-line \
        --mu-base-csv          "${MU_CSV}" \
        --s-nom-csv            "${SNOM_CSV}" \
        --f-base-csv           "${F_BASE_CSV}" \
        --f-boost-csv          "${F_BOOST_SINGLE}" \
        --target-line          "${TARGET_LINE}" \
        --network              "${SOLVED_NET}" \
        --output-dir           "${OUT_DIR}" \
        --battery-mw           "${BATTERY_MW}" \
        --alpha                "${ALPHA}" \
        --redispatch-cost-year "${COST_YEAR}" \
        --target-area          "${TARGET_AREA}" \
        $(if [[ -n "${CUSTOM_LINES}" ]]; then echo "--custom-lines" "${CUSTOM_LINES}"; fi)

    echo ""
    echo "[METHOD B — ONE-LINE] completed."
    echo "KPIs: ${OUT_DIR}/alleviation_kpi_battery${BATTERY_MW}mw_alpha$(printf '%.2f' ${ALPHA})_one_line.csv"

# ══════════════════════════════════════════════════════════════════════════════
# METHOD C — OPTIMAL
# ══════════════════════════════════════════════════════════════════════════════
elif [[ "${ALLEVIATION_METHOD}" == "optimal_alleviation" ]]; then

    mkdir -p "${BOOST_SOLVE_DIR}"

    echo "[METHOD C — OPTIMAL]"
    echo "  One LOPF re-solve per corridor line; best Δf assigned per hour."
    echo "  Runtime scales linearly with the number of corridor lines."
    echo ""

    # Step C1: per-line boost LOPF re-solves
    # Write line IDs one-per-line (handles IDs containing spaces).
    # LINES_FILE is kept alive through Step C2 for manifest building.
    echo "[Step C1] Extracting corridor lines from mu_upper CSV …"
    LINES_FILE="$(mktemp)"
    python3 - "${MU_CSV}" "${LINES_FILE}" <<'PY'
import sys, pandas as pd
cols = pd.read_csv(sys.argv[1], index_col=0, nrows=0).columns.tolist()
with open(sys.argv[2], "w") as f:
    f.write("\n".join(cols) + "\n")
PY

    N_LINES=$(wc -l < "${LINES_FILE}")
    ALPHA_FMT=$(printf '%.2f' "${ALPHA}")
    N_SOLVED=0
    N_SKIPPED=0
    echo "[Step C1] Found ${N_LINES} corridor lines."
    echo ""

    while IFS= read -r LINE_ID; do
        SAFE_LINE="${LINE_ID// /_}"
        SAFE_LINE="${SAFE_LINE//\//-}"
        BOOST_CSV="${BOOST_SOLVE_DIR}/line_flow_abs_mw_${SIM_YEAR}_boost_mw${BATTERY_MW%.*}_a${ALPHA_FMT}_line${SAFE_LINE}.csv"

        if [[ -f "${BOOST_CSV}" ]]; then
            echo "  [Skip]  ${LINE_ID}  (already solved)"
            (( N_SKIPPED++ )) || true
        else
            echo "  [Solve] ${LINE_ID}"
            python3 "${WORKFLOW_SCRIPT}" \
                --mode          solve \
                --input-network "${SOLVED_NET}" \
                --output-dir    "${BOOST_SOLVE_DIR}" \
                --battery-mw    "${BATTERY_MW}" \
                --alpha         "${ALPHA}" \
                --target-area   "${TARGET_AREA}" \
                $(if [[ -n "${CUSTOM_LINES}" ]]; then echo "--custom-lines" "${CUSTOM_LINES}"; fi) \
                --boost-lines   "${LINE_ID}"
            echo "  [Done]  ${LINE_ID}"
            (( N_SOLVED++ )) || true
        fi
    done < "${LINES_FILE}"

    echo ""
    echo "[Step C1] Complete: ${N_SOLVED} new solve(s), ${N_SKIPPED} loaded from cache."
    echo ""

    # Step C2: build boost manifest JSON directly from corridor line IDs.
    # Uses LINES_FILE (still in scope) to map each line to its boost CSV —
    # avoids fragile filename reverse-engineering.
    MANIFEST_PATH="${BOOST_SOLVE_DIR}/boost_manifest_optimal.json"
    echo "[Step C2] Building boost manifest JSON …"

    python3 - "${BOOST_SOLVE_DIR}" "${BATTERY_MW}" "${ALPHA}" "${SIM_YEAR}" "${MANIFEST_PATH}" "${LINES_FILE}" <<'PY'
import sys, json
from pathlib import Path

boost_dir     = Path(sys.argv[1])
battery_mw    = int(float(sys.argv[2]))
alpha         = float(sys.argv[3])
sim_year      = sys.argv[4]
manifest_path = Path(sys.argv[5])
lines_file    = Path(sys.argv[6])

corridor_lines = [ln for ln in lines_file.read_text().splitlines() if ln.strip()]
prefix = f"line_flow_abs_mw_{sim_year}_boost_mw{battery_mw}_a{alpha:.2f}_line"

manifest = []
missing  = []
for line_id in corridor_lines:
    safe = line_id.replace(" ", "_").replace("/", "-")
    csv_path = boost_dir / f"{prefix}{safe}.csv"
    if csv_path.exists():
        manifest.append({"line_id": line_id, "f_boost_single_csv": str(csv_path)})
    else:
        missing.append(line_id)

if missing:
    raise RuntimeError(
        f"Boost CSV missing for {len(missing)} corridor line(s): {missing[:5]}\n"
        "Re-run with a clean BOOST_SOLVE_DIR or check Step C1 for errors.")

manifest_path.write_text(json.dumps(manifest, indent=2))
print(f"Manifest written: {manifest_path}  ({len(manifest)} entries)")
for e in manifest:
    print(f"  {e['line_id']} -> {Path(e['f_boost_single_csv']).name}")
PY

    rm -f "${LINES_FILE}"

    if [[ ! -f "${MANIFEST_PATH}" ]]; then
        echo "ERROR: Manifest JSON not created: ${MANIFEST_PATH}" >&2
        exit 1
    fi
    echo ""

    # Step C3: optimal alleviation calculation
    echo "[Step C3] Optimal alleviation calculation …"

    python3 "${ALLEVIATION_SCRIPT}" \
        --run-mode             optimal \
        --mu-base-csv          "${MU_CSV}" \
        --s-nom-csv            "${SNOM_CSV}" \
        --f-base-csv           "${F_BASE_CSV}" \
        --boost-manifest-json  "${MANIFEST_PATH}" \
        --network              "${SOLVED_NET}" \
        --output-dir           "${OUT_DIR}" \
        --battery-mw           "${BATTERY_MW}" \
        --alpha                "${ALPHA}" \
        --redispatch-cost-year "${COST_YEAR}" \
        --target-area          "${TARGET_AREA}" \
        $(if [[ -n "${CUSTOM_LINES}" ]]; then echo "--custom-lines" "${CUSTOM_LINES}"; fi)

    echo ""
    echo "[METHOD C — OPTIMAL] completed."
    echo "KPIs: ${OUT_DIR}/alleviation_kpi_battery${BATTERY_MW}mw_alpha$(printf '%.2f' ${ALPHA})_optimal.csv"

else
    echo "ERROR: Unknown ALLEVIATION_METHOD='${ALLEVIATION_METHOD}'." >&2
    echo "       Choose: simple | one_line | optimal" >&2
    exit 1
fi


# This block invokes the merge_alleviation_revenues() function from
# congestion_cost_alleviation.py via a Python heredoc — no extra CLI flags
# need to be added to the script's existing argparse. Idempotent: every
# alleviation submission refreshes the merged CSV with whatever per-method
# results exist on disk.
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# MERGE STEP — refresh the 3-series merged hourly CSV
# Reads whatever per-method results exist on disk (under
# congestion_alleviation/{simple,one_line,optimal_alleviation}/) and writes:
#   results/kupferzell_${SCENARIO}/congestion_alleviation/
#       alleviation_revenues_merged_${SIM_YEAR}.csv
# Methods not yet run are written as zero columns; the merge is idempotent.
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
