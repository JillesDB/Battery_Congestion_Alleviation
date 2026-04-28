#!/usr/bin/env bash
#BSUB -q man
#BSUB -J pypsa_congestion_count_full
#BSUB -n 8
#BSUB -R "span[hosts=1]"
#BSUB -R "rusage[mem=8GB]"
#BSUB -M 8GB
#BSUB -W 08:00
#BSUB -o hpc_output_and_error_files/Output_Congestion_count_full_%J.out
#BSUB -e hpc_output_and_error_files/Output_Congestion_count_full_%J.err

set -euo pipefail

# ══════════════════════════════════════════════════════════════════════════════
# CONGESTION OCCURRENCE — PyPSA shadow-price extraction  (kupferzell_full)
#
# Run this script once before any alleviation job.
# Set the three TOGGLES below; everything else is auto-derived.
# ══════════════════════════════════════════════════════════════════════════════

# ┌─────────────────────────────────────────────────────────────────────────────
# │  TOGGLES  — the only lines you need to edit before submitting
# ├─────────────────────────────────────────────────────────────────────────────
SCENARIO="full"               # simple | full
CONGESTION_METHOD="dual"      # dual | loading | n_minus_1 | redispatch_trigger
TARGET_AREA="corridor"        # corridor | kupferzell | all
# └─────────────────────────────────────────────────────────────────────────────

# ── Less-commonly changed parameters ──────────────────────────────────────────
SIM_YEAR="2025"
THRESHOLD="0.98"              # loading threshold  (loading / n_minus_1 methods)
THRESHOLD_N1="1.00"           # post-contingency threshold  (n_minus_1 only)
MINIMUM_VOLTAGE="0"           # minimum line voltage [kV]; 0 = no filter
REQUESTED_LINES=""            # comma-separated line ids to restrict; empty = all

# ── Fixed project paths ───────────────────────────────────────────────────────
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
PYPSA_EUR_DIR="/zhome/26/e/209460/PycharmProjects/pypsa-eur"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"
CONGESTION_SCRIPT="${PROJECT_DIR}/congestion_occurence_pypsa.py"

# ── Derived paths (auto-set from toggles — do not edit) ───────────────────────
NETWORK_PATH="${PYPSA_EUR_DIR}/results/kupferzell_2024_${SCENARIO}/networks/base_s_256_elec_.nc"
OUTPUT_ROOT="${PROJECT_DIR}/results"
OCC_DIR="${OUTPUT_ROOT}/kupferzell_${SCENARIO}/congestion_occurrence"

# ── Environment setup ─────────────────────────────────────────────────────────
module purge || true
module load python3/3.12.4 || true

source "${VENV_ACTIVATE}"
cd "${PROJECT_DIR}"

mkdir -p hpc_output_and_error_files
mkdir -p "${OCC_DIR}"

# ── Diagnostics ───────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════════════════════════"
echo "  CONGESTION OCCURRENCE — SHADOW-PRICE EXTRACTION"
echo "════════════════════════════════════════════════════════════════════════════"
echo "Host              : $(hostname)"
echo "Date              : $(date)"
echo "Python            : $(which python3)"
echo "Scenario          : ${SCENARIO}"
echo "Congestion method : ${CONGESTION_METHOD}"
echo "Target area       : ${TARGET_AREA}"
echo "Network           : ${NETWORK_PATH}"
echo "Output dir        : ${OCC_DIR}"
echo "Threshold         : ${THRESHOLD}  (N-1: ${THRESHOLD_N1})"
echo "Min voltage       : ${MINIMUM_VOLTAGE} kV"
echo "Req. lines        : ${REQUESTED_LINES:-<all lines in target area>}"
echo "════════════════════════════════════════════════════════════════════════════"
echo ""

# ── Input validation ──────────────────────────────────────────────────────────
if [[ ! -f "${NETWORK_PATH}" ]]; then
    echo "ERROR: Network file not found: ${NETWORK_PATH}" >&2
    exit 1
fi

# ── Step 1: congestion extraction ─────────────────────────────────────────────
CMD=(
    python3 "${CONGESTION_SCRIPT}"
    --network          "${NETWORK_PATH}"
    --output-dir       "${OUTPUT_ROOT}"
    --threshold        "${THRESHOLD}"
    --threshold-n1     "${THRESHOLD_N1}"
    --minimum-voltage  "${MINIMUM_VOLTAGE}"
    --method           "${CONGESTION_METHOD}"
    --target-area      "${TARGET_AREA}"
)
[[ -n "${REQUESTED_LINES}" ]] && CMD+=(--lines "${REQUESTED_LINES}")

echo "Executing: ${CMD[*]}"
echo ""
"${CMD[@]}"
echo ""

# ── Step 2: post-processing supplement ───────────────────────────────────────
# Generates corridor_s_nom and corridor_f_base_abs_mw (consumed by the
# alleviation script), and regenerates shadow_long/wide/concurrency CSVs using
# the correct dual_tol=1e-3 instead of the loading threshold.
echo "════════════════════════════════════════════════════════════════════════════"
echo "  STEP 2 — POST-PROCESSING SUPPLEMENT"
echo "════════════════════════════════════════════════════════════════════════════"

python3 - "${NETWORK_PATH}" "${OCC_DIR}" "${CONGESTION_METHOD}" "${SIM_YEAR}" <<'PY'
import sys
from pathlib import Path
import pandas as pd
import pypsa

network_path      = sys.argv[1]
occ_dir           = Path(sys.argv[2])
congestion_method = sys.argv[3]
sim_year          = sys.argv[4]
DUAL_TOL          = 1e-3   # correct shadow-price threshold [EUR/MWh]

print("Loading network …")
n = pypsa.Network(network_path)

mu_csv = occ_dir / f"congestion_corridor_{congestion_method}_{sim_year}_mu_upper.csv"
if not mu_csv.exists():
    raise FileNotFoundError(
        f"mu_upper CSV not found: {mu_csv}\nStep 1 must complete successfully first.")
mu = pd.read_csv(mu_csv, index_col=0, parse_dates=True)
corridor_lines = mu.columns.tolist()
print(f"Corridor lines (from mu_upper columns): {len(corridor_lines)}")

# corridor_s_nom
s_nom_path = occ_dir / f"corridor_s_nom_{sim_year}.csv"
s_nom = n.lines["s_nom"].reindex(corridor_lines).rename("s_nom_mw")
missing = s_nom[s_nom.isna()].index.tolist()
if missing:
    raise ValueError(f"Lines in mu_upper not found in network.lines: {missing[:5]}")
s_nom.to_csv(s_nom_path, header=True)
print(f"[saved] {s_nom_path.name}  ({len(s_nom)} lines)")

# corridor_f_base_abs_mw
f_base_path = occ_dir / f"corridor_f_base_abs_mw_{sim_year}.csv"
f_base = n.lines_t.p0.abs().reindex(columns=corridor_lines, fill_value=0.0)
f_base.to_csv(f_base_path)
print(f"[saved] {f_base_path.name}  ({f_base.shape[0]} h x {f_base.shape[1]} lines)")

# shadow_long / shadow_wide / concurrency (regenerated with dual_tol=1e-3)
mu_abs          = mu.abs()
congested_mask  = mu_abs > DUAL_TOL
n_cong_per_hour = congested_mask.sum(axis=1).rename("n_congested")

records = []
for ts in mu_abs.index:
    cong_lines = mu_abs.columns[congested_mask.loc[ts]]
    if len(cong_lines) == 0:
        continue
    mu_vals = mu_abs.loc[ts, cong_lines].sort_values(ascending=False)
    for rank, (lid, mu_val) in enumerate(mu_vals.items(), start=1):
        records.append({
            "timestamp":   ts,
            "line_id":     lid,
            "mu_abs":      float(mu_val),
            "s_nom_mw":    float(n.lines.at[lid, "s_nom"]) if lid in n.lines.index else float("nan"),
            "n_congested": int(n_cong_per_hour.loc[ts]),
            "rank_in_hour": rank,
        })

hourly_long = (pd.DataFrame(records) if records else
               pd.DataFrame(columns=["timestamp","line_id","mu_abs","s_nom_mw","n_congested","rank_in_hour"]))
long_path = occ_dir / f"corridor_congestion_shadow_long_{sim_year}.csv"
hourly_long.to_csv(long_path, index=False, float_format="%.5f")
print(f"[saved] {long_path.name}  ({len(hourly_long)} congested line-hours, dual_tol=1e-3)")

hourly_wide = mu.where(congested_mask, other=0.0)
wide_path = occ_dir / f"corridor_congestion_shadow_wide_{sim_year}.csv"
hourly_wide.to_csv(wide_path, float_format="%.5f")
print(f"[saved] {wide_path.name}")

if not hourly_long.empty:
    per_hour = (hourly_long.groupby("timestamp")["n_congested"].first()
                .value_counts().sort_index().reset_index())
    per_hour.columns = ["n_congested_lines", "hours"]
    per_hour["share_pct"] = (per_hour["hours"] / per_hour["hours"].sum() * 100).round(2)
else:
    per_hour = pd.DataFrame(columns=["n_congested_lines", "hours", "share_pct"])
conc_path = occ_dir / f"corridor_congestion_concurrency_{sim_year}.csv"
per_hour.to_csv(conc_path, index=False)
print(f"[saved] {conc_path.name}")

n_cong_h  = int(congested_mask.any(axis=1).sum())
n_cong_lh = int(congested_mask.values.sum())
print(f"\nSummary (dual_tol=1e-3): {n_cong_h} congested hours, {n_cong_lh} line-hours")

if not hourly_long.empty:
    print("\nTop 5 lines by congested hours (use for TARGET_LINE in one-line / simple alleviation):")
    hours_per_line = congested_mask.sum(axis=0).sort_values(ascending=False)
    rent = (mu_abs.where(congested_mask, 0.0)
            .multiply(n.lines["s_nom"].reindex(corridor_lines).fillna(0.0), axis=1)
            .sum())
    for lid, n_h in hours_per_line.head(5).items():
        r = float(rent.get(lid, 0.0))
        print(f"  {lid:<40s}  cong_h={int(n_h):>4d}  rent_proxy={r:>12.0f}")
PY

echo ""
echo "════════════════════════════════════════════════════════════════════════════"
echo "Congestion occurrence step completed."
echo ""
echo "Key output files in ${OCC_DIR} :"
echo "  [ALL METHODS]       congestion_corridor_${CONGESTION_METHOD}_${SIM_YEAR}_mu_upper.csv"
echo "  [ALL METHODS]       corridor_s_nom_${SIM_YEAR}.csv"
echo "  [ONE-LINE/OPTIMAL]  corridor_f_base_abs_mw_${SIM_YEAR}.csv"
echo "  [DIAGNOSTICS]       corridor_congestion_shadow_long_${SIM_YEAR}.csv"
echo "  [DIAGNOSTICS]       corridor_congestion_concurrency_${SIM_YEAR}.csv"
echo ""
echo "Next: submit job_congestion_alleviation_kupferzell_full_*.sh with"
echo "  SCENARIO=${SCENARIO}  CONGESTION_METHOD=${CONGESTION_METHOD}"
echo "════════════════════════════════════════════════════════════════════════════"
