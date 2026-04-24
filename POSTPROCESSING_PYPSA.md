# PyPSA Post-Processing Scripts

This folder now contains three focused scripts for validation and congestion analysis:

- `run_validation_pypsa.py`
- `congestion_occurence_pypsa.py`
- `plotting.py`

`research_workflow.py` remains as a compatibility orchestrator.

## Quick run (no arguments)

All three scripts now include defaults, so they can be launched directly:

```bash
python3 run_validation_pypsa.py
python3 congestion_occurence_pypsa.py
python3 research_workflow.py
```

Default behavior targets the solved simple network from `pypsa-eur` and writes outputs to `Battery_Congestion_Alleviation/outputs/postprocess_simple`.

## 1) Validate a solved run

```bash
python3 run_validation_pypsa.py \
  --network /zhome/26/e/209460/PycharmProjects/pypsa-eur/results/kupferzell_2024_simple/networks/base_s_256_elec_.nc \
  --output-dir outputs/postprocess_simple \
  --powerplants-csv /zhome/26/e/209460/PycharmProjects/pypsa-eur/resources/kupferzell_2024_simple/powerplants_s_256.csv
```

Main outputs:
- `installed_capacity_by_country_carrier_2025.csv`
- `generation_estimate_by_country_carrier_2025.csv`
- `load_shedding_hourly_2025.csv`
- `load_shedding_by_country_2025.csv`
- `load_shedding_summary_2025.csv`
- `capacity_validation_country_carrier_2025.csv`
- `capacity_validation_country_totals_2025.csv`
- `model_fidelity_overview_2025.csv`
- `model_validation_summary_2025.csv`

### Validation checks implemented

The validation script now writes two levels of checks:

1. **Structural / time-series checks**
   - snapshots are present
   - line flow time series are present
   - generator, line, and load time-series are aligned with the snapshot index
   - load demand is nonnegative
   - the objective is finite / available in the solved network

2. **Model-fidelity summary checks**
   - total installed capacity (MW)
   - total generation estimate (TWh)
   - total load not served (MWh)
   - number of hours with load shedding
   - load-shedding share of total demand
   - maximum country-level capacity mismatch vs reference
      - maximum country-carrier capacity mismatch vs reference (percentage max calculated only for reference capacities >= 10 MW)
      - maximum absolute country-carrier mismatch in MW

These checks are saved in `model_validation_summary_2025.csv` and `model_fidelity_overview_2025.csv`.

## 2) Run congestion occurrence analysis

```bash
python3 congestion_occurence_pypsa.py \
  --network /zhome/26/e/209460/PycharmProjects/pypsa-eur/results/kupferzell_2024_simple/networks/base_s_256_elec_.nc \
  --output-dir outputs/postprocess_simple \
  --threshold 0.98
```

Main outputs:
- `line_loading_hourly_2025.csv`
- `congestion_hourly_flags_2025.csv`
- `congestion_by_line_2025.csv`
- `congestion_monthly_2025.csv`
- `congestion_by_country_pair_2025.csv`
- `kupferzell_line_proximity_hourly_2025.csv`
- figure PNG files for line congestion and Kupferzell loading

## 3) Use compatibility orchestrator

```bash
python3 research_workflow.py \
  --mode postprocess \
  --solved-network /zhome/26/e/209460/PycharmProjects/pypsa-eur/results/kupferzell_2024_simple/networks/base_s_256_elec_.nc \
  --output-dir outputs/postprocess_simple \
  --powerplants-csv /zhome/26/e/209460/PycharmProjects/pypsa-eur/resources/kupferzell_2024_simple/powerplants_s_256.csv \
  --congestion-threshold 0.98
```

## Notes

- The script name `congestion_occurence_pypsa.py` is intentionally kept as requested.
- `plotting.py` currently provides reusable static plotting helpers used by the congestion script.