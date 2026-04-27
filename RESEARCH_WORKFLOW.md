# Research workflow — picks up where `SETUP_GUIDE.md` ends

`SETUP_GUIDE.md` ends at the point the HPC environment is working, the
PyPSA-Eur fork is cloned and patched, custom powerplants and the 2025 grid
data are in place, and the snakemake rules can produce
`results/<run>/networks/base_s_256_elec_.nc` for both `kupferzell_2024_simple`
and `kupferzell_2024_full`. This file takes over from there and describes the
paper pipeline: four LSF jobs, submitted in a fixed order, producing the
congestion-rent-difference savings that go into the results section.

`research_workflow.py` is obsolete. Nothing in the pipeline imports it, and
its `orchestrator` mode in `job_postprocessing_pypsa.sh` has been replaced by
calling the two scripts directly. Archive the file and drop the orchestrator
branch.

---

## 1. Four stages, four LSF jobs

```
┌──────────────────────────────────────┐
│ 1.  job_snakemake_kupferzell_*.sh    │   generate base network (snakemake
│     (or job_snakemake_default.sh)    │   solves it via Gurobi + keep_shadowprices)
└──────────────┬───────────────────────┘
               ▼
┌──────────────────────────────────────┐
│ 2.  job_postprocessing_pypsa.sh      │   validate structural integrity,
│                                      │   installed capacity, load shedding
└──────────────┬───────────────────────┘
               ▼
┌──────────────────────────────────────┐
│ 3.  job_congestion_occurrence_pypsa  │   (a) uprate + re-solve  → boost .nc
│     .sh                              │   (b) dual congestion count on base
│                                      │   (c) dual congestion count on boost
└──────────────┬───────────────────────┘
               ▼
┌──────────────────────────────────────┐
│ 4.  job_congestion_cost_alleviation  │   rent_diff = Σ_t|μ_base|·s_nom
│     .sh                              │               − Σ_t|μ_boost|·s_nom
└──────────────────────────────────────┘
```

Stage 1 is a vanilla snakemake run. Stage 2 is the same two scripts the old
`orchestrator` mode used. Stages 3 and 4 are where the shadow-price
methodology lives.

---

## 2. Prerequisites in the PyPSA-Eur fork

These changes live in `~/PycharmProjects/pypsa-eur/config/` (and are tracked
on the `hpc` branch of the pypsa-eur fork). They are *not* edited by this
project's jobs; they must be set up once in the config YAMLs that
`job_snakemake_*.sh` passes to snakemake.

In `config/kupferzell_2024_simple.yaml` **and**
`config/kupferzell_2024_full.yaml`:

```yaml
lines:
  s_max_pu: 0.7              # preventive N-1 — do NOT raise to 1.0

solving:
  solver:
    name: gurobi
    options: gurobi-default  # or gurobi-barrier, see below
  options:
    gurobi-default:
      threads: 8
      method: 2              # barrier
      crossover: 0           # interior solution is fine for duals
      BarConvTol: 1.0e-6
      FeasibilityTol: 1.0e-5
      OptimalityTol:  1.0e-5
      seed: 123
  keep_shadowprices: true    # critical — populates n.lines_t.mu_upper
```

Two notes:

1. `keep_shadowprices: true` lives at `solving.keep_shadowprices` in the
   PyPSA-Eur config schema (see pypsa-eur `scripts/solve_network.py`,
   function `solve_network`). If the schema on this fork is older and this
   key is ignored, patch `solve_network.py` so the call site passes
   `keep_shadowprices=True` to `n.optimize(...)`.
2. Do *not* change `s_max_pu` here. The shadow-price detector is invariant
   to `s_max_pu`; leaving it at 0.7 makes the baseline the methodologically
   honest preventive-N-1 LP.

---

## 3. Stage 1 — snakemake

Nothing in the `Battery_Congestion_Alleviation` repo changes between runs;
only the config YAML varies. Pick one:

```bash
bsub < shell_scripts/job_snakemake_default.sh
# -- produces results/networks/base_s_256_elec_.nc (Europe-wide default)

bsub < shell_scripts/job_snakemake_kupferzell_simple.sh
# -- kupferzell_2024_simple, short time horizon, used for pypsa-validation

bsub < shell_scripts/job_snakemake_kupferzell_full.sh
# -- kupferzell_2024_full, full 8760-hour year, the publication run
```

Success means
`~/PycharmProjects/pypsa-eur/results/<run>/networks/base_s_256_elec_.nc`
exists and contains non-empty `lines_t.mu_upper` (verify once with
`python3 -c "import pypsa; n=pypsa.Network('...'); print(len(n.lines_t.mu_upper))"`).

If `mu_upper` is empty, re-check `keep_shadowprices` — no downstream stage
can work without it.

---

## 4. Stage 2 — postprocessing

```bash
bsub < shell_scripts/job_postprocessing_pypsa.sh all \
     /zhome/26/e/209460/PycharmProjects/pypsa-eur/results/kupferzell_2024_simple/networks/base_s_256_elec_.nc \
     /zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation/results
```

Positional args: `MODE`, `NETWORK_PATH`, `RESULTS_ROOT`, `POWERPLANTS_CSV` (optional),
`THRESHOLD` (optional), `CONGESTION_OUTPUT_ROOT` (optional). Modes are:

- `all`            — run pypsa-validation **and** a quick-look loading-based
                     congestion scan (legacy, kept for sanity checks)
- `pypsa-validation` — validation only (installed capacity vs SMARD, load
                     shedding summaries, generation mix)
- `congestion`     — loading-based occurrence scan only

The `orchestrator` mode is removed. The shadow-price congestion is produced
in stage 3, not here.

---

## 5. Stage 3 — congestion occurrence (with booster uprate)

This is the new critical-path stage. `job_congestion_occurrence_pypsa.sh`
does three things in one submission:

1. **Uprate + re-solve.** Calls
   `python3 congestion_occurence_pypsa.py --mode uprate_and_solve` with a
   pre-solve netCDF input, monitored-area selector, battery MW and α.
   Internally the script:
   - selects the monitored lines via `select_target_lines(...,
     target_area="corridor")` (or whichever target area is requested);
   - sets `n.lines.loc[monitored, "s_max_pu"] = min(1.0,
     (0.7 · s_nom + α·P_bat / N_monitored) / s_nom)`;
   - solves with `n.optimize(solver_name="gurobi",
     solver_options={"Method": 2, "Crossover": 0, ...},
     keep_shadowprices=True)`;
   - writes `network_<year>_boost_mw<mw>_a<alpha>.nc` next to the base
     solved netCDF.

2. **Dual count on base.** `--mode count --method dual --network <base.nc>`
   emits `{prefix}_mu_upper.csv`, `{prefix}_mu_lower.csv`,
   `{prefix}_congestion_rent_eur_per_line.csv` under
   `results/<scenario>/congestion_occurrence/base/`.

3. **Dual count on boost.** Same as (2) but `--network <boost.nc>` and the
   outputs land under `.../congestion_occurrence/boost/`.

Submit:

```bash
bsub < shell_scripts/job_congestion_occurrence_pypsa.sh \
     /zhome/26/e/209460/PycharmProjects/pypsa-eur/results/kupferzell_2024_simple/networks/base_s_256_elec_.nc \
     /zhome/26/e/209460/PycharmProjects/Battery_Congestion_Alleviation/results \
     0.90    \    # loading threshold (diagnostic only; unused by dual)
     220     \    # minimum voltage [kV]
     ""      \    # requested lines (empty = use --target-area)
     dual    \    # method  {dual | loading | n_minus_1 | redispatch_trigger}
     corridor\    # target-area  {corridor | kupferzell | all}
     250     \    # battery_mw  (0 = skip the boost solve, run base only)
     1.0          # alpha
```

`method=dual` is the paper primary statistic. `redispatch_trigger` is the
appendix robustness check. `n_minus_1` is available for per-contingency
attribution but it is the expensive option — do not run it in the default
pipeline.

The `--target-area corridor` default captures the BW + N-S corridor feeders.
Switch to `kupferzell` for the narrow radius sanity-check, or `all` for a
full-network sweep.

---

## 6. Stage 4 — congestion cost alleviation

The savings are computed directly from the two `mu_upper.csv` files
produced by stage 3. No solve here — it is a CSV diff.

```bash
bsub < shell_scripts/job_congestion_cost_alleviation.sh \
     ""                    \   # input csv (unused in dual mode)
     ""                    \   # output dir (auto-resolved to sibling alleviation/)
     250                   \   # battery_mw
     1.0                   \   # alpha
     0.98                  \   # threshold (unused in dual mode)
     unit_cost             \   # cost_mode (unused in dual mode)
     no                    \   # run sensitivity? kept for the overload path
     mean                  \   # redispatch cost year (overload path only)
     dual                  \   # --source: dual | overload
     "<base mu_upper.csv>" \   # --mu-base-csv
     "<boost mu_upper.csv>"    # --mu-boost-csv
```

Output: `cost_alleviation_<...>.csv` with per-line `rent_base_eur`,
`rent_boost_eur`, `saving_eur`. Paper headline number is
`saving_eur.sum()` restricted to the target-area mask.

The legacy overload path remains under `--source overload` for the appendix
robustness check (uses the `kupferzell_line_proximity_hourly_*.csv` and the
`S_eff = S_nom + α·P_bat` Taylor approximation). Treat divergence between
the two larger than ~2× as a signal that one of the two is mis-specified.

---

## 7. Sensitivity grid (optional)

The `(battery_mw × alpha)` sensitivity grid stays in
`congestion_cost_alleviation.py --sensitivity`. It still uses the overload
path by design — running the dual path over a grid means re-solving the
LOPF per grid point, which is not what the sensitivity number is for in
the paper. The overload grid is the "how does the result scale with
sizing assumptions" appendix plot; the dual number is the point estimate.

---

## 8. File inventory after migration

Kept:
- `congestion_criteria.py`              (core primitives, dual promoted to default)
- `congestion_occurence_pypsa.py`       (counts congestion; now has `--mode uprate_and_solve`)
- `congestion_cost_alleviation.py`      (rent-diff path + overload fallback)
- `run_validation_pypsa.py`             (pypsa-validation, unchanged)
- `plotting.py`                         (unchanged)
- `plot_congestion_maps.py`             (publication maps, unchanged)
- `data_validation_pypsa.py`            (unchanged)
- `add_analysis_variables.py`           (unchanged)

Archived:
- `research_workflow.py`                 → move to `archive/`

Removed:
- `shell_scripts/job_congestion_pipeline.sh`

Updated:
- `shell_scripts/job_snakemake_default.sh`
- `shell_scripts/job_snakemake_kupferzell_simple.sh`
- `shell_scripts/job_snakemake_kupferzell_full.sh`
- `shell_scripts/job_postprocessing_pypsa.sh`
- `shell_scripts/job_congestion_occurrence_pypsa.sh`
- `shell_scripts/job_congestion_cost_alleviation.sh`

---

## 9. End-to-end smoke test (one week, simple config)

```bash
cd ~/PycharmProjects/Battery_Congestion_Alleviation

# 1. Build + solve baseline via snakemake
bsub < shell_scripts/job_snakemake_kupferzell_simple.sh

# wait until done, then:

# 2. Validate
bsub < shell_scripts/job_postprocessing_pypsa.sh all

# 3. Dual congestion count + boost solve
bsub < shell_scripts/job_congestion_occurrence_pypsa.sh

# 4. Rent-diff savings
bsub < shell_scripts/job_congestion_cost_alleviation.sh "" "" 250 1.0 0.98 unit_cost no mean dual \
     results/kupferzell_simple/congestion_occurrence/base/*_mu_upper.csv \
     results/kupferzell_simple/congestion_occurrence/boost/*_mu_upper.csv
```

Expected outcome on the 168-hour January 2025 simple-network slice:

- `boost_mu_upper.csv` is strictly less (column sum) than `base_mu_upper.csv`
  on the Kupferzell-Großgartach and Kupferzell-Goldshöfe segments.
- `saving_eur.sum()` on the corridor is positive and of the same order as
  the `--source overload` number (they should agree to ~2×).
- No warnings about `mu_upper empty` — if there are, go back to §2.