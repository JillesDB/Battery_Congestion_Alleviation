import pandas as pd
import numpy as np
import os
from pathlib import Path
path_parent_folder = str((Path.cwd().resolve().parents[0])) # FOR HPC THIS IS 0, for Laptop it is 1

def add_residual_load(df_exog: pd.DataFrame) -> pd.DataFrame:
    """Compute residual load = Load Forecast - (Solar + Total Wind)."""
    df_exog = df_exog.copy()
    for col in ["Load Forecast", "Solar", "Total Wind"]:
        if col not in df_exog.columns:
            print(f"Warning: exogenous column '{col}' missing.")
    if "Residual Load" in df_exog.columns:
        df_exog.drop(columns=["Residual Load"], inplace=True)
    df_exog["Total RES"] = df_exog["Solar"].fillna(0) + df_exog["Total Wind"].fillna(0)
    df_exog["Residual Load"] = df_exog["Load Forecast"].fillna(0) - df_exog["Total RES"]
    return df_exog

def add_marginal_cost_ocgt(
    df: pd.DataFrame,
    price_col_gas: str = "TTF NG [EUR/MWh]",
    co2_col: str = "Carbon Permits [EUR/tCO2]",
    eta: float = 0.40,
    ef_t_per_MWh_fuel: float = 0.202,
    vom: float = 3.0
) -> pd.DataFrame:
    """
    Add a column 'MC_OCGT' to df estimating marginal cost (EUR/MWh_electric)
    for open-cycle gas turbines (OCGT).
    """
    df = df.copy()
    fuel_price = df[price_col_gas].astype(float)
    fuel_cost_per_MWh_elec = fuel_price / eta
    co2_cost_per_MWh_elec = (df[co2_col].astype(float) * ef_t_per_MWh_fuel) / eta
    if "MC_OCGT" in df.columns:
        df.drop(columns=["MC_OCGT"], inplace=True)
    df["MC_OCGT"] = fuel_cost_per_MWh_elec + co2_cost_per_MWh_elec + vom
    return df

def add_marginal_cost_ccgt(
    df: pd.DataFrame,
    price_col_gas: str = "TTF NG [EUR/MWh]",
    co2_col: str = "Carbon Permits [EUR/tCO2]",
    eta: float = 0.56,
    ef_t_per_MWh_fuel: float = 0.202,
    vom: float = 3.0
) -> pd.DataFrame:
    """
    Add a column 'MC_OCGT' to df estimating marginal cost (EUR/MWh_electric)
    for open-cycle gas turbines (OCGT).
    """
    df = df.copy()
    fuel_price = df[price_col_gas].astype(float)
    fuel_cost_per_MWh_elec = fuel_price / eta
    co2_cost_per_mwh_elec = (df[co2_col].astype(float) * ef_t_per_MWh_fuel)/ eta
    if "MC_CCGT" in df.columns:
        df.drop(columns=["MC_CCGT"], inplace=True)
    df["MC_CCGT"] = fuel_cost_per_MWh_elec + co2_cost_per_mwh_elec + vom
    return df


def add_marginal_cost_oil_peak(
    df: pd.DataFrame,
    price_col_oil: str = "Brent Crude Oil[EUR/Barrel]",
    co2_col: str = "Carbon Permits [EUR/tCO2]",
    eta: float = 0.34, #previous: 0.38
    ef_t_per_MWh_fuel: float = 0.28, # For heavy oil: 0.29
    vom: float = 4.0,
) -> pd.DataFrame:
    """
    Add a column 'MC_Oil_Peak' to df estimating marginal cost (EUR/MWh_electric)
    for oil-fired peaking units.
    """
    df = df.copy()
    GJ_per_barrel = 6.119
    eur_per_barrel = df[price_col_oil].astype(float)
    eur_per_MWh_fuel = (eur_per_barrel / GJ_per_barrel) * 3.6
    fuel_cost_per_MWh_elec = eur_per_MWh_fuel / eta
    co2_cost_per_MWh_elec = (df[co2_col].astype(float) * ef_t_per_MWh_fuel) / eta
    if "MC_Oil_Peak" in df.columns:
        df.drop(columns=["MC_Oil_Peak"], inplace=True)
    df["MC_Oil_Peak"] = fuel_cost_per_MWh_elec + co2_cost_per_MWh_elec + vom
    return df

def add_marginal_cost_coal_baseline(
    df: pd.DataFrame,
    price_col_coal: str = "Amsterdam Coal Futures [USD/Ton]",
    co2_col: str = "Carbon Permits [EUR/tCO2]",
    eta: float = 0.4,
    ef_t_per_MWh_fuel: float = 0.34,
    vom: float = 6.0,
    GJ_per_ton_coal: float = 24.0,
    usd_to_eur: float = 0.92
) -> pd.DataFrame:
    """
    Add a column 'MC_Coal_Baseline' to df estimating marginal cost (EUR/MWh_electric)
    for coal-fired baseline units.
    """
    df = df.copy()
    price_usd_per_ton = df[price_col_coal].astype(float)
    eur_per_ton = price_usd_per_ton * usd_to_eur
    eur_per_MWh_fuel = (eur_per_ton / GJ_per_ton_coal) * 3.6
    fuel_cost_per_MWh_elec = eur_per_MWh_fuel / eta
    co2_cost_per_MWh_elec = (df[co2_col].astype(float) * ef_t_per_MWh_fuel) / eta
    if "MC_Coal_Baseline" in df.columns:
        df.drop(columns=["MC_Coal_Baseline"], inplace=True)
    df["MC_Coal_Baseline"] = fuel_cost_per_MWh_elec + co2_cost_per_MWh_elec + vom
    return df


def add_marginal_cost_lignite(
        df: pd.DataFrame,
        co2_col: str = "Carbon Permits [EUR/tCO2]",
        eta: float = 0.38,
        ef_t_per_MWh_fuel: float = 0.4,
        vom: float = 6.0,
) -> pd.DataFrame:
    """
    Add a column 'MC_Coal_Baseline' to df estimating marginal cost (EUR/MWh_electric)
    for coal-fired baseline units.
    """
    df = df.copy()
    fuel_cost_per_MWh_elec = 4
    co2_cost_per_MWh_elec = (df[co2_col].astype(float) * ef_t_per_MWh_fuel) / eta
    if "MC_Lignite" in df.columns:
        df.drop(columns=["MC_Lignite"], inplace=True)
    df["MC_Lignite"] = fuel_cost_per_MWh_elec + co2_cost_per_MWh_elec + vom
    return df



def add_sunlight_dummy(df: pd.DataFrame, hour_col: str = "Hour") -> pd.DataFrame:
    """
    Add a column 'Sunlight_Dummy' equal to 1 during hours 9-17 (inclusive), 0 otherwise.

    Hour is determined in this order:
    1. If df has a DateTimeIndex, use df.index.hour
    2. Else if `hour_col` exists in df, use that column
    3. Else if a 'Time' column exists, parse it and use its hour
    Otherwise fills the column with NaN and prints a warning.
    """
    df = df.copy()

    if isinstance(df.index, pd.DatetimeIndex):
        hours = df.index.hour
    elif hour_col in df.columns:
        try:
            hours = df[hour_col].astype(int)
        except Exception:
            hours = pd.to_datetime(df[hour_col]).dt.hour
    elif "Time" in df.columns:
        hours = pd.to_datetime(df["Time"]).dt.hour
    else:
        print("Warning: cannot determine hour for Sunlight_Dummy; filling with NaN.")
        df["Sunlight_Hour"] = np.nan
        return df

    df["Sunlight_Hour"] = ((hours >= 9) & (hours <= 17)).astype(int)
    return df

def add_frequency_reserve_data(df_exog: pd.DataFrame) -> pd.DataFrame:
    """
    Efficiently loads frequency reserve data, calculates aggregated Positive and
    Negative Reserve Volumes, and merges them into the exogenous dataframe.
    """
    # 1. Load data efficiently using only the necessary columns
    file_path = os.path.join("EntsoE Driver Data", "germany", "Frequency_Reserve_data_germany_with_cet.csv")
    full_path = os.path.join(path_parent_folder, file_path)
    needed_cols = ["Time_CET", "aFRR pos", "mFRR pos", "aFRR neg", "mFRR neg"]
    df_res = pd.read_csv(full_path, parse_dates=["Time_CET"], usecols=needed_cols)

    # 2. Vectorized computation of aggregated volumes (retaining fillna(0) logic)
    df_res["Positive Reserve Vol"] = df_res["aFRR pos"].fillna(0) + df_res["mFRR pos"].fillna(0)
    df_res["Negative Reserve Vol"] = df_res["aFRR neg"].fillna(0) + df_res["mFRR neg"].fillna(0)

    # 3. Streamline for merging
    df_res = df_res[["Time_CET", "Positive Reserve Vol", "Negative Reserve Vol"]].set_index("Time_CET")

    # 4. Join with existing dataframe while preserving structure
    df_out = df_exog.copy()

    # Flexible join: handles cases where Time_CET is a column or the index
    if "Time_CET" in df_out.columns:
        df_out = df_out.merge(df_res, on="Time_CET", how="left")
    else:
        df_out = df_out.join(df_res, how="left")

    # 5. Final cleanup to ensure numerical consistency for time series analysis
    return df_out.fillna({"Positive Reserve Vol": 0, "Negative Reserve Vol": 0})

# --- Usage Example based on your provided paths ---
# file_path = os.path.join("EntsoE Driver Data", "germany", "Frequency_Reserve_data_germany_with_cet.csv")
# full_path = os.path.join(path_parent_folder, file_path)
# df_final = add_frequency_reserves(df_exog, full_path)

def add_monthly_and_rolling_averages(
    df: pd.DataFrame,
    cols: list[str],
    windows: tuple = (1, 3, 6, 12),
    month_end_freq: str = "ME",
    min_periods: int = 1
) -> pd.DataFrame:
    """
    Add month-end averages and x-month rolling averages for one or more columns.

    For each column `c` in `cols` and each x in `windows`, a column
    `{c}_{x}_monthly` is added, where:
    - x = 1 corresponds to the simple monthly mean
    - x > 1 corresponds to a rolling x-month mean (calendar-based)

    All monthly values are mapped back to the original timestamps.
    """
    df = df.copy()

    # Ensure datetime index
    if not isinstance(df.index, pd.DatetimeIndex):
        try:
            df.index = pd.to_datetime(df.index)
        except Exception:
            if "Time" in df.columns:
                df = df.set_index(pd.to_datetime(df["Time"]))
            else:
                raise

    # Precompute month-end timestamps once
    month_ends = df.index.to_period("M").to_timestamp("M")

    for col in cols:
        if col not in df.columns:
            print(f"Warning: column '{col}' missing.")
            for w in windows:
                df[f"{col}_{w}_monthly"] = np.nan
            continue

        # Monthly (month-end) mean
        monthly = df[col].resample(month_end_freq).mean()
        monthly_non_null = monthly.dropna()

        # Rolling monthly means
        rolling = {
            w: (
               monthly_non_null
                    .rolling(window=w, min_periods=min_periods)
                    .mean()
                    .reindex(monthly.index)
            )
            for w in windows
        }

        # Map month-end values back to original rows
        for w, series in rolling.items():
            df[f"{col}_{w}_monthly"] = month_ends.map(series.to_dict())


    return df




def add_missing_volumes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute missing available capacity per technology and add columns:
    `Missing_Vol_CCGT`, `Missing_Vol_OCGT`, `Missing_Vol_Oil`, `Missing_Vol_Coal`.
    If no relevant input columns are present for a technology the result is NaN.
    """
    missing_volumes_df = None

    csv_path = os.path.join(path_parent_folder, 'EntsoE Driver Data', 'germany', 'missing_capacity_germany.csv')
    if os.path.exists(rf"{csv_path}"):
        missing_volumes_df = pd.read_csv(csv_path, parse_dates=['Time'], index_col='Time')
        # reindex missing_volumes_df to df inex if df index is datetime
        if isinstance(df.index, pd.DatetimeIndex):
            missing_volumes_df = missing_volumes_df.reindex(df.index)


    df = df.copy()
    cols = set(df.columns)

    # compute fallback values using available columns in df
    computed = {}

    # helper for creating NaN result when no relevant columns exist
    def _na_result():
        return pd.Series(np.nan, index=df.index) if isinstance(df.index, pd.DatetimeIndex) else np.nan

    # CCGT
    ccgt_avail = ['Avail_CCGT']
    ccgt_gen = ['Gen_CCGT']
    if any(c in cols for c in ccgt_avail + ccgt_gen):
        avail_sum = sum((df[c].fillna(0) if c in cols else 0) for c in ccgt_avail)
        gen_sum = sum((df[c].fillna(0) if c in cols else 0) for c in ccgt_gen)
        computed['Missing_Vol_CCGT'] = (avail_sum - gen_sum).clip(lower=0)
    else:
        computed['Missing_Vol_CCGT'] = _na_result()

    # OCGT (incl. OCG and waste gas)
    ocgt_avail = ['Avail_OCGT', 'Avail_OCG', 'Avail_OCG_WasteGas']
    ocgt_gen = ['Gen_OCGT', 'Gen_OCG', 'Gen_OCG_WasteGas']
    if any(c in cols for c in ocgt_avail + ocgt_gen):
        avail_sum = sum((df[c].fillna(0) if c in cols else 0) for c in ocgt_avail)
        gen_sum = sum((df[c].fillna(0) if c in cols else 0) for c in ocgt_gen)
        computed['Missing_Vol_OCGT'] = (avail_sum - gen_sum).clip(lower=0)
    else:
        computed['Missing_Vol_OCGT'] = _na_result()

    # Oil (oil + ICE)
    oil_avail = ['Avail_Oil', 'Avail_ICE']
    oil_gen = ['Gen_Oil', 'Gen_ICE']
    if any(c in cols for c in oil_avail + oil_gen):
        avail_sum = sum((df[c].fillna(0) if c in cols else 0) for c in oil_avail)
        gen_sum = sum((df[c].fillna(0) if c in cols else 0) for c in oil_gen)
        computed['Missing_Vol_Oil'] = (avail_sum - gen_sum).clip(lower=0)
    else:
        computed['Missing_Vol_Oil'] = _na_result()

    # Coal (hard coal + lignite + conversion units)
    coal_avail = ['Avail_HardCoal', 'Avail_HardCoal2OCGIn2024', 'Avail_Lignite']
    coal_gen = ['Gen_HardCoal', 'Gen_HardCoal2OCGIn2024', 'Gen_Lignite']
    if any(c in cols for c in coal_avail + coal_gen):
        avail_sum = sum((df[c].fillna(0) if c in cols else 0) for c in coal_avail)
        gen_sum = sum((df[c].fillna(0) if c in cols else 0) for c in coal_gen)
        computed['Missing_Vol_Coal'] = (avail_sum - gen_sum).clip(lower=0)
    else:
        computed['Missing_Vol_Coal'] = _na_result()

    # prefer CSV-provided missing capacities if available, else use computed values
    if isinstance(missing_volumes_df, pd.DataFrame):
        # mapping from CSV column names to target column names
        mapping = {
            'Missing_Cap_CCGT': 'Missing_Vol_CCGT',
            'Missing_Cap_OCGT': 'Missing_Vol_OCGT',
            'Missing_Cap_Oil': 'Missing_Vol_Oil',
            'Missing_Cap_Coal': 'Missing_Vol_Coal',
        }
        for src, tgt in mapping.items():
            if src in missing_volumes_df.columns:
                # assign aligned series (may contain NaN)
                df[tgt] = missing_volumes_df[src]
            else:
                df[tgt] = computed[tgt]
    else:
        # no CSV available: use computed (or NaN) values
        for k, v in computed.items():
            df[k] = v

    return df

if __name__ == "__main__":
    # input and output paths (relative to project root)
    csv_in = Path("data") / "df_full.csv"
    csv_out = Path("data") / "df_full_mc_ocgt_monthly.csv"

    if csv_in.exists():
        df = pd.read_csv(csv_in, parse_dates=["Time"], index_col="Time")
        df = add_marginal_cost_ccgt(df)
        # add monthly / rolling and also attach month-end and year-end means
        cols = ["MC_OCGT", "MC_Coal_Baseline", "MC_Oil_Peak","MC_CCGT"]
        for col in cols:
            df = add_monthly_and_rolling_averages(df, col, windows=(1, 12))

        df.index = pd.to_datetime(df.index)

        cols_year = ["MC_OCGT", "MC_Oil_Peak", "MC_Coal_Baseline","MC_CCGT"]
        for year in range(2020, 2025):
            df_year = df[df.index.year == year]
            for col in cols_year:

                mean_val = df_year[col].mean() if col in df.columns else np.nan
                print(f"{col} mean {year}: {mean_val:.2f}")
        df.to_csv(csv_out,float_format="%.2f")
    else:
        print(f"Warning: file `{csv_in}` not found.")