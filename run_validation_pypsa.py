"""Run validation utilities for solved PyPSA networks.

Exports model diagnostics used to assess how close the run is to a real-world case:
- total generation estimates
- installed capacities
- load not served (ENS)
- capacity comparison against processed real-world reference data
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

SIM_YEAR = 2025
PROJECT_DIR = Path(__file__).resolve().parent
PYPSA_EUR_DIR = PROJECT_DIR.parent / "pypsa-eur"
DEFAULT_SOLVED_NETWORK = PYPSA_EUR_DIR / "results" / "kupferzell_2024_simple" / "networks" / "base_s_256_elec_.nc"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "outputs" / "postprocess_simple"
DEFAULT_POWERPLANTS_CSV = PYPSA_EUR_DIR / "resources" / "kupferzell_2024_simple" / "powerplants_s_256.csv"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate solved PyPSA run and export diagnostics")
    p.add_argument("--network", type=Path, default=DEFAULT_SOLVED_NETWORK, help="Solved PyPSA network netcdf")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output directory")
    p.add_argument(
        "--powerplants-csv",
        type=Path,
        default=DEFAULT_POWERPLANTS_CSV,
        help="Reference capacities from PyPSA preprocessing",
    )
    return p.parse_args()


def _country_from_bus(series: pd.Series) -> pd.Series:
    return series.astype(str).str[:2]


def installed_capacities(n: pypsa.Network) -> pd.DataFrame:
    """Aggregate installed capacities by country and carrier."""
    g = n.generators[n.generators.carrier != "load"].copy()
    g["country"] = _country_from_bus(g["bus"])
    g["carrier_cmp"] = g["carrier"].replace(
        {
            "solar-hsat": "solar",
            "offwind-ac": "offwind",
            "offwind-dc": "offwind",
            "offwind-float": "offwind",
        }
    )
    g_agg = g.groupby(["country", "carrier_cmp"], as_index=False)["p_nom"].sum()
    g_agg = g_agg.rename(columns={"p_nom": "installed_mw"})

    su = n.storage_units.copy()
    su["country"] = _country_from_bus(su["bus"])
    su["carrier_cmp"] = su["carrier"]
    su_agg = su.groupby(["country", "carrier_cmp"], as_index=False)["p_nom"].sum()
    su_agg = su_agg.rename(columns={"p_nom": "installed_mw"})

    out = pd.concat([g_agg, su_agg], ignore_index=True)
    out = out.groupby(["country", "carrier_cmp"], as_index=False)["installed_mw"].sum()
    out = out.sort_values(["country", "carrier_cmp"]).reset_index(drop=True)
    return out


def generation_totals(n: pypsa.Network) -> pd.DataFrame:
    """Export estimated generation by country and carrier in TWh.

    Uses positive generator dispatch and positive storage discharge.
    """
    g = n.generators[n.generators.carrier != "load"].copy()
    g["country"] = _country_from_bus(g["bus"])
    g["carrier_cmp"] = g["carrier"].replace(
        {
            "solar-hsat": "solar",
            "offwind-ac": "offwind",
            "offwind-dc": "offwind",
            "offwind-float": "offwind",
        }
    )
    gp = n.generators_t.p[g.index].clip(lower=0)
    g_energy = gp.sum(axis=0).rename("mwh").to_frame().join(g[["country", "carrier_cmp"]])
    g_energy = g_energy.groupby(["country", "carrier_cmp"], as_index=False)["mwh"].sum()

    su = n.storage_units.copy()
    su["country"] = _country_from_bus(su["bus"])
    su["carrier_cmp"] = su["carrier"]
    if n.storage_units_t.p_dispatch.empty:
        su_energy = pd.DataFrame(columns=["country", "carrier_cmp", "mwh"])
    else:
        sup = n.storage_units_t.p_dispatch[su.index].clip(lower=0)
        su_energy = sup.sum(axis=0).rename("mwh").to_frame().join(su[["country", "carrier_cmp"]])
        su_energy = su_energy.groupby(["country", "carrier_cmp"], as_index=False)["mwh"].sum()

    out = pd.concat([g_energy, su_energy], ignore_index=True)
    out = out.groupby(["country", "carrier_cmp"], as_index=False)["mwh"].sum()
    out["generation_twh"] = out["mwh"] / 1e6
    out = out.drop(columns=["mwh"]).sort_values(["country", "carrier_cmp"]).reset_index(drop=True)
    return out


def timeseries_alignment_checks(n: pypsa.Network) -> pd.DataFrame:
    """Check whether the main network time series are aligned with snapshots."""
    expected = len(n.snapshots)
    checks: list[tuple[str, str, str]] = []

    for label, ts in [
        ("lines_t.p0", n.lines_t.p0),
        ("generators_t.p", n.generators_t.p),
        ("loads_t.p_set", n.loads_t.p_set),
    ]:
        aligned = (ts.empty or len(ts.index) == expected) and ts.index.equals(n.snapshots)
        checks.append((f"{label}_aligned", "pass" if aligned else "warning", f"shape={ts.shape}"))

    return pd.DataFrame(checks, columns=["check", "status", "detail"])


def load_not_served(n: pypsa.Network) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Compute ENS from load-shedding generators if present."""
    ls = n.generators.index[
        n.generators.carrier.fillna("").str.lower().eq("load")
        | n.generators.index.str.lower().str.contains("load shedding")
    ]

    if len(ls) == 0 or n.generators_t.p.empty:
        hourly = pd.DataFrame(index=n.snapshots, data={"load_shed_mw": 0.0})
        by_country = pd.DataFrame(columns=["country", "load_shed_mwh"])
    else:
        p = n.generators_t.p[ls].clip(lower=0)
        hourly = p.sum(axis=1).to_frame("load_shed_mw")
        meta = n.generators.loc[ls, ["bus"]].copy()
        meta["country"] = _country_from_bus(meta["bus"])
        by_gen = p.sum(axis=0).rename("load_shed_mwh")
        by_country = by_gen.to_frame().join(meta["country"]).groupby("country", as_index=False)["load_shed_mwh"].sum()

    total_load_mwh = float(n.loads_t.p_set.sum().sum()) if not n.loads_t.p_set.empty else np.nan
    total_shed_mwh = float(hourly["load_shed_mw"].sum())
    summary = pd.DataFrame(
        [
            {
                "metric": "total_load_mwh",
                "value": total_load_mwh,
            },
            {
                "metric": "total_load_shed_mwh",
                "value": total_shed_mwh,
            },
            {
                "metric": "load_shed_share_pct",
                "value": (100.0 * total_shed_mwh / total_load_mwh) if total_load_mwh and np.isfinite(total_load_mwh) else np.nan,
            },
            {
                "metric": "hours_with_load_shedding",
                "value": int((hourly["load_shed_mw"] > 1e-6).sum()),
            },
            {
                "metric": "max_hourly_load_shed_mw",
                "value": float(hourly["load_shed_mw"].max()),
            },
        ]
    )
    return hourly, by_country, summary


def _map_reference_carrier(df: pd.DataFrame) -> pd.Series:
    fuel = df["Fueltype"].fillna("").str.lower()
    tech = df["Technology"].fillna("").str.lower()

    c = pd.Series("", index=df.index, dtype=object)
    c[fuel.str.contains("solar")] = "solar"
    c[fuel.str.contains("wind")] = "offwind"
    c[fuel.str.contains("wind") & tech.str.contains("onshore")] = "onwind"
    c[fuel.str.contains("wind") & ~tech.str.contains("offshore") & ~tech.str.contains("onshore")] = "onwind"

    c[fuel.str.contains("hydro")] = "hydro"
    c[fuel.str.contains("hydro") & tech.str.contains("run-?of-?river")] = "ror"
    c[fuel.str.contains("hydro") & tech.str.contains("pumped")] = "PHS"

    c[fuel.str.contains("bio")] = "biomass"
    c[fuel.str.contains("waste")] = "waste"
    c[fuel.str.contains("nuclear|uranium")] = "nuclear"
    c[fuel.str.contains("lignite")] = "lignite"
    c[fuel.str.contains("hard coal|coal")] = "coal"
    c[fuel.str.contains("oil|diesel")] = "oil"
    c[fuel.str.contains("geothermal")] = "geothermal"

    is_gas = fuel.str.contains("gas")
    c[is_gas] = "OCGT"
    c[is_gas & tech.str.contains("ccgt|combined")] = "CCGT"
    c[is_gas & tech.str.contains("ocgt|open")] = "OCGT"
    return c


def capacity_vs_reference(installed: pd.DataFrame, reference_csv: Path) -> pd.DataFrame:
    """Compare model installed capacities against reference powerplants file."""
    ref_raw = pd.read_csv(reference_csv)
    ref_raw["country"] = ref_raw["Country"].astype(str)
    ref_raw["carrier_cmp"] = _map_reference_carrier(ref_raw)
    ref = ref_raw[ref_raw["carrier_cmp"] != ""].copy()
    ref = ref.groupby(["country", "carrier_cmp"], as_index=False)["Capacity"].sum()
    ref = ref.rename(columns={"Capacity": "reference_mw"})

    out = installed.merge(ref, on=["country", "carrier_cmp"], how="outer").fillna(0.0)
    out["delta_mw"] = out["installed_mw"] - out["reference_mw"]
    out["delta_pct_vs_reference"] = out.apply(
        lambda r: (r["delta_mw"] / r["reference_mw"] * 100.0) if r["reference_mw"] > 0 else np.nan,
        axis=1,
    )
    out = out.sort_values(["country", "carrier_cmp"]).reset_index(drop=True)
    return out


def model_fidelity_overview(
    n: pypsa.Network,
    installed: pd.DataFrame,
    generation: pd.DataFrame,
    ens_summary: pd.DataFrame,
    cap_compare: pd.DataFrame,
) -> pd.DataFrame:
    """Summarize the main validation KPIs in a single table."""

    total_generation_twh = float(generation["generation_twh"].sum()) if not generation.empty else np.nan
    total_installed_mw = float(installed["installed_mw"].sum()) if not installed.empty else np.nan
    total_shed_mwh = float(ens_summary.loc[ens_summary.metric.eq("total_load_shed_mwh"), "value"].iloc[0])
    shed_hours = int(ens_summary.loc[ens_summary.metric.eq("hours_with_load_shedding"), "value"].iloc[0])
    shed_share = float(ens_summary.loc[ens_summary.metric.eq("load_shed_share_pct"), "value"].iloc[0])

    country_totals = cap_compare.groupby("country", as_index=False)[["installed_mw", "reference_mw", "delta_mw"]].sum()
    country_totals["delta_pct_vs_reference"] = np.where(
        country_totals["reference_mw"] > 0,
        100.0 * country_totals["delta_mw"] / country_totals["reference_mw"],
        np.nan,
    )
    max_country_gap = float(country_totals["delta_pct_vs_reference"].abs().max()) if not country_totals.empty else np.nan
    carrier_threshold = 10.0
    large_refs = cap_compare[cap_compare["reference_mw"] >= carrier_threshold]
    max_country_carrier_gap = float(large_refs["delta_pct_vs_reference"].abs().max()) if not large_refs.empty else np.nan
    max_country_carrier_gap_mw = float(cap_compare["delta_mw"].abs().max()) if not cap_compare.empty else np.nan

    return pd.DataFrame(
        [
            {"metric": "total_generation_twh", "value": total_generation_twh, "unit": "TWh"},
            {"metric": "total_installed_mw", "value": total_installed_mw, "unit": "MW"},
            {"metric": "total_load_shed_mwh", "value": total_shed_mwh, "unit": "MWh"},
            {"metric": "hours_with_load_shedding", "value": shed_hours, "unit": "hours"},
            {"metric": "load_shed_share_pct", "value": shed_share, "unit": "%"},
            {"metric": "max_country_capacity_gap_pct", "value": max_country_gap, "unit": "%"},
            {
                "metric": "max_country_carrier_capacity_gap_pct_ref_ge_10mw",
                "value": max_country_carrier_gap,
                "unit": "%",
            },
            {
                "metric": "max_country_carrier_capacity_gap_mw",
                "value": max_country_carrier_gap_mw,
                "unit": "MW",
            },
            {
                "metric": "objective_finite",
                "value": bool(np.isfinite(getattr(n, "objective", np.nan))),
                "unit": "bool",
            },
        ]
    )


def validation_summary(n: pypsa.Network, ens_summary: pd.DataFrame) -> pd.DataFrame:
    """High-level pass/fail/warn checks for solved network sanity."""
    checks: list[tuple[str, str, str]] = []
    checks.append(("snapshots_present", "pass" if len(n.snapshots) > 0 else "fail", str(len(n.snapshots))))
    checks.append(("line_flows_present", "pass" if not n.lines_t.p0.empty else "fail", str(n.lines_t.p0.shape)))
    checks.extend(timeseries_alignment_checks(n).itertuples(index=False, name=None))

    if n.loads_t.p_set.empty:
        checks.append(("load_timeseries_present", "fail", "No load timeseries"))
    else:
        total_load = n.loads_t.p_set.sum(axis=1)
        checks.append(("load_nonnegative", "pass" if (total_load >= 0).all() else "fail", f"min={total_load.min():.2f} MW"))

    load_shed_share = float(ens_summary.loc[ens_summary.metric.eq("load_shed_share_pct"), "value"].iloc[0])
    status = "pass" if load_shed_share <= 0.5 else ("warning" if load_shed_share <= 2.0 else "fail")
    checks.append(("load_shed_share_pct", status, f"{load_shed_share:.4f}%"))

    if np.isfinite(getattr(n, "objective", np.nan)):
        checks.append(("objective_finite", "pass", f"{float(n.objective):.2f}"))
    else:
        checks.append(("objective_finite", "warning", "objective not stored in netcdf"))

    return pd.DataFrame(checks, columns=["check", "status", "detail"])


def run_validation(
    network: Path = DEFAULT_SOLVED_NETWORK,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    powerplants_csv: Path = DEFAULT_POWERPLANTS_CSV,
) -> None:
    if not network.exists():
        raise FileNotFoundError(f"Solved network not found: {network}")
    if not powerplants_csv.exists():
        raise FileNotFoundError(f"Reference powerplants file not found: {powerplants_csv}")

    output_dir.mkdir(parents=True, exist_ok=True)
    n = pypsa.Network(network)

    installed = installed_capacities(n)
    generation = generation_totals(n)
    ens_hourly, ens_country, ens_summary = load_not_served(n)
    cap_compare = capacity_vs_reference(installed, powerplants_csv)
    overview = model_fidelity_overview(n, installed, generation, ens_summary, cap_compare)
    checks = validation_summary(n, ens_summary)

    installed.to_csv(output_dir / f"installed_capacity_by_country_carrier_{SIM_YEAR}.csv", index=False)
    generation.to_csv(output_dir / f"generation_estimate_by_country_carrier_{SIM_YEAR}.csv", index=False)
    ens_hourly.to_csv(output_dir / f"load_shedding_hourly_{SIM_YEAR}.csv")
    ens_country.to_csv(output_dir / f"load_shedding_by_country_{SIM_YEAR}.csv", index=False)
    ens_summary.to_csv(output_dir / f"load_shedding_summary_{SIM_YEAR}.csv", index=False)
    cap_compare.to_csv(output_dir / f"capacity_validation_country_carrier_{SIM_YEAR}.csv", index=False)
    checks.to_csv(output_dir / f"model_validation_summary_{SIM_YEAR}.csv", index=False)
    overview.to_csv(output_dir / f"model_fidelity_overview_{SIM_YEAR}.csv", index=False)

    country_totals = cap_compare.groupby("country", as_index=False)[["installed_mw", "reference_mw", "delta_mw"]].sum()
    country_totals["delta_pct_vs_reference"] = np.where(
        country_totals["reference_mw"] > 0,
        100.0 * country_totals["delta_mw"] / country_totals["reference_mw"],
        np.nan,
    )
    country_totals.to_csv(output_dir / f"capacity_validation_country_totals_{SIM_YEAR}.csv", index=False)

    print("Validation files written to:", output_dir)
    print("Top-level checks:")
    print(checks.to_string(index=False))
    print("Model fidelity overview:")
    print(overview.to_string(index=False))


def main() -> None:
    args = parse_args()
    run_validation(args.network, args.output_dir, args.powerplants_csv)


if __name__ == "__main__":
    main()

