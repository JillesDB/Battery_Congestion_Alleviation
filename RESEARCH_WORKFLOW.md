# Research workflow

This file describes the full paper pipeline from raw PyPSA-Eur model to NPV
results. Six sequential stages, each submitted as a separate LSF job on the
HPC cluster. The two scenarios are `kupferzell_simple` (short test horizon) and
`kupferzell_full` (full 8760-hour year, publication run).

---

## Pipeline overview

```
┌─────────────────────────────────────────┐
│  0.  job_snakemake_kupferzell_*.sh      │  solve PyPSA-Eur LOPF with shadow
│      (pypsa-eur, Gurobi barrier)        │  prices → base_s_256_elec_.nc
└───────────────┬─────────────────────────┘
                ▼
┌─────────────────────────────────────────┐
│  1.  job_postprocessing_pypsa.sh        │  structural validation vs. SMARD /
│      (run_validation_pypsa.py)          │  ENTSO-E + zoomed network map
└───────────────┬─────────────────────────┘
                ▼
┌─────────────────────────────────────────┐
│  2.  job_congestion_occurrence_pypsa.sh │  extract dual shadow prices per line
│      (congestion_occurence_pypsa.py)    │  per hour → mu_upper CSV + helpers
└───────────────┬─────────────────────────┘
                ▼
┌─────────────────────────────────────────┐
│  3.  job_congestion_alleviation.sh      │  estimate congestion relief under
│      (congestion_cost_alleviation.py    │  three alleviation modes; merge +
│       + research_workflow.py)           │  map results
└───────────────┬─────────────────────────┘
                ▼
┌─────────────────────────────────────────┐
│  4.  job_estimate_merchant_revenues.sh  │  solve 365 daily 24h LPs for
│      (merchant_revenues.py)             │  unconstrained + TSO-constrained
└───────────────┬─────────────────────────┘
                ▼
┌─────────────────────────────────────────┐
│  5.  job_full_gridbooster_allocation.sh │  combine TSO relief + merchant
│      (gridbooster_allocation_model.py)  │  revenue under chosen allocation rule
└───────────────┬─────────────────────────┘
                ▼
┌─────────────────────────────────────────┐
│  6.  job_battery_profitability.sh       │  compute NPV / EAA over battery
│      (battery_profitability.py)         │  lifetime; compare to TL baseline
└─────────────────────────────────────────┘
```

---

## Step 0 — Snakemake (PyPSA-Eur LOPF solve)

**Shell script:** `shell_scripts/job_snakemake_kupferzell_{simple|full}.sh`

Runs the PyPSA-Eur Snakemake workflow to build and solve the linearised optimal
power flow. The Gurobi barrier solver (method=2, crossover=0) is required to
obtain interior-point dual variables (shadow prices) on line flow constraints.

### Key PyPSA-Eur config requirements (kupferzell_2024_*.yaml)

```yaml
lines:
  s_max_pu: 0.7              # preventive N-1 — do not raise to 1.0

solving:
  options:
    keep_shadowprices: true  # critical — populates n.lines_t.mu_upper / mu_lower
  solver:
    name: gurobi
    options: gurobi-default
  solver_options:
    gurobi-default:
      method: 2              # barrier
      crossover: 0           # keep interior-point solution; duals are well-defined
      BarConvTol: 1.0e-6
      FeasibilityTol: 1.0e-5
      OptimalityTol:  1.0e-5
      threads: 8
      seed: 123
```

### Input
- PyPSA-Eur config YAML: `config/kupferzell_2024_{simple|full}.yaml`

### Output
```
~/PycharmProjects/pypsa-eur/results/
  kupferzell_2024_{simple|full}/
    networks/
      base_s_256_elec_.nc      ← main output: solved network with shadow prices
```

### Verification
```bash
python3 -c "import pypsa; n=pypsa.Network('...base_s_256_elec_.nc'); \
            print('mu_upper rows:', len(n.lines_t.mu_upper))"
```
If `mu_upper` is empty, re-check `keep_shadowprices` — no downstream step can
work without it.

---

## Step 1 — PyPSA postprocessing / validation

**Shell script:** `shell_scripts/job_postprocessing_pypsa.sh`  
**Python script:** `run_validation_pypsa.py`  
**Mode toggle:** `MODE="pypsa-validation"` (default)

Validates the solved network against observed 2025 generation and load data,
and produces an annotated zoomed network map for the Kupferzell study area.

### Toggles (edit before submitting)
| Variable | Values | Description |
|---|---|---|
| `SCENARIO` | `kupferzell_simple` / `kupferzell_full` | which solved network to load |
| `MODE` | `pypsa-validation` / `all` / `congestion` | what to run |
| `TARGET_AREA` | `custom_lines` | line scope for the zoomed map |
| `CUSTOM_LINES` | comma-separated IDs | lines highlighted on the map |
| `VALIDATION_SOURCE` | `both` / `eurostat` / `entsoe` | reference data source |

### Input
- `base_s_256_elec_.nc` (auto-derived from SCENARIO)
- `resources/kupferzell_2024_{simple|full}/powerplants_s_256.csv`

### Output — `results/{scenario}/1_pypsa_validation/`
| File | Description |
|---|---|
| Installed-capacity comparison charts | GridBooster vs. SMARD / ENTSO-E |
| Generation-mix and load-shedding figures | sanity checks on model calibration |
| `figure_kupferzell_zoomed_network_map.png` | annotated map; custom lines highlighted in blue |

---

## Step 2 — Congestion occurrence

**Shell script:** `shell_scripts/job_congestion_occurrence_pypsa.sh`  
**Python script:** `congestion_occurence_pypsa.py`

Extracts hourly congestion information from the solved network using the dual
shadow prices on line flow constraints. Two sub-steps run in sequence.

### Toggles (edit before submitting)
| Variable | Default | Description |
|---|---|---|
| `SCENARIO` | `kupferzell_full` | which network to analyse |
| `CONGESTION_METHOD` | `dual` | detection method (`dual` / `loading` / `n_minus_1` / `redispatch_trigger`) |
| `TARGET_AREA` | `custom_lines` | line scope (`kupferzell_node` / `kupferzell_corridor` / `custom_lines` / `all`) |
| `CUSTOM_LINES` | 17 line IDs | comma-separated; overrides `TARGET_AREA` when set |
| `THRESHOLD` | `0.98` | loading threshold (loading/n-1 methods; reference CSV only in dual mode) |
| `MINIMUM_VOLTAGE` | `220` | minimum line voltage [kV] |

### Input
- `base_s_256_elec_.nc` (auto-derived from SCENARIO)

### Sub-step 1 — shadow-price extraction (`congestion_occurence_pypsa.py`)

Output dir: `results/{scenario}/2_congestion_occurrence/`

| File | Description |
|---|---|
| `congestion_corridor_dual_2025_mu_upper.csv` | **main output**: hourly shadow prices [EUR/MWh], rows = hours, cols = corridor lines |
| `congestion_corridor_dual_2025_mu_lower.csv` | lower-bound shadow prices |
| `congestion_custom_lines_dual_2025_by_line.csv` | per-line congestion statistics |
| `congestion_custom_lines_dual_2025_monthly.csv` | monthly aggregation |
| `congestion_custom_lines_dual_2025_hourly_flags.csv` | binary congestion flag per line-hour |
| `congestion_custom_lines_dual_2025_congestion_rent_eur_per_line.csv` | annual congestion rent per line |
| `kupferzell_line_proximity_hourly_2025.csv` | hourly line-loading for Kupferzell proximity lines |
| `figure_..._occurrence_per_line.png` | bar chart of congested hours per line |
| `figure_..._monthly.png` | monthly congestion occurrence bar chart |
| `figure_..._congestion_occurrence_map.png` | geographic map of congestion incidence |
| `figure_..._kupferzell_zoomed_network_map.png` | zoomed annotated network map |
| `figure_..._average_load_map.png` | average bus load map |
| `figure_..._average_line_loading_map.png` | average line loading map |

A dual-tolerance sensitivity table is printed to the log (DUAL_TOL = 0.1 EUR/MWh).

### Sub-step 2 — post-processing supplement (inline Python)

Reads `mu_upper` to derive auxiliary CSVs consumed by the alleviation step.

| File | Description |
|---|---|
| `corridor_s_nom_2025.csv` | rated capacity [MW] per corridor line |
| `corridor_f_base_abs_mw_2025.csv` | hourly absolute power flow [MW] per corridor line |
| `corridor_congestion_shadow_long_2025.csv` | long-format congested line-hours (one row per line-hour with |μ| > 0.1) |
| `corridor_congestion_shadow_wide_2025.csv` | wide-format hourly μ (zero for non-binding hours) |
| `corridor_congestion_concurrency_2025.csv` | distribution of simultaneous congested line counts |

---

## Step 3 — Congestion alleviation

**Shell script:** `shell_scripts/job_congestion_alleviation.sh`  
**Python scripts:** `congestion_cost_alleviation.py`, `research_workflow.py` (for LOPF re-solves)

Estimates how much congestion the 250 MW GridBooster battery would alleviate.
Submit once per `ALLEVIATION_METHOD`. A merge step runs automatically at the end
of each submission and accumulates results across methods.

### Toggles (edit before submitting)
| Variable | Default | Description |
|---|---|---|
| `SCENARIO` | `kupferzell_full` | must match occurrence job |
| `CONGESTION_METHOD` | `dual` | must match occurrence job |
| `ALLEVIATION_METHOD` | `dynamic_one_line` | `flat_one_line` / `dynamic_one_line` / `dynamic_multiple_lines` |
| `TARGET_AREA` | `custom_lines` | must match occurrence job |
| `CUSTOM_LINES` | 17 line IDs | must match occurrence job |
| `BATTERY_MW` | `250` | GridBooster rated power [MW] |
| `ALPHA` | `1.0` | fraction of battery MW available for congestion relief |
| `COST_YEAR` | `2025` | redispatch cost reference year |
| `HARD_RERUN` | `true` | when `true`, re-solves boost LOPFs even if cached CSVs exist |

### Input
| File | Source step |
|---|---|
| `congestion_corridor_dual_2025_mu_upper.csv` | Step 2 |
| `corridor_s_nom_2025.csv` | Step 2 |
| `corridor_f_base_abs_mw_2025.csv` | Step 2 |
| `base_s_256_elec_.nc` | Step 0 |

### Method A — flat_one_line (upper-frequency bound)

No LOPF re-solve. Assumes the battery deploys the full α × P_bat MW in every
congested hour of the single most-congested corridor line.

Output dir: `results/{scenario}/3_congestion_alleviation/flat_one_line/`

| File | Description |
|---|---|
| `alleviation_hourly_flat_one_line_battery250mw_alpha1.00.csv` | hourly avoided volume and relief EUR |
| `alleviation_kpi_flat_one_line_battery250mw_alpha1.00.csv` | annual KPIs |

### Method B — dynamic_one_line (lower bound)

One LOPF re-solve via `research_workflow.py --mode solve`. The battery is
permanently committed to the auto-selected target line (the one with maximum
MWh-relief potential based on μ and f_base). Avoided volume = Δf on congested hours.

Boost solve cache: `results/{scenario}/0_boost_solves/`  
Key intermediate: `line_flow_abs_mw_2025_boost_mw250_a1.00_line{TARGET_LINE}.csv`

Output dir: `results/{scenario}/3_congestion_alleviation/dynamic_one_line/`

| File | Description |
|---|---|
| `alleviation_hourly_dynamic_one_line_battery250mw_alpha1.00.csv` | hourly Δf and relief EUR |
| `alleviation_kpi_dynamic_one_line_battery250mw_alpha1.00.csv` | annual KPIs |

### Method C — dynamic_multiple_lines (upper bound)

One LOPF re-solve per congested corridor line. The best Δf across all re-solved
lines is assigned to each congested hour. Runtime scales linearly with the number
of congested lines.

Boost solve cache: `results/{scenario}/0_boost_solves/` (one CSV per line)  
Manifest: `boost_manifest_optimal.json`

Output dir: `results/{scenario}/3_congestion_alleviation/dynamic_multiple_lines/`

| File | Description |
|---|---|
| `alleviation_hourly_dynamic_multiple_lines_battery250mw_alpha1.00.csv` | hourly optimal Δf and relief EUR |
| `alleviation_kpi_dynamic_multiple_lines_battery250mw_alpha1.00.csv` | annual KPIs |

### Merge step (runs automatically at end of each submission)

Accumulates results across all three methods into a single hourly CSV.

Output: `results/{scenario}/3_congestion_alleviation/alleviation_revenues_merged_2025.csv`

This file is the primary input to Steps 4 and 5.

---

## Step 4 — Merchant revenues

**Shell script:** `shell_scripts/job_estimate_merchant_revenues.sh`  
**Python script:** `merchant_revenues.py`  
**Solver:** scipy HiGHS (no Gurobi licence required)

Solves 365 independent daily 24-hour LPs to estimate day-ahead spot-market
revenue under perfect foresight. Two modes:

1. **unconstrained** — battery fully free to optimise revenue; daily cyclic SoC = E_nom.
2. **tso_constrained** — TSO congestion hours (from the merged alleviation CSV) lock
   SoC = E_nom and disable charge/discharge for that hour. Run once per alleviation method.

### Input
| File | Description |
|---|---|
| `data/germany_hourly_spot_prices.csv` | Day-ahead spot prices (column `Time_CET` + price column) |
| `alleviation_revenues_merged_2025.csv` | Required for `tso_constrained` runs (Step 3) |

### Output — `results/{scenario}/4_merchant_revenues/`

| File | Description |
|---|---|
| `dam_merchant_revenues_unconstrained_2025.csv` | Unconstrained revenue: daily 24h LP results |
| `dam_merchant_revenues_tso_constrained_flat_one_line_2025.csv` | TSO-constrained (flat_one_line) |
| `dam_merchant_revenues_tso_constrained_dynamic_one_line_2025.csv` | TSO-constrained (dynamic_one_line) |
| `dam_merchant_revenues_tso_constrained_dynamic_multiple_lines_2025.csv` | TSO-constrained (dynamic_multiple_lines) |

---

## Step 5 — Full GridBooster allocation

**Shell script:** `shell_scripts/job_full_gridbooster_allocation.sh`  
**Python script:** `gridbooster_allocation_model.py`

Combines the TSO congestion-relief stream (Step 3) with the merchant revenue
stream (Step 4) under a chosen hour-by-hour allocation rule. Submit once per
`(ALLOCATION_METHOD, ALLEVIATION_METHOD)` combination of interest.

### Toggles (edit before submitting)
| Variable | Default | Description |
|---|---|---|
| `SCENARIO` | `kupferzell_full` | scenario |
| `ALLOCATION_METHOD` | `temporal` | `temporal` / `tso_priority` / `optimal_revenue` |
| `ALLEVIATION_METHOD` | `dynamic_multiple_lines` | selects which congestion-relief column to use |
| `TEMPORAL_ALLOCATION` | JSON string | month-to-role assignment (temporal mode only) |

### Allocation methods

| Method | Logic |
|---|---|
| `temporal` | Months pre-assigned to TSO or merchant (default: Nov–Mar TSO, Apr–Oct merchant). Uses unconstrained merchant CSV. |
| `tso_priority` | Every hour where congestion is binding → TSO; remaining hours → TSO-constrained merchant. |
| `optimal_revenue` | Daily comparison: whichever role (TSO or merchant) yields higher daily revenue is chosen. Requires both unconstrained and TSO-constrained merchant CSVs. |

### Input
| File | Source step |
|---|---|
| `alleviation_revenues_merged_2025.csv` | Step 3 |
| `dam_merchant_revenues_unconstrained_2025.csv` | Step 4 |
| `dam_merchant_revenues_tso_constrained_{method}_2025.csv` | Step 4 |

### Output — `results/{scenario}/5_final_allocation/`

| File | Description |
|---|---|
| `allocation_{allocation_method}_{method_tag}_2025.csv` | hourly allocation: role, revenue, congestion relief |
| `allocation_{allocation_method}_{method_tag}_2025_kpi.csv` | annual KPIs: total TSO revenue, merchant revenue, hours per role |

---

## Step 6 — NPV calculation

**Shell script:** `shell_scripts/job_battery_profitability.sh`  
**Python script:** `battery_profitability.py`

Reads the allocation KPIs from Step 5 and computes the net present value (NPV)
and equivalent annual annuity (EAA) over the battery lifetime. Also benchmarks
against a conventional transmission-line expansion baseline.

### Toggles (edit before submitting)
| Variable | Default | Description |
|---|---|---|
| `SCENARIO` | `kupferzell_full` | must match allocation job |
| `ALLOCATION_METHOD` | `optimal_revenue` | must match allocation job |
| `ALLEVIATION_METHOD` | `dynamic_multiple_lines` | selects the revenue stream |
| `SIM_YEAR` | `2025` | simulation year |

### Input
- `results/{scenario}/5_final_allocation/allocation_{method}_{tag}_2025.csv` (Step 5)

### Output — `results/{scenario}/6_npv_calculation/`

| File | Description |
|---|---|
| `npv_results_{allocation_method}_{merchant_tag}_{year}.csv` | NPV, EAA, CAPEX, annual cash flow |
| `npv_comparison_{allocation_method}_{merchant_tag}_{year}.png` | bar chart: GridBooster NPV vs. TL baseline |
| `alpha_assessment_{tag}.csv` | sensitivity across α (battery MW fraction) values |

---

## Results directory layout

```
results/
  {scenario}/                          kupferzell_simple | kupferzell_full
    0_boost_solves/                    cached LOPF re-solve flows (Step 3)
    1_pypsa_validation/                Step 1 validation outputs
    2_congestion_occurrence/           Step 2 shadow-price CSVs and figures
    3_congestion_alleviation/
      flat_one_line/                   Method A outputs
      dynamic_one_line/                Method B outputs
      dynamic_multiple_lines/          Method C outputs
      alleviation_revenues_merged_2025.csv
    4_merchant_revenues/               Step 4 day-ahead LP outputs
    5_final_allocation/                Step 5 hourly allocation + KPIs
    6_npv_calculation/                 Step 6 NPV results and figures
```

---

## Submission order

```bash
# 0. Build and solve base network (PyPSA-Eur)
bsub < shell_scripts/job_snakemake_kupferzell_full.sh

# 1. Validate (wait for step 0)
bsub < shell_scripts/job_postprocessing_pypsa.sh

# 2. Extract congestion shadow prices (wait for step 0)
bsub < shell_scripts/job_congestion_occurrence_pypsa.sh

# 3. Congestion alleviation — run all three methods (wait for step 2)
# Edit ALLEVIATION_METHOD before each submission
bsub < shell_scripts/job_congestion_alleviation.sh   # flat_one_line
bsub < shell_scripts/job_congestion_alleviation.sh   # dynamic_one_line
bsub < shell_scripts/job_congestion_alleviation.sh   # dynamic_multiple_lines

# 4. Merchant revenues (wait for step 3 merge)
bsub < shell_scripts/job_estimate_merchant_revenues.sh

# 5. Full allocation — one run per (allocation_method × alleviation_method) (wait for steps 3 + 4)
bsub < shell_scripts/job_full_gridbooster_allocation.sh

# 6. NPV (wait for step 5)
bsub < shell_scripts/job_battery_profitability.sh
```

---

## Known issues / notes

- `job_estimate_merchant_revenues.sh` references `4_merchant_revenues.py` but
  the actual script is `merchant_revenues.py`. Update the `MERCHANT_SCRIPT`
  variable in the shell script if the job fails with a file-not-found error.
- `research_workflow.py` is still used by `job_congestion_alleviation.sh` for
  the `--mode solve` LOPF re-solve step (Methods B and C). Do not archive it.
- Steps 1 and 2 can run in parallel once step 0 is complete.
- Steps 3 (three submissions) can run in parallel once step 2 is complete.
