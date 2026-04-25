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

# ══════════════════════════════════════════════════════════════════════════════
# CONGESTION OCCURRENCE — PyPSA shadow-price extraction
# ══════════════════════════════════════════════════════════════════════════════
#
# PURPOSE
# -------
# Reads the solved PyPSA network (kupferzell_2024_simple) and extracts
# hourly shadow prices (mu_upper) for all corridor lines within 1.3° of
# Kupferzell. These outputs are the SHARED INPUT for all three alleviation
# methods (simple, one-line, optimal) in job_congestion_alleviation.sh.
#
# This script must be run ONCE before any of the three alleviation modes.
#
# OUTPUTS — written to $OUTPUT_ROOT/kupferzell_simple/congestion_occurrence/
# ---------------------------------------------------------------------------
# Used by ALL THREE alleviation methods:
#   congestion_corridor_dual_2025_mu_upper.csv
#       Shadow prices (μ) per hour per corridor line [EUR/MW·h].
#       Positive μ in a given hour means that line's thermal constraint binds
#       in the LOPF solution — this is the primary congestion flag.
#
#   corridor_s_nom_2025.csv
#       Thermal ratings [MW] for each corridor line (index = line_id).
#
# Used by ONE-LINE and OPTIMAL methods only (require Δf comparison):
#   corridor_f_base_abs_mw_2025.csv
#       Absolute base-case power flows |p0| [MW] per hour per line.
#       Used to compute Δf = |f_boost| − |f_base| after each boost solve.
#
# Diagnostic / paper tables (not directly consumed by alleviation script):
#   corridor_congestion_shadow_long_2025.csv
#       Long-format table: one row per congested line-hour, with mu_abs,
#       s_nom_mw, n_congested (simultaneous lines), and rank_in_hour.
#       USE THIS to identify TARGET_LINE for the one-line method:
#         python3 -c "
#           import pandas as pd
#           df = pd.read_csv('results/kupferzell_simple/congestion_occurrence/corridor_congestion_shadow_long_2025.csv')
#           print(df.groupby('line_id')['mu_abs'].sum().sort_values(ascending=False).head(5))
#         "
#   corridor_congestion_shadow_wide_2025.csv
#       Wide-format: timestamp × line, with raw μ values.
#   corridor_congestion_concurrency_2025.csv
#       How many lines bind simultaneously — breakdown table.
#   congestion_corridor_dual_2025_by_line.csv
#       Per-line summary: congested hours, mean/p95/max loading, congestion rent.
#   congestion_corridor_dual_2025_monthly.csv
#       Monthly aggregation of congested line-hours.
#   kupferzell_line_proximity_hourly_2025.csv
#       Loading-fraction table consumed by the legacy run_legacy() pipeline.
#   congestion_corridor_dual_2025_congestion_rent_eur_per_line.csv
#       Total LP congestion rent Σ_t |μ| × s_nom per line [EUR].
# ══════════════════════════════════════════════════════════════════════════════

# ── Fixed project paths ───────────────────────────────────────────────────────
PROJECT_DIR="/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation"
VENV_ACTIVATE="/zhome/26/e/209460/venvs/kupferzell/bin/activate"
CONGESTION_SCRIPT="$PROJECT_DIR/congestion_occurence_pypsa.py"

# ── User-configurable parameters ──────────────────────────────────────────────
# Positional arguments (all optional — defaults shown):
#   $1  NETWORK_PATH   Solved PyPSA .nc file
#   $2  OUTPUT_ROOT    Root directory for all results
#   $3  THRESHOLD      Base-case loading threshold (unused in dual mode, kept for compatibility)
#   $4  THRESHOLD_N1   N-1 post-contingency threshold (unused in dual mode)
#   $5  MINIMUM_VOLTAGE_RAW  Minimum line voltage [kV]; 0 = no filter
#   $6  REQUESTED_LINES      Comma-separated line ids to restrict analysis; empty = auto
#   $7  METHOD         Congestion detection method: dual | loading | n_minus_1 | redispatch_trigger
#   $8  TARGET_AREA    Line selection: corridor | kupferzell | all

NETWORK_PATH="${1:-/zhome/26/e/209460/PycharmProjects/pypsa-eur/results/kupferzell_2024_simple/networks/base_s_256_elec_.nc}"
OUTPUT_ROOT="${2:-/zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation/results}"
THRESHOLD="${3:-0.98}"
THRESHOLD_N1="${4:-1.00}"
MINIMUM_VOLTAGE_RAW="${5:-0}"
REQUESTED_LINES="${6:-}"
METHOD="${7:-dual}"
TARGET_AREA="${8:-corridor}"

# ── Environment setup ─────────────────────────────────────────────────────────
module purge || true
module load python3/3.12.4 || true

source "$VENV_ACTIVATE"
cd "$PROJECT_DIR"

mkdir -p hpc_output_and_error_files
mkdir -p "$OUTPUT_ROOT"

# ── Diagnostics ───────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════════════════════════"
echo "  CONGESTION OCCURRENCE — SHADOW-PRICE EXTRACTION"
echo "════════════════════════════════════════════════════════════════════════════"
echo "Host          : $(hostname)"
echo "Date          : $(date)"
echo "Python        : $(which python3)"
echo "Network       : $NETWORK_PATH"
echo "Output root   : $OUTPUT_ROOT"
echo "Method        : $METHOD"
echo "Target area   : $TARGET_AREA"
echo "Threshold     : $THRESHOLD"
echo "Threshold N-1 : $THRESHOLD_N1"

# ── Minimum voltage parsing (backwards-compat: non-numeric arg 5 → line ids) ─
if [[ "$MINIMUM_VOLTAGE_RAW" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    MINIMUM_VOLTAGE="$MINIMUM_VOLTAGE_RAW"
else
    MINIMUM_VOLTAGE="0"
    if [[ -n "$MINIMUM_VOLTAGE_RAW" ]]; then
        REQUESTED_LINES="${REQUESTED_LINES:-$MINIMUM_VOLTAGE_RAW}"
    fi
fi

echo "Min voltage   : $MINIMUM_VOLTAGE kV"
echo "Req. lines    : ${REQUESTED_LINES:-<all lines in target area>}"
echo ""

# ── Input validation ──────────────────────────────────────────────────────────
if [[ ! -f "$NETWORK_PATH" ]]; then
    echo "ERROR: Network file does not exist: $NETWORK_PATH" >&2
    exit 1
fi

# ── Build and execute command ─────────────────────────────────────────────────
CMD=(
    python3 "$CONGESTION_SCRIPT"
    --network        "$NETWORK_PATH"
    --output-dir     "$OUTPUT_ROOT"
    --threshold      "$THRESHOLD"
    --threshold-n1   "$THRESHOLD_N1"
    --minimum-voltage "$MINIMUM_VOLTAGE"
    --method         "$METHOD"
    --target-area    "$TARGET_AREA"
)

if [[ -n "$REQUESTED_LINES" ]]; then
    CMD+=(--lines "$REQUESTED_LINES")
fi

echo "Executing: ${CMD[*]}"
echo "════════════════════════════════════════════════════════════════════════════"
echo ""

"${CMD[@]}"

echo ""

# ── Step 2: post-processing supplement ───────────────────────────────────────
# Generates two files that congestion_occurence_pypsa.py does not currently
# export, and fixes the shadow_long CSV which uses the wrong dual_tol (0.98
# loading threshold instead of 1e-3 EUR/MWh):
#   corridor_s_nom_2025.csv           (needed by all three alleviation methods)
#   corridor_f_base_abs_mw_2025.csv   (needed by one-line and optimal methods)
#   corridor_congestion_shadow_long_2025.csv  (regenerated with dual_tol=1e-3)

SCENARIO="kupferzell_simple"
if [[ "$NETWORK_PATH" == *"_full"* ]]; then
    SCENARIO="kupferzell_full"
fi
OCC_DIR="${OUTPUT_ROOT}/${SCENARIO}/congestion_occurrence"

echo "════════════════════════════════════════════════════════════════════════════"
echo "  POST-PROCESSING SUPPLEMENT"
echo "  Writing : corridor_s_nom_2025.csv, corridor_f_base_abs_mw_2025.csv"
echo "  Fixing  : corridor_congestion_shadow_long_2025.csv (dual_tol=1e-3)"
echo "  Dir     : $OCC_DIR"
echo "════════════════════════════════════════════════════════════════════════════"

python3 - "$NETWORK_PATH" "$OCC_DIR" <<'PY'
import sys
from pathlib import Path
import pandas as pd
import pypsa

NETWORK_PATH = sys.argv[1]
OCC_DIR      = Path(sys.argv[2])
SIM_YEAR     = 2025
DUAL_TOL     = 1e-3  # correct shadow-price threshold [EUR/MWh]

print("Loading network ...")
n   = pypsa.Network(NETWORK_PATH)
mu_csv = OCC_DIR / f"congestion_corridor_dual_{SIM_YEAR}_mu_upper.csv"
if not mu_csv.exists():
    raise FileNotFoundError(f"mu_upper CSV not found: {mu_csv}. Step 1 must complete first.")
mu = pd.read_csv(mu_csv, index_col=0, parse_dates=True)
corridor_lines = mu.columns.tolist()
print(f"Corridor lines (from mu_upper columns): {len(corridor_lines)}")

# 1. corridor_s_nom_2025.csv
s_nom_path = OCC_DIR / f"corridor_s_nom_{SIM_YEAR}.csv"
s_nom = n.lines["s_nom"].reindex(corridor_lines).rename("s_nom_mw")
missing = s_nom[s_nom.isna()].index.tolist()
if missing:
    raise ValueError(f"Lines in mu_upper not found in network.lines: {missing[:5]}")
s_nom.to_csv(s_nom_path, header=True)
print(f"[saved] {s_nom_path.name}  ({len(s_nom)} lines)")

# 2. corridor_f_base_abs_mw_2025.csv
f_base_path = OCC_DIR / f"corridor_f_base_abs_mw_{SIM_YEAR}.csv"
f_base = n.lines_t.p0.abs().reindex(columns=corridor_lines, fill_value=0.0)
f_base.to_csv(f_base_path)
print(f"[saved] {f_base_path.name}  ({f_base.shape[0]} h x {f_base.shape[1]} lines)")

# 3. Regenerate shadow_long/wide/concurrency with dual_tol=1e-3
mu_abs = mu.abs()
congested_mask = mu_abs > DUAL_TOL
n_cong_per_hour = congested_mask.sum(axis=1).rename("n_congested")

records = []
for ts in mu_abs.index:
    cong_lines = mu_abs.columns[congested_mask.loc[ts]]
    if len(cong_lines) == 0:
        continue
    mu_vals = mu_abs.loc[ts, cong_lines].sort_values(ascending=False)
    for rank, (lid, mu_val) in enumerate(mu_vals.items(), start=1):
        records.append({
            "timestamp": ts, "line_id": lid, "mu_abs": float(mu_val),
            "s_nom_mw": float(n.lines.at[lid, "s_nom"]) if lid in n.lines.index else float("nan"),
            "n_congested": int(n_cong_per_hour.loc[ts]), "rank_in_hour": rank,
        })

hourly_long = pd.DataFrame(records) if records else pd.DataFrame(
    columns=["timestamp", "line_id", "mu_abs", "s_nom_mw", "n_congested", "rank_in_hour"])
long_path = OCC_DIR / f"corridor_congestion_shadow_long_{SIM_YEAR}.csv"
hourly_long.to_csv(long_path, index=False, float_format="%.5f")
print(f"[saved] {long_path.name}  ({len(hourly_long)} congested line-hours, dual_tol=1e-3)")

hourly_wide = mu.where(congested_mask, other=0.0)
wide_path = OCC_DIR / f"corridor_congestion_shadow_wide_{SIM_YEAR}.csv"
hourly_wide.to_csv(wide_path, float_format="%.5f")
print(f"[saved] {wide_path.name}")

if not hourly_long.empty:
    per_hour = (hourly_long.groupby("timestamp")["n_congested"].first()
                .value_counts().sort_index().reset_index())
    per_hour.columns = ["n_congested_lines", "hours"]
    per_hour["share_pct"] = (per_hour["hours"] / per_hour["hours"].sum() * 100).round(2)
else:
    per_hour = pd.DataFrame(columns=["n_congested_lines", "hours", "share_pct"])
conc_path = OCC_DIR / f"corridor_congestion_concurrency_{SIM_YEAR}.csv"
per_hour.to_csv(conc_path, index=False)
print(f"[saved] {conc_path.name}")

n_cong_h = int(congested_mask.any(axis=1).sum())
n_cong_lh = int(congested_mask.values.sum())
print(f"\nSupplement summary (dual_tol=1e-3): {n_cong_h} congested hours, {n_cong_lh} line-hours")
if not hourly_long.empty:
    print("\nTop 5 lines by rent proxy (use for TARGET_LINE selection):")
    rent = mu_abs.where(congested_mask, 0.0).multiply(
        n.lines["s_nom"].reindex(corridor_lines).fillna(0.0), axis=1).sum().sort_values(ascending=False)
    for lid, val in rent.head(5).items():
        print(f"  {lid:<40s}  rent_proxy={val:>12.0f}  cong_h={int(congested_mask[lid].sum()):>4d}")
PY

echo ""
echo "════════════════════════════════════════════════════════════════════════════"
echo "Congestion occurrence step completed."
echo ""
echo "Key output files in $OUTPUT_ROOT/kupferzell_simple/congestion_occurrence/ :"
echo "  [ALL METHODS]  congestion_corridor_dual_2025_mu_upper.csv"
echo "  [ALL METHODS]  corridor_s_nom_2025.csv"
echo "  [ONE-LINE / OPTIMAL]  corridor_f_base_abs_mw_2025.csv"
echo "  [DIAGNOSTICS]  corridor_congestion_shadow_long_2025.csv"
echo "  [DIAGNOSTICS]  corridor_congestion_concurrency_2025.csv"
echo ""
echo "Before running one-line alleviation, identify TARGET_LINE:"
echo "  python3 -c \""
echo "    import pandas as pd"
OCC_DIR="${OUTPUT_ROOT}/kupferzell_simple/congestion_occurrence"
echo "    df = pd.read_csv('${OCC_DIR}/corridor_congestion_shadow_long_2025.csv')"
echo "    print(df.groupby('line_id')['mu_abs'].sum().sort_values(ascending=False).head(5))"
echo "  \""
echo "════════════════════════════════════════════════════════════════════════════"