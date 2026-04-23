"""
Scale network demand from 2013 levels to 2025 levels.

Run AFTER Snakemake produces base_s_256_elec_.nc.
Produces: base_s_256_elec_demand2025.nc

Usage:
  python3 scripts/scale_demand_2025.py \
      --input  ~/PycharmProjects/pypsa-eur/results/kupferzell_2024/networks/base_s_256_elec_.nc \
      --output ~/PycharmProjects/pypsa-eur/results/kupferzell_2024/networks/base_s_256_elec_demand2025.nc
"""

import pypsa
import pandas as pd
import argparse
from pathlib import Path

# Country-level demand scaling factors (2025 / 2013 annual totals)
# Update these from Step D1 once you have exact ENTSO-E figures
DEMAND_SCALE = {
    "DE": 465.5/561,   # ~595 TWh (2013) → ~475 TWh (2025) https://www.energy-charts.info/charts/energy/chart.htm?l=en&c=DE&legendItems=2x2vv7v&interval=year&source=total&year=2013
    "DK": 36.8/32.8,   # ~34 TWh (2013)  → ~35 TWh (2025) https://www.energy-charts.info/charts/energy/chart.htm?l=en&c=DK&legendItems=1wa&interval=year&source=total&year=2015
}


def scale_loads(n: pypsa.Network, scale_factors: dict) -> pypsa.Network:
    """
    Scale network loads by country.
    n.loads_t.p_set contains hourly load profiles in MW.
    """
    loads_by_country = n.loads.bus.map(n.buses.country)

    for country, factor in scale_factors.items():
        country_loads = n.loads[loads_by_country == country].index
        if country_loads.empty:
            print(f"  Warning: no loads found for country {country}")
            continue

        original_annual = n.loads_t.p_set[country_loads].sum().sum() / 1e6  # TWh
        n.loads_t.p_set[country_loads] *= factor
        scaled_annual  = n.loads_t.p_set[country_loads].sum().sum() / 1e6

        print(f"  {country}: {original_annual:.1f} TWh → {scaled_annual:.1f} TWh "
              f"(×{factor:.3f})")

    return n


DEFAULT_INPUT  = Path.home() / "PycharmProjects/pypsa-eur/results/kupferzell_2024/networks/base_s_256_elec_.nc"
DEFAULT_OUTPUT = Path.home() / "PycharmProjects/pypsa-eur/results/kupferzell_2024/networks/base_s_256_elec__dem2025.nc"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",  default=str(DEFAULT_INPUT),
                        help="Path to base_s_256_elec_.nc (from Snakemake solve_network)")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help="Output path for scaled network (typically base_s_256_elec__dem2025.nc)")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    # Validate input exists
    if not input_path.exists():
        raise FileNotFoundError(
            f"Input network not found: {input_path}\n"
            f"Expected from: snakemake ... -- results/kupferzell_2024/networks/base_s_256_elec_.nc\n"
            f"Run PyPSA-Eur Snakemake first before scaling."
        )

    print(f"[1] Loading network: {input_path}")
    n = pypsa.Network(str(input_path))
    print(f"    {len(n.buses)} buses | {len(n.generators)} generators | "
          f"{len(n.loads)} loads | {len(n.snapshots)} snapshots")

    print(f"\n[2] Scaling loads by country to 2025 annual levels:")
    n = scale_loads(n, DEMAND_SCALE)

    # Create output directory
    output_path.parent.mkdir(parents=True, exist_ok=True)
    n.export_to_netcdf(str(output_path))
    print(f"\n[3] Saved scaled network to:\n    {output_path}")

    # Verify output was written
    if output_path.exists():
        file_size_mb = output_path.stat().st_size / (1024**2)
        print(f"    File size: {file_size_mb:.1f} MB ✓")
    else:
        raise RuntimeError(f"Output file was not created: {output_path}")

if __name__ == "__main__":
    main()
