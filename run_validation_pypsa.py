"""Run pypsa-validation utilities for solved PyPSA networks.

Exports model diagnostics used to assess how close the run is to a real-world case:
- total generation estimates
- installed capacities
- load not served (ENS)
- capacity comparison against processed real-world reference data
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import pypsa

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from plotting import plot_average_load_map

SIM_YEAR = 2025
PROJECT_DIR = Path(__file__).resolve().parent
PYPSA_EUR_DIR = PROJECT_DIR.parent / "pypsa-eur"
DEFAULT_SOLVED_NETWORK = PYPSA_EUR_DIR / "results" / "kupferzell_2024_simple" / "networks" / "base_s_256_elec_.nc"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "results"
DEFAULT_POWERPLANTS_CSV = PYPSA_EUR_DIR / "resources" / "kupferzell_2024_simple" / "powerplants_s_256.csv"
DEFAULT_EUROSTAT_CSV = PYPSA_EUR_DIR / "resources" / "eurostat_energy_balances.csv"
DEFAULT_VERSIONS_CSV = PYPSA_EUR_DIR / "data" / "versions.csv"
GENERATION_MIX_DOC_PNG = (
    PYPSA_EUR_DIR / "doc" / "img" / "validation_country_generation_mix_overview.png"
)
KUPFERZELL_LON = 9.695
KUPFERZELL_LAT = 49.227


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate solved PyPSA run and export diagnostics")
    p.add_argument("--network", type=Path, default=DEFAULT_SOLVED_NETWORK, help="Solved PyPSA network netcdf")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=(
            "Validation output root directory. Results are written to "
            "<output-dir>/<scenario>/pypsa-validation, where scenario is inferred from the network path."
        ),
    )
    p.add_argument(
        "--powerplants-csv",
        type=Path,
        default=DEFAULT_POWERPLANTS_CSV,
        help="Reference capacities from PyPSA preprocessing",
    )
    p.add_argument(
        "--eurostat-csv",
        type=Path,
        default=DEFAULT_EUROSTAT_CSV,
        help="Reference Eurostat balances exported by PyPSA-Eur",
    )
    p.add_argument(
        "--versions-csv",
        type=Path,
        default=DEFAULT_VERSIONS_CSV,
        help="PyPSA-Eur data versions.csv used for reference provenance",
    )
    return p.parse_args()


def _country_from_bus(series: pd.Series) -> pd.Series:
    return series.astype(str).str[:2]


def infer_validation_scenario(network: Path) -> str:
    """Infer a stable scenario label from the input network path."""
    text = str(network).lower()
    parts = [p.lower() for p in network.parts]

    # Prefer explicit run folder names if present.
    for part in parts:
        if "kupferzell" in part and "simple" in part:
            return "kupferzell_simple"
        if "kupferzell" in part and "full" in part:
            return "kupferzell_full"

    # Fallback to broad matching on the full path string.
    if "kupferzell" in text and "simple" in text:
        return "kupferzell_simple"
    if "kupferzell" in text and "full" in text:
        return "kupferzell_full"

    return "default"


def resolve_validation_output_dir(network: Path, output_root: Path) -> tuple[Path, str]:
    """Return run-specific output directory under output_root."""
    scenario = infer_validation_scenario(network)
    return output_root / scenario / "pypsa-validation", scenario


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


def load_not_served(
    n: pypsa.Network,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Compute ENS from load-shedding generators if present."""
    ls = n.generators.index[
        n.generators.carrier.fillna("").str.lower().eq("load")
        | n.generators.index.str.lower().str.contains("load shedding")
    ]

    if len(ls) == 0 or n.generators_t.p.empty:
        hourly = pd.DataFrame(index=n.snapshots, data={"load_shed_mw": 0.0})
        by_country = pd.DataFrame(columns=["country", "load_shed_mwh"])
        hourly_by_country = pd.DataFrame(index=n.snapshots)
    else:
        p = n.generators_t.p[ls].clip(lower=0)
        hourly = p.sum(axis=1).to_frame("load_shed_mw")
        meta = n.generators.loc[ls, ["bus"]].copy()
        meta["country"] = _country_from_bus(meta["bus"])
        by_gen = p.sum(axis=0).rename("load_shed_mwh")
        by_country = by_gen.to_frame().join(meta["country"]).groupby("country", as_index=False)["load_shed_mwh"].sum()
        p_country = p.T.join(meta["country"]).groupby("country").sum().T
        hourly_by_country = p_country

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
    return hourly, by_country, summary, hourly_by_country


def load_shedding_country_metrics(
    n: pypsa.Network,
    by_country: pd.DataFrame,
    hourly_by_country: pd.DataFrame,
) -> pd.DataFrame:
    """Build country-level ENS diagnostics with totals, shares, and hours."""
    if n.loads_t.p_set.empty:
        country_load = pd.DataFrame(columns=["country", "country_load_mwh"])
    else:
        load_meta = n.loads[["bus"]].copy()
        load_meta["country"] = _country_from_bus(load_meta["bus"])
        load_country = n.loads_t.p_set.T.join(load_meta["country"]).groupby("country").sum().T
        country_load = (
            load_country.sum(axis=0)
            .rename("country_load_mwh")
            .reset_index()
            .rename(columns={"index": "country"})
        )

    if hourly_by_country.empty:
        shed_hours = pd.DataFrame(columns=["country", "hours_with_load_shedding"])
    else:
        shed_hours = (
            (hourly_by_country > 1e-6)
            .sum(axis=0)
            .rename("hours_with_load_shedding")
            .reset_index()
            .rename(columns={"index": "country"})
        )

    out = by_country.merge(country_load, on="country", how="outer").merge(shed_hours, on="country", how="outer")
    out = out.fillna(0.0)
    out["load_shed_share_pct"] = np.where(
        out["country_load_mwh"] > 0,
        100.0 * out["load_shed_mwh"] / out["country_load_mwh"],
        np.nan,
    )
    out = out.sort_values("load_shed_mwh", ascending=False).reset_index(drop=True)
    return out


def plot_load_shedding_country_bars(country_metrics: pd.DataFrame, output_png: Path, output_pdf: Path) -> None:
    """Plot three country-bar panels for ENS totals, ENS share, and ENS hours."""
    if country_metrics.empty:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, "No load shedding data available", ha="center", va="center")
        ax.axis("off")
        fig.tight_layout()
        fig.savefig(output_png, dpi=160)
        fig.savefig(output_pdf)
        plt.close(fig)
        return

    df = country_metrics.copy()
    df = df.sort_values("load_shed_mwh", ascending=False)

    x = np.arange(len(df))
    fig, axes = plt.subplots(1, 3, figsize=(18, 6), sharex=True)

    axes[0].bar(x, df["load_shed_mwh"], color="tab:red")
    axes[0].set_title("Total load shed")
    axes[0].set_ylabel("MWh")

    axes[1].bar(x, df["load_shed_share_pct"], color="tab:orange")
    axes[1].set_title("Shed share of country load")
    axes[1].set_ylabel("%")

    axes[2].bar(x, df["hours_with_load_shedding"], color="tab:purple")
    axes[2].set_title("Hours with load shedding")
    axes[2].set_ylabel("hours")

    for ax in axes:
        ax.set_xticks(x)
        ax.set_xticklabels(df["country"], rotation=45, ha="right")

    fig.suptitle(f"Country load shedding diagnostics ({SIM_YEAR})")
    fig.tight_layout()
    fig.savefig(output_png, dpi=180)
    fig.savefig(output_pdf)
    plt.close(fig)


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
    """Summarize the main pypsa-validation KPIs in a single table."""

    total_generation_twh = float(generation["generation_twh"].sum()) if not generation.empty else np.nan
    total_installed_mw = float(installed["installed_mw"].sum()) if not installed.empty else np.nan
    total_shed_mwh = float(ens_summary.loc[ens_summary.metric.eq("total_load_shed_mwh"), "value"].iloc[0])
    shed_hours = int(ens_summary.loc[ens_summary.metric.eq("hours_with_load_shedding"), "value"].iloc[0])
    shed_share = float(ens_summary.loc[ens_summary.metric.eq("load_shed_share_pct"), "value"].iloc[0])

    country_totals = cap_compare.groupby("country", as_index=False)[
        ["installed_mw", "reference_mw", "delta_mw"]
    ].sum()
    country_totals["delta_pct_vs_reference"] = np.where(
        country_totals["reference_mw"] > 0,
        100.0 * country_totals["delta_mw"] / country_totals["reference_mw"],
        np.nan,
    )
    max_country_gap = (
        float(country_totals["delta_pct_vs_reference"].abs().max())
        if not country_totals.empty
        else np.nan
    )
    carrier_threshold = 10.0
    large_refs = cap_compare[cap_compare["reference_mw"] >= carrier_threshold]
    max_country_carrier_gap = (
        float(large_refs["delta_pct_vs_reference"].abs().max())
        if not large_refs.empty
        else np.nan
    )
    max_country_carrier_gap_mw = (
        float(cap_compare["delta_mw"].abs().max()) if not cap_compare.empty else np.nan
    )

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


def assign_locations_like_pypsa_eur(n: pypsa.Network) -> None:
    """Assign component locations with the same pattern used in PyPSA-Eur summaries."""
    bus_locations = n.buses.get("location", pd.Series(index=n.buses.index, dtype=object))
    bus_locations = bus_locations.astype(str).str.strip()
    fallback = n.buses.index.to_series().astype(str).str[:2]
    invalid = bus_locations.isin(["", "nan", "None"])
    bus_locations = bus_locations.where(~invalid, fallback)
    n.buses["location"] = bus_locations.astype(str)

    for c in n.components[n.one_port_components]:
        if c.static.empty or "bus" not in c.static.columns:
            continue
        c.static["location"] = c.static.bus.map(n.buses.location)

    for c in n.components[n.branch_components]:
        if c.static.empty:
            continue
        c_bus_cols = c.static.filter(regex="^bus")
        if c_bus_cols.empty:
            continue
        locs = c_bus_cols.apply(lambda s: s.map(n.buses.location)).sort_index(axis=1)
        c.static["location"] = locs.apply(
            lambda row: next((loc for loc in row.dropna() if loc != "EU"), "EU"),
            axis=1,
        )


def calculate_nodal_energy_balance_like_pypsa_eur(n: pypsa.Network) -> pd.Series:
    """Calculate nodal energy balance grouped by carrier, location and bus_carrier."""
    assign_locations_like_pypsa_eur(n)
    return n.statistics.energy_balance(groupby=["carrier", "location", "bus_carrier"])


def _map_model_generation_carrier(carrier: str) -> str:
    c = str(carrier).strip().lower()

    if "solar" in c:
        return "solar"
    if "wind" in c or c.startswith("onwind") or c.startswith("offwind"):
        return "wind"
    if "hydro" in c or "run of river" in c or c in {"ror", "phs"}:
        return "hydro"
    if "nuclear" in c:
        return "nuclear"
    if "biomass" in c or c.startswith("bio"):
        return "biomass"
    if "waste" in c:
        return "waste"
    if "geothermal" in c:
        return "geothermal"
    if "lignite" in c:
        return "lignite"
    if "coal" in c:
        return "coal"
    if "oil" in c or "diesel" in c:
        return "oil"
    if "gas" in c or c in {"ocgt", "ccgt"}:
        return "gas"

    return "other"


def _map_eurostat_siec_to_carrier(siec: str) -> str:
    s = str(siec).strip().upper()

    if s.startswith("RA3"):
        return "wind"
    if s.startswith("RA4"):
        return "solar"
    if s.startswith("RA1"):
        return "hydro"
    if s.startswith("RA5") or s == "BIOE":
        return "biomass"
    if s.startswith("RA6"):
        return "geothermal"
    if s.startswith("N9"):
        return "nuclear"
    if s.startswith("G3"):
        return "gas"
    if s.startswith("C"):
        return "coal"
    if s.startswith("O"):
        return "oil"
    if s.startswith("W"):
        return "waste"

    return "other"


def model_generation_mix_by_country(n: pypsa.Network) -> pd.DataFrame:
    """Build country-level modeled electricity generation mix from nodal balances."""
    balance = calculate_nodal_energy_balance_like_pypsa_eur(n)
    if balance.empty:
        return pd.DataFrame(columns=["country", "carrier_palette", "model_twh"])

    df = balance.reset_index(name="mwh")
    df = df[df["component"].isin(["Generator", "StorageUnit"])].copy()
    df = df[df["bus_carrier"].eq("AC")].copy()
    df = df[df["mwh"] > 0].copy()

    if df.empty:
        return pd.DataFrame(columns=["country", "carrier_palette", "model_twh"])

    df["country"] = df["location"].astype(str).str[:2]
    df["carrier_palette"] = df["carrier"].map(_map_model_generation_carrier)

    out = (
        df.groupby(["country", "carrier_palette"], as_index=False)["mwh"]
        .sum()
        .rename(columns={"mwh": "model_twh"})
    )
    out["model_twh"] = out["model_twh"] / 1e6
    return out.sort_values(["country", "carrier_palette"]).reset_index(drop=True)


def simulation_time_context(n: pypsa.Network) -> dict[str, object]:
    """Return simulation window and represented hours for fair annual reference scaling."""
    if len(n.snapshots) == 0:
        return {
            "sim_start": pd.NaT,
            "sim_end": pd.NaT,
            "sim_hours": np.nan,
        }

    sim_start = pd.Timestamp(n.snapshots.min())
    sim_end = pd.Timestamp(n.snapshots.max())

    sim_hours = np.nan
    if hasattr(n, "snapshot_weightings") and "generators" in n.snapshot_weightings.columns:
        sim_hours = float(n.snapshot_weightings["generators"].sum())

    if not np.isfinite(sim_hours):
        if len(n.snapshots) > 1:
            diffs_h = n.snapshots.to_series().diff().dropna().dt.total_seconds() / 3600.0
            step_h = float(diffs_h.mode().iloc[0]) if not diffs_h.empty else 1.0
            sim_hours = step_h * float(len(n.snapshots))
        else:
            sim_hours = 1.0

    return {
        "sim_start": sim_start,
        "sim_end": sim_end,
        "sim_hours": sim_hours,
    }


def load_eurostat_generation_mix(
    eurostat_csv: Path,
    year: int,
    versions_csv: Path,
    sim_context: dict[str, object] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load Eurostat generation mix and attach version provenance."""
    eurostat = pd.read_csv(eurostat_csv)
    eurostat = eurostat[eurostat["nrg_bal"] == "GEP"].copy()
    available_years = sorted(eurostat["year"].dropna().astype(int).unique())
    if not available_years:
        raise ValueError(f"No GEP rows found in Eurostat balances file: {eurostat_csv}")
    reference_year = year if year in available_years else max(y for y in available_years if y <= max(available_years))
    if year in available_years:
        reference_year = year
    else:
        candidates = [y for y in available_years if y <= year]
        reference_year = max(candidates) if candidates else max(available_years)

    eurostat = eurostat[eurostat["year"] == reference_year].copy()
    eurostat = eurostat[eurostat["value"].notna()].copy()
    eurostat["country"] = eurostat["country"].astype(str)
    eurostat = eurostat[eurostat["country"].str.len() == 2].copy()
    eurostat["carrier_palette"] = eurostat["siec"].map(_map_eurostat_siec_to_carrier)

    ref = (
        eurostat.groupby(["country", "carrier_palette"], as_index=False)["value"]
        .sum()
        .rename(columns={"value": "reference_twh_annual"})
    )

    year_hours = (
        pd.Timestamp(year=reference_year + 1, month=1, day=1)
        - pd.Timestamp(year=reference_year, month=1, day=1)
    ).total_seconds() / 3600.0
    sim_hours = float((sim_context or {}).get("sim_hours", np.nan))
    coverage_factor = (sim_hours / year_hours) if (np.isfinite(sim_hours) and year_hours > 0) else 1.0
    ref["reference_twh"] = ref["reference_twh_annual"] * coverage_factor

    versions = pd.read_csv(versions_csv)
    version_info = versions.loc[
        versions["dataset"].eq("eurostat_balances")
        & versions["source"].isin(["archive", "primary"])
    ].copy()
    if not version_info.empty and "added" in version_info.columns:
        version_info = version_info.sort_values("added")
        version_info = version_info.tail(1)
    if version_info.empty:
        version_info = pd.DataFrame(
            [
                {
                    "dataset": "eurostat_balances",
                    "version": "unknown",
                    "source": "unknown",
                    "added": "",
                    "note": "No matching entry found in versions.csv",
                    "url": "",
                }
            ]
        )

    version_info = version_info.copy()
    version_info["reference_year"] = reference_year
    version_info["sim_start"] = (sim_context or {}).get("sim_start", pd.NaT)
    version_info["sim_end"] = (sim_context or {}).get("sim_end", pd.NaT)
    version_info["sim_hours"] = sim_hours
    version_info["reference_year_hours"] = year_hours
    version_info["coverage_factor"] = coverage_factor
    version_info["reference_scaled_to_simulation_window"] = bool(np.isfinite(sim_hours))

    return ref.sort_values(["country", "carrier_palette"]).reset_index(drop=True), version_info


def compare_country_generation_mix(model_mix: pd.DataFrame, ref_mix: pd.DataFrame) -> pd.DataFrame:
    """Compare modeled and historical generation mix at country/carrier level."""
    comparison = model_mix.merge(ref_mix, on=["country", "carrier_palette"], how="outer").fillna(0.0)

    if "reference_twh_annual" not in comparison.columns:
        comparison["reference_twh_annual"] = comparison["reference_twh"]

    model_totals = model_mix.groupby("country", as_index=False)["model_twh"].sum().rename(
        columns={"model_twh": "model_total_twh"}
    )
    ref_totals = ref_mix.groupby("country", as_index=False)["reference_twh"].sum().rename(
        columns={"reference_twh": "reference_total_twh"}
    )
    ref_totals_annual = ref_mix.groupby("country", as_index=False)["reference_twh_annual"].sum().rename(
        columns={"reference_twh_annual": "reference_total_twh_annual"}
    )
    totals = model_totals.merge(ref_totals, on="country", how="outer")
    totals = totals.merge(ref_totals_annual, on="country", how="outer").fillna(0.0)

    comparison = comparison.merge(totals, on="country", how="left")
    comparison["model_share_pct"] = np.where(
        comparison["model_total_twh"] > 0,
        100.0 * comparison["model_twh"] / comparison["model_total_twh"],
        np.nan,
    )
    comparison["reference_share_pct"] = np.where(
        comparison["reference_total_twh"] > 0,
        100.0 * comparison["reference_twh"] / comparison["reference_total_twh"],
        np.nan,
    )
    comparison["delta_twh"] = comparison["model_twh"] - comparison["reference_twh"]
    comparison["delta_twh_vs_annual"] = comparison["model_twh"] - comparison["reference_twh_annual"]
    comparison["delta_share_pct_points"] = (
        comparison["model_share_pct"] - comparison["reference_share_pct"]
    )

    cols = [
        "country",
        "carrier_palette",
        "model_twh",
        "reference_twh_annual",
        "reference_twh",
        "delta_twh",
        "delta_twh_vs_annual",
        "model_share_pct",
        "reference_share_pct",
        "delta_share_pct_points",
        "model_total_twh",
        "reference_total_twh_annual",
        "reference_total_twh",
    ]
    return comparison[cols].sort_values(["country", "carrier_palette"]).reset_index(drop=True)


def country_generation_mix_quick_overview(comparison: pd.DataFrame) -> pd.DataFrame:
    """Create an easy-to-read country summary from detailed mix comparison."""
    if comparison.empty:
        return pd.DataFrame(
            columns=[
                "country",
                "model_total_twh",
                "reference_total_twh",
                "delta_total_twh",
                "mean_abs_share_gap_pct_points",
                "max_abs_share_gap_pct_points",
            ]
        )

    summary = (
        comparison.groupby("country", as_index=False)
        .agg(
            model_total_twh=("model_total_twh", "first"),
            reference_total_twh=("reference_total_twh", "first"),
            mean_abs_share_gap_pct_points=("delta_share_pct_points", lambda s: s.abs().mean()),
            max_abs_share_gap_pct_points=("delta_share_pct_points", lambda s: s.abs().max()),
        )
        .sort_values("mean_abs_share_gap_pct_points", ascending=False)
    )
    summary["delta_total_twh"] = summary["model_total_twh"] - summary["reference_total_twh"]
    ordered_cols = [
        "country",
        "model_total_twh",
        "reference_total_twh",
        "delta_total_twh",
        "mean_abs_share_gap_pct_points",
        "max_abs_share_gap_pct_points",
    ]
    return summary[ordered_cols].reset_index(drop=True)


def plot_country_generation_mix_overview(
    summary: pd.DataFrame,
    output_png: Path,
    output_pdf: Path,
) -> None:
    """Plot a compact two-panel overview for quick pypsa-validation checks."""
    if summary.empty:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, "No generation-mix data available", ha="center", va="center")
        ax.axis("off")
        fig.tight_layout()
        fig.savefig(output_png, dpi=160)
        fig.savefig(output_pdf)
        plt.close(fig)
        return

    top_by_reference = summary.sort_values("reference_total_twh", ascending=False).head(12)
    top_by_gap = summary.sort_values("mean_abs_share_gap_pct_points", ascending=False).head(12)

    fig, axes = plt.subplots(1, 2, figsize=(15, 6))

    x = np.arange(len(top_by_reference))
    width = 0.42
    axes[0].bar(x - width / 2, top_by_reference["reference_total_twh"], width=width, label="Eurostat")
    axes[0].bar(x + width / 2, top_by_reference["model_total_twh"], width=width, label="Model")
    axes[0].set_title("Country total generation (top by Eurostat)")
    axes[0].set_ylabel("TWh")
    axes[0].set_xticks(x)
    axes[0].set_xticklabels(top_by_reference["country"], rotation=45, ha="right")
    axes[0].legend()

    axes[1].barh(
        top_by_gap["country"],
        top_by_gap["mean_abs_share_gap_pct_points"],
        color="tab:orange",
    )
    axes[1].invert_yaxis()
    axes[1].set_title("Country mean absolute mix-share gap")
    axes[1].set_xlabel("Percentage points")

    fig.suptitle(f"Generation mix pypsa-validation overview ({SIM_YEAR})")
    fig.tight_layout()
    fig.savefig(output_png, dpi=180)
    fig.savefig(output_pdf)
    plt.close(fig)


def _prepare_pie_series(values: pd.Series, min_share_pct: float = 1.0) -> pd.Series:
    """Sort and optionally group tiny slices into an 'other' category for readability."""
    s = values.dropna().copy()
    s = s[s > 0]
    if s.empty:
        return s

    s = s.sort_values(ascending=False)
    total = float(s.sum())
    if total <= 0:
        return pd.Series(dtype=float)

    small = (100.0 * s / total) < min_share_pct
    if small.any():
        other = float(s[small].sum())
        s = s[~small]
        if other > 0:
            s.loc["other"] = other
    return s.sort_values(ascending=False)


def _plot_single_pie(ax: plt.Axes, values: pd.Series, title: str, unit: str) -> None:
    if values.empty:
        ax.text(0.5, 0.5, "No data", ha="center", va="center")
        ax.axis("off")
        ax.set_title(title)
        return

    wedges, _, _ = ax.pie(
        values.values,
        labels=values.index,
        autopct="%.1f%%",
        startangle=110,
        textprops={"fontsize": 8},
    )
    ax.set_title(f"{title}\nTotal: {values.sum():.2f} {unit}")
    ax.legend(wedges, values.index, loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=8)


def plot_technology_mix_pies(
    installed_by_carrier: pd.DataFrame,
    generation_model_by_carrier: pd.DataFrame,
    generation_ref_by_carrier: pd.DataFrame,
    output_png: Path,
    output_pdf: Path,
) -> None:
    """Create pie charts for capacity and generation technology mixes."""
    installed_series = _prepare_pie_series(
        installed_by_carrier.set_index("carrier_cmp")["installed_mw"]
    )
    model_gen_series = _prepare_pie_series(
        generation_model_by_carrier.set_index("carrier_cmp")["model_twh"]
    )
    ref_gen_series = _prepare_pie_series(
        generation_ref_by_carrier.set_index("carrier_palette")["reference_twh"]
    )

    fig, axes = plt.subplots(1, 3, figsize=(18, 6))
    _plot_single_pie(axes[0], installed_series, "Installed capacity mix", "MW")
    _plot_single_pie(axes[1], model_gen_series, "Model generation mix", "TWh")
    _plot_single_pie(axes[2], ref_gen_series, "Eurostat generation mix (scaled)", "TWh")

    fig.suptitle(f"Technology mix validation pies ({SIM_YEAR})")
    fig.tight_layout()
    fig.savefig(output_png, dpi=180)
    fig.savefig(output_pdf)
    plt.close(fig)


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


def annual_load_by_bus(n: pypsa.Network) -> pd.Series:
    """Return per-bus mean load in MW, aggregated across all loads at each bus."""
    if n.loads.empty:
        return pd.Series(dtype=float)

    p_t = getattr(n.loads_t, "p", None)
    if p_t is None or p_t.empty:
        p_t = getattr(n.loads_t, "p_set", None)
    if p_t is None or p_t.empty:
        return pd.Series(dtype=float)

    mean_per_load = p_t.mean(axis=0)
    bus_map = n.loads["bus"].reindex(mean_per_load.index)
    return (
        pd.DataFrame({"bus": bus_map, "mw": mean_per_load.values})
        .groupby("bus")["mw"]
        .sum()
    )


def select_high_voltage_lines(
    n: pypsa.Network,
    buses: pd.DataFrame,
    minimum_voltage: float = 220.0,
) -> pd.DataFrame:
    """Select high-voltage lines connected to the provided bus subset."""
    if n.lines.empty:
        return n.lines.iloc[0:0].copy()

    bus_ids = pd.Index(buses.index)
    lines = n.lines[n.lines.bus0.isin(bus_ids) | n.lines.bus1.isin(bus_ids)].copy()
    if lines.empty:
        return lines

    if minimum_voltage > 0 and "v_nom" in lines.columns:
        v_nom = pd.to_numeric(lines["v_nom"], errors="coerce")
        lines = lines.loc[v_nom >= minimum_voltage]
    return lines


def plot_germany_bus_load_map(
    bus_load_mw: pd.Series,
    buses: pd.DataFrame,
    output_png: Path,
    output_pdf: Path,
    hv_lines: pd.DataFrame | None = None,
) -> None:
    """Plot a DE nodal load map to PNG/PDF using the shared plotting helper."""
    lines = hv_lines if hv_lines is not None else pd.DataFrame(columns=["bus0", "bus1"])
    plot_average_load_map(
        buses=buses,
        load_per_bus_mw=bus_load_mw,
        lines=lines,
        output_path=str(output_png),
        title=f"Germany nodal load map ({SIM_YEAR})",
        colorbar_label="Mean load [MW]",
    )
    plot_average_load_map(
        buses=buses,
        load_per_bus_mw=bus_load_mw,
        lines=lines,
        output_path=str(output_pdf),
        title=f"Germany nodal load map ({SIM_YEAR})",
        colorbar_label="Mean load [MW]",
    )


def run_validation(
    network: Path = DEFAULT_SOLVED_NETWORK,
    output_dir: Path = DEFAULT_OUTPUT_ROOT,
    powerplants_csv: Path = DEFAULT_POWERPLANTS_CSV,
    eurostat_csv: Path = DEFAULT_EUROSTAT_CSV,
    versions_csv: Path = DEFAULT_VERSIONS_CSV,
) -> None:
    if not network.exists():
        raise FileNotFoundError(f"Solved network not found: {network}")
    if not powerplants_csv.exists():
        raise FileNotFoundError(f"Reference powerplants file not found: {powerplants_csv}")
    if not eurostat_csv.exists():
        raise FileNotFoundError(f"Eurostat balances file not found: {eurostat_csv}")
    if not versions_csv.exists():
        raise FileNotFoundError(f"versions.csv file not found: {versions_csv}")

    resolved_output_dir, scenario = resolve_validation_output_dir(network, output_dir)
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    n = pypsa.Network(network)

    installed = installed_capacities(n)
    generation = generation_totals(n)
    ens_hourly, ens_country, ens_summary, ens_hourly_by_country = load_not_served(n)
    ens_country_metrics = load_shedding_country_metrics(n, ens_country, ens_hourly_by_country)
    cap_compare = capacity_vs_reference(installed, powerplants_csv)
    overview = model_fidelity_overview(n, installed, generation, ens_summary, cap_compare)
    checks = validation_summary(n, ens_summary)

    installed.to_csv(resolved_output_dir / f"installed_capacity_by_country_carrier_{SIM_YEAR}.csv", index=False)
    generation.to_csv(resolved_output_dir / f"generation_estimate_by_country_carrier_{SIM_YEAR}.csv", index=False)
    ens_hourly.to_csv(resolved_output_dir / f"load_shedding_hourly_{SIM_YEAR}.csv")
    ens_country.to_csv(resolved_output_dir / f"load_shedding_by_country_{SIM_YEAR}.csv", index=False)
    ens_country_metrics.to_csv(resolved_output_dir / f"load_shedding_country_metrics_{SIM_YEAR}.csv", index=False)
    ens_summary.to_csv(resolved_output_dir / f"load_shedding_summary_{SIM_YEAR}.csv", index=False)
    cap_compare.to_csv(resolved_output_dir / f"capacity_validation_country_carrier_{SIM_YEAR}.csv", index=False)
    checks.to_csv(resolved_output_dir / f"model_validation_summary_{SIM_YEAR}.csv", index=False)
    overview.to_csv(resolved_output_dir / f"model_fidelity_overview_{SIM_YEAR}.csv", index=False)

    country_totals = cap_compare.groupby("country", as_index=False)[["installed_mw", "reference_mw", "delta_mw"]].sum()
    country_totals["delta_pct_vs_reference"] = np.where(
        country_totals["reference_mw"] > 0,
        100.0 * country_totals["delta_mw"] / country_totals["reference_mw"],
        np.nan,
    )
    country_totals.to_csv(resolved_output_dir / f"capacity_validation_country_totals_{SIM_YEAR}.csv", index=False)

    ens_plot_png = resolved_output_dir / f"figure_load_shedding_country_bars_{SIM_YEAR}.png"
    ens_plot_pdf = resolved_output_dir / f"figure_load_shedding_country_bars_{SIM_YEAR}.pdf"
    plot_load_shedding_country_bars(ens_country_metrics, ens_plot_png, ens_plot_pdf)

    bus_load = annual_load_by_bus(n)
    buses = n.buses.copy()
    if "country" not in buses.columns:
        buses["country"] = _country_from_bus(buses.index.to_series())
    buses_de = buses[buses["country"].eq("DE")]
    bus_load = bus_load.reindex(buses_de.index).dropna()
    hv_lines = select_high_voltage_lines(n, buses_de)
    load_map_png = resolved_output_dir / f"figure_germany_nodal_load_map_{SIM_YEAR}.png"
    load_map_pdf = resolved_output_dir / f"figure_germany_nodal_load_map_{SIM_YEAR}.pdf"
    plot_germany_bus_load_map(bus_load, buses_de, load_map_png, load_map_pdf, hv_lines=hv_lines)

    generation_mix_dir = resolved_output_dir / "generation_mix"
    generation_mix_dir.mkdir(parents=True, exist_ok=True)
    model_mix = model_generation_mix_by_country(n)
    sim_context = simulation_time_context(n)
    reference_mix, reference_meta = load_eurostat_generation_mix(
        eurostat_csv=eurostat_csv,
        year=SIM_YEAR,
        versions_csv=versions_csv,
        sim_context=sim_context,
    )
    mix_comparison = compare_country_generation_mix(model_mix, reference_mix)
    mix_summary = country_generation_mix_quick_overview(mix_comparison)

    installed_by_carrier = (
        installed.groupby("carrier_cmp", as_index=False)["installed_mw"]
        .sum()
        .sort_values("installed_mw", ascending=False)
    )
    generation_model_by_carrier = (
        model_mix.groupby("carrier_palette", as_index=False)["model_twh"]
        .sum()
        .rename(columns={"carrier_palette": "carrier_cmp"})
        .sort_values("model_twh", ascending=False)
    )
    generation_ref_by_carrier = (
        reference_mix.groupby("carrier_palette", as_index=False)[["reference_twh", "reference_twh_annual"]]
        .sum()
        .sort_values("reference_twh", ascending=False)
    )

    mix_comparison.to_csv(
        generation_mix_dir / "country_generation_mix_comparison.csv",
        index=False,
    )
    mix_summary.to_csv(
        generation_mix_dir / "country_generation_mix_quick_overview.csv",
        index=False,
    )
    installed_by_carrier.to_csv(
        generation_mix_dir / "technology_installed_capacity_totals.csv",
        index=False,
    )
    generation_model_by_carrier.to_csv(
        generation_mix_dir / "technology_generation_totals_model.csv",
        index=False,
    )
    generation_ref_by_carrier.to_csv(
        generation_mix_dir / "technology_generation_totals_eurostat_scaled.csv",
        index=False,
    )
    reference_meta.to_csv(
        generation_mix_dir / "eurostat_reference_metadata.csv",
        index=False,
    )

    overview_png = generation_mix_dir / "country_generation_mix_overview.png"
    overview_pdf = generation_mix_dir / "country_generation_mix_overview.pdf"
    plot_country_generation_mix_overview(
        summary=mix_summary,
        output_png=overview_png,
        output_pdf=overview_pdf,
    )
    tech_pies_png = generation_mix_dir / "technology_mix_pies.png"
    tech_pies_pdf = generation_mix_dir / "technology_mix_pies.pdf"
    plot_technology_mix_pies(
        installed_by_carrier=installed_by_carrier,
        generation_model_by_carrier=generation_model_by_carrier,
        generation_ref_by_carrier=generation_ref_by_carrier,
        output_png=tech_pies_png,
        output_pdf=tech_pies_pdf,
    )
    GENERATION_MIX_DOC_PNG.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(overview_png, GENERATION_MIX_DOC_PNG)

    print("Validation scenario:", scenario)
    print("Validation files written to:", resolved_output_dir)
    print("Top-level checks:")
    print(checks.to_string(index=False))
    print("Model fidelity overview:")
    print(overview.to_string(index=False))
    if not reference_meta.empty and "coverage_factor" in reference_meta.columns:
        scale = float(reference_meta["coverage_factor"].iloc[0])
        print(f"Eurostat annual totals scaled to simulation window with factor: {scale:.6f}")
    print("Generation mix pypsa-validation files written to:", generation_mix_dir)


def main() -> None:
    args = parse_args()
    run_validation(
        args.network,
        args.output_dir,
        args.powerplants_csv,
        args.eurostat_csv,
        args.versions_csv,
    )


if __name__ == "__main__":
    main()
