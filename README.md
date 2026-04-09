# Kupferzell GridBooster — Transmission Congestion Simulation

**Purpose**: Quantify hourly congestion on Kupferzell-area 380 kV lines  
using PyPSA-EUR market dispatch (LOPF), calibrated to SMARD 2024 actuals.

---

## Quick Start

```bash
# 1. Clone / create environment
conda create -n kupferzell python=3.11
conda activate kupferzell

# 2. Install dependencies
pip install pypsa>=0.28 pandas numpy matplotlib requests scipy highspy
# highspy is the open-source HiGHS LP solver (required for Step 3)

# 3. Test run (4 weeks, ~30 min total)
python run_pypsa_pipeline.py --test

# 4. Full year run (~8–12 hours total)
python run_pypsa_pipeline.py
```

---

## Pipeline Steps

| Step | Script | Runtime | Output |
|------|--------|---------|--------|
| 1 | `01_download_smard.py` | ~20 min | `data/smard_2024.csv` |
| 2 | `02_build_network.py` | ~10 min | `data/networks/elec_s_128_prepared.nc` |
| 3 | `03_run_lopf.py` | ~6 hrs | `outputs/lopf_results.nc`, `outputs/line_loading_hourly.csv` |
| 4 | `04_congestion_analysis.py` | ~5 min | `outputs/*.csv`, `outputs/*.png`, `outputs/*.tex` |

Run individual steps with `--step N` flag:
```bash
python run_pypsa_pipeline.py --step 3   # resume from Step 3
python run_pypsa_pipeline.py --only 4   # re-run only analysis
```

---

## Methodological Notes

### Network choice: PyPSA-EUR (128-bus)
Cross-border flows from Nordic/North Sea wind are the primary physical  
driver of the German N-S corridor overloads that motivate the Kupferzell  
GridBooster. PyPSA-DE cannot capture these; PyPSA-EUR is required.

**Reference**: Frysztacki et al. (2021), *Energy* 246:123234  
→ validates PyPSA-EUR DC flows against ENTSO-E observations on German corridor.

### Congestion criterion
Line `l` is congested in hour `t` iff:

    |p0_{l,t}| / s_nom_l ≥ 0.98

The 2% tolerance handles LP numerical precision. The `s_nom` values in  
PyPSA-EUR encode N-1 secure thermal ratings (i.e., they already incorporate  
the N-1 security factor used by TransnetBW in operation).

### Spatial resolution caveat
At 128-bus clustering, Kupferzell is aggregated into the  
Heilbronn/Hohenlohe/northeastern BW cluster. The corridor lines  
Kupferzell–Großgartach and Kupferzell–Stalldorf may be represented  
as a single aggregated line in the clustered network.

**Mitigation**: Run at 256 or 512 buses for publication-quality spatial  
resolution (increases Step 3 runtime to ~24–48 hours).  
Set `NETWORK_CLUSTERING = 256` in `00_config.py`.

### LOPF vs full AC OPF
DC linearisation introduces ~5–15% error on individual line flows  
relative to full AC OPF. For congestion frequency (a binary threshold),  
this error affects the precision of the congestion count but not the  
qualitative finding. Standard in the literature.

### N-1 security
Full SCLOPF (Security-Constrained LOPF) is computationally prohibitive  
for 8784-hour problems. We implement a post-hoc PTDF-based N-1 check  
in Step 4 for the corridor lines (see `n1_contingency_check()`).

---

## Key Output Files

| File | Description |
|------|-------------|
| `outputs/congestion_summary.csv` | Per-line annual congestion stats |
| `outputs/congestion_hourly.csv` | Binary congestion indicator (8784 × n_lines) |
| `outputs/congestion_monthly.csv` | Monthly congestion hours per line |
| `outputs/table_congestion_summary.tex` | LaTeX Table 1 for paper |
| `outputs/table_monthly_congestion.tex` | LaTeX Table 2 for paper |
| `outputs/figure_loading_dist.png` | Line loading distribution |
| `outputs/figure_seasonal_heatmap.png` | Month × hour congestion heatmap |
| `outputs/figure_monthly_congestion.png` | Monthly bar chart |

---

## Flags for Submission (Outstanding Verifications)

- [ ] **BNetzA capacity figures**: Verify 2024 installed capacity by carrier  
      against BNetzA Monitoringbericht 2024 (Step 2, `fix_generator_capacities_to_2024`)
- [ ] **ETS CO2 price**: Verify 60 EUR/tCO2 assumption for 2024 against EEX data  
      (Step 3, `set_marginal_costs`)
- [ ] **Network file URL**: Confirm Zenodo DOI for PyPSA-EUR 128-bus network  
      matches the infrastructure year stated in paper Section 2
- [ ] **Spatial resolution**: Confirm 128-bus is sufficient to resolve  
      Kupferzell corridor distinctly, or upgrade to 256-bus
- [ ] **SMARD NaN fraction**: Confirm < 1% missing data in downloaded series

---

## Hardware Requirements

| Configuration | RAM | Step 3 Runtime |
|---------------|-----|----------------|
| 128-bus, full year | 8 GB | ~6–8 hours |
| 256-bus, full year | 24 GB | ~24–36 hours |
| 128-bus, test (4 wk) | 4 GB | ~30 min |

Recommended: 16-core workstation, 32 GB RAM, SSD storage.

---

## Citation

If using this pipeline in your paper, cite:

```bibtex
@software{kupferzell_congestion_sim,
  title  = {Kupferzell GridBooster Congestion Simulation Pipeline},
  author = {[Authors]},
  year   = {2025},
  note   = {Based on PyPSA-EUR; SMARD data from Bundesnetzagentur}
}

@article{horsch2018pypsa,
  title   = {{PyPSA}: Python for Power System Analysis},
  author  = {Horsch, Jonas and Hofmann, Fabian and Schlachtberger, David and Brown, Tom},
  journal = {Journal of Open Research Software},
  volume  = {6},
  year    = {2018},
  doi     = {10.5334/jors.188}
}

@article{frysztacki2021strong,
  title   = {The strong effect of network resolution on electricity system models},
  author  = {Frysztacki, Martha Maria and Hörsch, Jonas and Hagenmeyer, Veit and Brown, Tom},
  journal = {Energy},
  volume  = {246},
  pages   = {123234},
  year    = {2021},
  doi     = {10.1016/j.energy.2022.123234}
}
```
