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
#   flat_one_line — binary deployment every congested hour; no extra LOPF solve.
#              Upper-frequency bound on avoided-cost hours.
#   dynamic_one_line — battery committed to TARGET_LINE for the full year; one LOPF
#              re-solve. Lower bound on avoided volume.
#   dynamic_multiple_lines — one LOPF re-solve per corridor line; best Δf assigned per hour.
#              Upper bound on avoided volume.
# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────────
# │  TOGGLES  — the only lines you need to edit before submitting
# ├─────────────────────────────────────────────────────────────────────────────
SCENARIO="kupferzell_full"             # kupferzell_simple | kupferzell_full
CONGESTION_METHOD="dual"      # dual | loading | ...       ← match occurrence job
ALLEVIATION_METHOD="dynamic_multiple_lines"     # flat_one_line | dynamic_one_line | dynamic_multiple_lines
TARGET_AREA="custom_lines"  # kupferzell_node | kupferzell_corridor | kupferzell_brochure_line_selection | custom_lines | all  ← match occurrence job
HARD_RERUN="false"   # true | false — when true, re-solves boost LOPFs even if CSVs exist
# Optional: pass custom lines instead of using target_area.
# Format: comma-separated IDs or JSON array string, e.g. "111,222,333" or '{"111","222"}'
# When set, custom_lines overrides target_area selection.
CUSTOM_LINES="262,350,328,179,334,269,341,312,270,178,310,176,94,277,95,276,79,80,267,316,177,311"            # e.g. "Line 5234, Line 5235" — leave empty to use target_area

# Optional: override the auto-selected target line for ALLEVIATION_METHOD=dynamic_one_line.
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
OCC_DIR="${RESULTS_ROOT}/${SCENARIO}/congestion_occurrence"
MU_CSV="${OCC_DIR}/congestion_corridor_${CONGESTION_METHOD}_${SIM_YEAR}_mu_upper.csv"
SNOM_CSV="${OCC_DIR}/corridor_s_nom_${SIM_YEAR}.csv"
F_BASE_CSV="${OCC_DIR}/corridor_f_base_abs_mw_${SIM_YEAR}.csv"
PYPSA_SCENARIO="${SCENARIO#kupferzell_}"
SOLVED_NET="${PYPSA_EUR_DIR}/results/kupferzell_2024_${PYPSA_SCENARIO}/networks/base_s_256_elec_.nc"
OUT_DIR="${RESULTS_ROOT}/${SCENARIO}/congestion_alleviation/${ALLEVIATION_METHOD}"
BOOST_SOLVE_DIR="${RESULTS_ROOT}/${SCENARIO}/boost_solves"

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
# METHOD A — FLAT ONE-LINE
# ══════════════════════════════════════════════════════════════════════════════
if [[ "${ALLEVIATION_METHOD}" == "flat_one_line" ]]; then

    echo "[METHOD A — FLAT ONE-LINE]"
    echo "  Single most-congested line (by congested hours): full α×P_bat MW each of its congested hours."
    echo "  No additional LOPF re-solve needed."
    echo ""

    python3 "${ALLEVIATION_SCRIPT}" \
        --run-mode             flat-one-line \
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
    echo "[METHOD A — FLAT ONE-LINE] completed."
    echo "KPIs: ${OUT_DIR}/alleviation_kpi_flat_one_line_battery${BATTERY_MW}mw_alpha$(printf '%.2f' ${ALPHA}).csv"

    # Sanity check: volume_avoided_mwh equals alpha*battery_mw when > 0.
    python3 - "${OUT_DIR}" "${BATTERY_MW}" "${ALPHA}" <<'PY'
import sys
from pathlib import Path
import numpy as np
import pandas as pd

out_dir = Path(sys.argv[1])
battery_mw = float(sys.argv[2])
alpha = float(sys.argv[3])
expected = battery_mw * alpha

cands = [p for p in out_dir.glob("alleviation_hourly_*.csv")
         if "_kpi_" not in p.name and "_assignment_" not in p.name
         and not p.name.endswith("_monthly_summary.csv")]
if not cands:
    print("[SANITY] flat_one_line: no hourly CSV found; skipping.")
    sys.exit(0)

csv_path = max(cands, key=lambda p: p.stat().st_mtime)
df = pd.read_csv(csv_path)
if "volume_avoided_mwh" not in df.columns:
    print(f"[SANITY] flat_one_line: missing volume_avoided_mwh in {csv_path.name}; skipping.")
    sys.exit(0)

vol = pd.to_numeric(df["volume_avoided_mwh"], errors="coerce").fillna(0.0)
mask = vol > 0
if not mask.any():
    print("[SANITY] flat_one_line: no positive volume rows found.")
    sys.exit(0)

bad = ~np.isclose(vol[mask], expected, atol=1e-6)
if bad.any():
    count = int(bad.sum())
    sample = vol[mask][bad].head(5).tolist()
    raise SystemExit(f"[SANITY] flat_one_line: {count} rows have volume_avoided_mwh != {expected}. Sample: {sample}")

print(f"[SANITY] flat_one_line: volume_avoided_mwh matches {expected} for all positive rows.")
PY

# ══════════════════════════════════════════════════════════════════════════════
# METHOD B — DYNAMIC ONE-LINE
# ══════════════════════════════════════════════════════════════════════════════
elif [[ "${ALLEVIATION_METHOD}" == "dynamic_one_line" ]]; then

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

    echo "[METHOD B — DYNAMIC ONE-LINE]  Target line: ${TARGET_LINE}"
    echo "  Battery permanently committed to TARGET_LINE for the full year."
    echo "  Avoided volume = Δf = |f_boost| − |f_base| on congested hours."
    echo ""

    # Step B1: boost LOPF re-solve — uprate TARGET_LINE by α×P_bat/s_nom
    SAFE_ID="${TARGET_LINE// /_}"
    SAFE_ID="${SAFE_ID//\//-}"
    F_BOOST_SINGLE="${BOOST_SOLVE_DIR}/line_flow_abs_mw_${SIM_YEAR}_boost_mw${BATTERY_MW%.*}_a${ALPHA_FMT}_line${SAFE_ID}.csv"

    echo "[Step B1] Boost LOPF on '${TARGET_LINE}' (Python handles cache) …"
        python3 "${WORKFLOW_SCRIPT}" \
            --mode          solve \
            --input-network "${SOLVED_NET}" \
            --output-dir    "${BOOST_SOLVE_DIR}" \
            --battery-mw    "${BATTERY_MW}" \
            --alpha         "${ALPHA}" \
            --target-area   "${TARGET_AREA}" \
            $(if [[ -n "${CUSTOM_LINES}" ]]; then echo "--custom-lines" "${CUSTOM_LINES}"; fi) \
            --boost-lines   "${TARGET_LINE}" \
            $(if [[ "${HARD_RERUN}" == "true" ]]; then echo "--hard-rerun"; fi)

        if [[ ! -f "${F_BOOST_SINGLE}" ]]; then
            echo "ERROR: Expected boost flow CSV not produced: ${F_BOOST_SINGLE}" >&2
            exit 1
        fi
        echo "[Step B1] Complete. Flow CSV: ${F_BOOST_SINGLE}"

    # Step B2: one-line alleviation calculation
    echo "[Step B2] One-line alleviation calculation …"

    python3 "${ALLEVIATION_SCRIPT}" \
        --run-mode             dynamic-one-line \
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
    echo "[METHOD B — DYNAMIC ONE-LINE] completed."
    echo "KPIs: ${OUT_DIR}/alleviation_kpi_dynamic_one_line_battery${BATTERY_MW}mw_alpha$(printf '%.2f' ${ALPHA}).csv"

    # Sanity check: delta_f_mw in [0, alpha*battery_mw] for all hours.
    python3 - "${OUT_DIR}" "${BATTERY_MW}" "${ALPHA}" <<'PY'
import sys
from pathlib import Path
import numpy as np
import pandas as pd

out_dir = Path(sys.argv[1])
battery_mw = float(sys.argv[2])
alpha = float(sys.argv[3])
max_mw = battery_mw * alpha

cands = [p for p in out_dir.glob("alleviation_hourly_*.csv")
         if "_kpi_" not in p.name and "_assignment_" not in p.name
         and not p.name.endswith("_monthly_summary.csv")]
if not cands:
    print("[SANITY] dynamic_one_line: no hourly CSV found; skipping.")
    sys.exit(0)

csv_path = max(cands, key=lambda p: p.stat().st_mtime)
df = pd.read_csv(csv_path)
if "delta_f_mw" not in df.columns:
    print(f"[SANITY] dynamic_one_line: missing delta_f_mw in {csv_path.name}; skipping.")
    sys.exit(0)

vals = pd.to_numeric(df["delta_f_mw"], errors="coerce").fillna(0.0)
low_bad = vals < -1e-6
high_bad = vals > max_mw + 1e-6
if low_bad.any() or high_bad.any():
    count = int((low_bad | high_bad).sum())
    sample = vals[low_bad | high_bad].head(5).tolist()
    raise SystemExit(f"[SANITY] dynamic_one_line: {count} rows outside [0, {max_mw}]. Sample: {sample}")

print(f"[SANITY] dynamic_one_line: delta_f_mw within [0, {max_mw}] for all rows.")
PY

# ══════════════════════════════════════════════════════════════════════════════
# METHOD C — DYNAMIC MULTIPLE-LINES
# ══════════════════════════════════════════════════════════════════════════════
elif [[ "${ALLEVIATION_METHOD}" == "dynamic_multiple_lines" ]]; then

    mkdir -p "${BOOST_SOLVE_DIR}"

    echo "[METHOD C — DYNAMIC MULTIPLE-LINES]"
    echo "  One LOPF re-solve per corridor line; best Δf assigned per hour."
    echo "  Runtime scales linearly with the number of corridor lines."
    echo ""

    echo "[Step C1] Extracting CONGESTED corridor lines from mu_upper CSV …"
    LINES_FILE="$(mktemp)"
    python3 - "${MU_CSV}" "${LINES_FILE}" "${PROJECT_DIR}" <<'PY'
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, sys.argv[3])
from congestion_cost_alleviation import DUAL_TOL

mu_csv      = sys.argv[1]
lines_file  = sys.argv[2]

mu = pd.read_csv(mu_csv, index_col=0, parse_dates=True)
all_cols = mu.columns.tolist()

hours_per_line = (mu.abs() > DUAL_TOL).sum(axis=0)
congested_cols = hours_per_line[hours_per_line > 0].index.tolist()
skipped_cols   = [c for c in all_cols if c not in congested_cols]

with open(lines_file, "w") as f:
    f.write("\n".join(congested_cols) + ("\n" if congested_cols else ""))

print(f"[Step C1] Total corridor lines in mu_upper : {len(all_cols)}")
print(f"[Step C1] Congested (|mu|>{DUAL_TOL} in ≥1 h): {len(congested_cols)}")
print(f"[Step C1] Skipped (never bind, 0 MWh relief): {len(skipped_cols)}")
if skipped_cols:
    preview = ", ".join(skipped_cols[:10])
    suffix  = " …" if len(skipped_cols) > 10 else ""
    print(f"[Step C1] Skipped line ids: {preview}{suffix}")
PY

    N_LINES=$(wc -l < "${LINES_FILE}")
    ALPHA_FMT=$(printf '%.2f' "${ALPHA}")
    N_SOLVED=0
    N_SKIPPED=0

    if [[ "${N_LINES}" -eq 0 ]]; then
        echo "ERROR: No congested corridor lines found in ${MU_CSV}." >&2
        echo "       All |mu| values are below DUAL_TOL — nothing to alleviate." >&2
        rm -f "${LINES_FILE}"
        exit 1
    fi

    echo "[Step C1] Will solve boost LOPF for ${N_LINES} congested line(s)."
    echo ""

    while IFS= read -r LINE_ID; do
            echo "  [Run]   ${LINE_ID}"
            python3 "${WORKFLOW_SCRIPT}" \
                --mode          solve \
                --input-network "${SOLVED_NET}" \
                --output-dir    "${BOOST_SOLVE_DIR}" \
                --battery-mw    "${BATTERY_MW}" \
                --alpha         "${ALPHA}" \
                --target-area   "${TARGET_AREA}" \
                $(if [[ -n "${CUSTOM_LINES}" ]]; then echo "--custom-lines" "${CUSTOM_LINES}"; fi) \
                --boost-lines   "${LINE_ID}" \
                $(if [[ "${HARD_RERUN}" == "true" ]]; then echo "--hard-rerun"; fi)
        done < "${LINES_FILE}"

    echo ""
    echo "[Step C1] Complete: ${N_SOLVED} new solve(s), ${N_SKIPPED} loaded from cache."
    echo ""

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
        --run-mode             dynamic-multiple-lines \
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
    echo "[METHOD C — DYNAMIC MULTIPLE-LINES] completed."
    echo "KPIs: ${OUT_DIR}/alleviation_kpi_dynamic_multiple_lines_battery${BATTERY_MW}mw_alpha$(printf '%.2f' ${ALPHA}).csv"

    # Sanity checks: assigned_line in binding set and volume_mwh_optimal <= alpha*battery_mw.
    python3 - "${OUT_DIR}" "${BATTERY_MW}" "${ALPHA}" "${MU_CSV}" "${PROJECT_DIR}" <<'PY'
import sys
from pathlib import Path
import numpy as np
import pandas as pd

out_dir = Path(sys.argv[1])
battery_mw = float(sys.argv[2])
alpha = float(sys.argv[3])
mu_csv = Path(sys.argv[4])
project_dir = Path(sys.argv[5])
max_mw = battery_mw * alpha

sys.path.insert(0, str(project_dir))
from congestion_cost_alleviation import DUAL_TOL

cands = [p for p in out_dir.glob("alleviation_hourly_*.csv")
         if "_kpi_" not in p.name and "_assignment_" not in p.name
         and not p.name.endswith("_monthly_summary.csv")]
if not cands:
    print("[SANITY] dynamic_multiple_lines: no hourly CSV found; skipping.")
    sys.exit(0)

csv_path = max(cands, key=lambda p: p.stat().st_mtime)
df = pd.read_csv(csv_path)

if "volume_mwh_optimal" in df.columns:
    vol = pd.to_numeric(df["volume_mwh_optimal"], errors="coerce").fillna(0.0)
    high_bad = vol > max_mw + 1e-6
    if high_bad.any():
        count = int(high_bad.sum())
        sample = vol[high_bad].head(5).tolist()
        raise SystemExit(f"[SANITY] dynamic_multiple_lines: {count} rows with volume_mwh_optimal > {max_mw}. Sample: {sample}")
    print(f"[SANITY] dynamic_multiple_lines: volume_mwh_optimal <= {max_mw} for all rows.")
else:
    print(f"[SANITY] dynamic_multiple_lines: missing volume_mwh_optimal in {csv_path.name}; skipping volume check.")

if "assigned_line" in df.columns:
    if not mu_csv.exists():
        print(f"[SANITY] dynamic_multiple_lines: mu_upper CSV missing: {mu_csv}; skipping assigned_line check.")
        sys.exit(0)
    mu = pd.read_csv(mu_csv, index_col=0, parse_dates=True)
    binding_raw = mu.columns.tolist()

    def _norm_line(val):
        if pd.isna(val):
            return None
        s = str(val).strip()
        try:
            f = float(s)
            if f.is_integer():
                return str(int(f))
        except Exception:
            pass
        return s

    binding = set(_norm_line(c) for c in binding_raw if _norm_line(c) is not None)
    assigned_raw = df["assigned_line"].dropna()
    assigned_norm = assigned_raw.map(_norm_line).dropna()

    bad_mask = ~assigned_norm.isin(binding)
    if bad_mask.any():
        bad_raw = assigned_raw[bad_mask].head(5).tolist()
        bad_norm = assigned_norm[bad_mask].head(5).tolist()
        binding_sample = sorted(list(binding))[:10]
        raise SystemExit(
            "[SANITY] dynamic_multiple_lines: assigned_line not in binding set. "
            f"Bad raw: {bad_raw}; bad normalized: {bad_norm}; "
            f"binding size: {len(binding)}; binding sample: {binding_sample}"
        )
    print("[SANITY] dynamic_multiple_lines: assigned_line values are within binding set.")
else:
    print(f"[SANITY] dynamic_multiple_lines: missing assigned_line in {csv_path.name}; skipping assigned_line check.")
PY

else
    echo "ERROR: Unknown ALLEVIATION_METHOD='${ALLEVIATION_METHOD}'." >&2
    echo "       Choose: flat_one_line | dynamic_one_line | dynamic_multiple_lines" >&2
    exit 1
fi

# ── MERGE REVENUE CSVS ────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════════════════"
echo "  MERGING REVENUE CSVS"
echo "════════════════════════════════════════════════════════════════════════════"

python3 - "${PROJECT_DIR}" "${SCENARIO}" "${SIM_YEAR}" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from congestion_cost_alleviation import merge_alleviation_revenues
merge_alleviation_revenues(scenario=sys.argv[2], year=int(sys.argv[3]))
PY

# Sanity checks on merged CSV for line 178 binding hours.
python3 - "${PROJECT_DIR}" "${RESULTS_ROOT}" "${SCENARIO}" "${SIM_YEAR}" "${MU_CSV}" <<'PY'
import sys
from pathlib import Path
import numpy as np
import pandas as pd

project_dir = Path(sys.argv[1])
results_root = Path(sys.argv[2])
scenario = sys.argv[3]
year = int(sys.argv[4])
mu_csv = Path(sys.argv[5])

sys.path.insert(0, str(project_dir))
from congestion_cost_alleviation import DUAL_TOL

merged = (results_root / scenario / "congestion_alleviation" / f"alleviation_revenues_merged_{year}.csv")
if not merged.exists():
    print(f"[SANITY] merged CSV not found: {merged}; skipping.")
    sys.exit(0)

if not mu_csv.exists():
    print(f"[SANITY] mu_upper CSV not found: {mu_csv}; skipping.")
    sys.exit(0)

m = pd.read_csv(merged)
mu = pd.read_csv(mu_csv, index_col=0, parse_dates=True)

m_ts = next((c for c in m.columns if str(c).strip().lower() in ("time_cet", "timestamp", "time")), None)
if m_ts is None:
    print(f"[SANITY] merged CSV missing timestamp column: {merged}")
    sys.exit(1)

m[m_ts] = pd.to_datetime(m[m_ts])
m = m.set_index(m_ts).sort_index()

if "178" not in mu.columns:
    print("[SANITY] mu_upper does not include line 178; skipping line-178 checks.")
    sys.exit(0)

mu178 = mu["178"].abs().reindex(m.index, fill_value=0.0)
mask = mu178 > DUAL_TOL
if not mask.any():
    print("[SANITY] no binding hours for line 178; skipping ordering check.")
    sys.exit(0)

col_multi = next((c for c in m.columns if c in (
    "congestion_relief_dynamic_multiple_lines_eur",
    "congestion_relief_optimal_eur",
)), None)
col_one = next((c for c in m.columns if c in (
    "congestion_relief_dynamic_one_line_eur",
    "congestion_relief_one_line_eur",
)), None)
col_flat = next((c for c in m.columns if c in (
    "congestion_relief_flat_one_line_eur",
    "congestion_relief_simple_eur",
)), None)

if col_multi is None or col_one is None:
    print("[SANITY] merged CSV missing required columns for ordering check; skipping.")
    sys.exit(0)

multi = pd.to_numeric(m[col_multi], errors="coerce").fillna(0.0)
one = pd.to_numeric(m[col_one], errors="coerce").fillna(0.0)
flat = pd.to_numeric(m[col_flat], errors="coerce").fillna(0.0) if col_flat else None

bad = (multi[mask] + 1e-9 < one[mask]) | (one[mask] < -1e-9)
if bad.any():
    count = int(bad.sum())
    sample = m.loc[mask].iloc[bad.values].head(5).index.astype(str).tolist()
    raise SystemExit(f"[SANITY] merged ordering failed (multi >= one >= 0) at {count} timestamps. Sample: {sample}")

if col_flat is not None:
    lt_flat = (multi[mask] + 1e-9 < flat[mask])
    if lt_flat.any():
        count = int(lt_flat.sum())
        print(f"[SANITY] note: multi < flat at {count} line-178 binding hours (allowed if other lines dominate).")

print("[SANITY] merged ordering check passed for line 178 binding hours.")
PY
