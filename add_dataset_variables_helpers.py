import pandas as pd
import glob
from pathlib import Path
import add_analysis_variables
import os
import shutil
import datetime
import pandas as pd
import pandas as pd
import glob
import os





def update_renewables_in_targets(renewables_file: str,
                                 target_files: list,
                                 renewable_cols: list | None = None,
                                 float_format: str = "%.2f") -> list:
    """
    Load authoritative renewables from `renewables_file` and copy values into each file
    listed in `target_files`. Returns the list of updated file paths.
    """
    if renewable_cols is None:
        renewable_cols = ["Solar", "Wind Onshore", "Total Wind"]

    df_ren = pd.read_csv(renewables_file, parse_dates=["Time_CET"]).set_index("Time_CET")

    updated_files = []
    for file in target_files:
        print(f"Processing {file}...")
        df = pd.read_csv(file, parse_dates=["Time_CET"]).set_index("Time_CET")

        for col in renewable_cols:
            if col not in df.columns:
                df[col] = pd.NA

        common_index = df.index.intersection(df_ren.index)

        for col in renewable_cols:
            df.loc[common_index, col] = df_ren.loc[common_index, col]

        df = add_analysis_variables.add_residual_load(df)

        print(df.head())
        df.to_csv(file, index=True, float_format=float_format)

        print(f"Updated renewable values written to {file}")
        updated_files.append(file)

    print("All files processed successfully.")
    return updated_files



def process_dataset_to_cet(input_csv_path, output_csv_path=None):


    base, ext = os.path.splitext(input_csv_path)
    if not output_csv_path:
        output_csv_path = f"{base}_with_cet{ext}"

    input_abs = os.path.abspath(input_csv_path)
    output_abs = os.path.abspath(output_csv_path)
    print(f"Using output path: `{output_csv_path}`")

    if input_abs == output_abs:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_csv_path = f"{base}_with_cet_{timestamp}{ext}"
        output_abs = os.path.abspath(output_csv_path)
        print(f"Output path equals input. Using new output: `{output_csv_path}`")

    # =========================
    # Load dataset
    # =========================
    df = pd.read_csv(input_csv_path)

    # =========================
    # Parse Time column as UTC
    # =========================
    df = df.copy()
    if "Time" in df.columns:
        df["Time"] = pd.to_datetime(df["Time"], format="%d-%m-%Y %H:%M", utc=True)
    else:
        df = df.reset_index()
        idx_col = df.columns[0]
        if idx_col != "Time":
            df.rename(columns={idx_col: "Time"}, inplace=True)
        df["Time"] = pd.to_datetime(df["Time"], utc=True)

    # =========================
    # Convert UTC -> CET/CEST
    # =========================
    df["Time_CET"] = (
        df["Time"]
        .dt.tz_convert("Europe/Berlin")   # handles CET / CEST automatically
        .dt.tz_localize(None)             # drop timezone info for clean CSV output
    )

    # =========================
    # Shift specified columns one up
    # =========================
    shift_cols = [
        "Brent Crude Oil[EUR/Barrel]",
        "Carbon Permits [EUR/tCO2]",
        "TTF NG [EUR/MWh]",
        "Amsterdam Coal Futures [USD/Ton]",
        "MC_CCGT",
        "MC_OCGT",
        "MC_Oil_Peak",
        "MC_Coal_Baseline",
    ]
    existing_shift_cols = [c for c in shift_cols if c in df.columns]
    if existing_shift_cols:
        df.loc[:, existing_shift_cols] = df.loc[:, existing_shift_cols].shift(-1)

    # =========================
    # Delete Physical Export column if present
    # =========================
    if "Physical Export" in df.columns:
        df.drop(columns=["Physical Export"], inplace=True)

    # =========================
    # Set Time_CET as the index (replace current index)
    # =========================
    if "Time_CET" in df.columns:
        df["Time_CET"] = pd.to_datetime(df["Time_CET"])
        if not df.empty:
            first_row = df.iloc[0].copy()
            src_ts = first_row["Time_CET"]
            dst_ts = src_ts - pd.Timedelta(hours=1)
            if not (df["Time_CET"] == dst_ts).any():
                new_row = first_row.copy()
                new_row["Time_CET"] = dst_ts
                df = pd.concat([pd.DataFrame([new_row]), df], ignore_index=True, sort=False)
        if "Time" in df.columns:
            df.drop(columns=["Time"], inplace=True)
        df.set_index("Time_CET", inplace=True, drop=True)

    # Delete last row before saving
    if not df.empty:
        df = df.iloc[:-1]

    # As an extra safety, if paths still match, back up the original before saving
    if os.path.abspath(output_csv_path) == input_abs:
        backup = f"{input_csv_path}.bak_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(input_csv_path, backup)
        print(f"Backed up original to: `{backup}`")

    df.to_csv(output_csv_path, index=True)

    print("CET time column added as index; specified columns shifted; `Physical Export` removed (if present).")
    print(f"Output saved to: `{output_csv_path}`")

# ======================================================
# Paths
# ======================================================
DATA_DIR = (
    r"C:\Users\jille\OneDrive - Danmarks Tekniske Universitet"
    r"\Laptop Folder\DTU - PhD Work\EntsoE Driver Data\belgium\load"
)

OUTPUT_PATH = "belgium_hourly_load_forecast_2018_2024.csv"

# ======================================================
# Function: process load forecast files
# ======================================================
def process_load_files(file_pattern):
    files = glob.glob(os.path.join(DATA_DIR, file_pattern))
    if not files:
        print(f"No files found for pattern `{file_pattern}` in `{DATA_DIR}`")
        return pd.DataFrame(columns=["Load Forecast"])

    dfs = []
    for file in files:
        df = pd.read_csv(file)

        mtu_col = "MTU (CET/CEST)"
        if mtu_col not in df.columns:
            print(f"Skipping `{file}`: missing column `{mtu_col}`")
            continue

        # Extract start of MTU interval and remove trailing timezone markers like " (CET)"
        times = (
            df[mtu_col]
            .astype(str)
            .str.split(" - ")
            .str[0]
            .str.replace(r"\s*\(.*\)$", "", regex=True)
            .str.strip()
        )

        # Parse with dayfirst; be forgiving for minor format variations
        df["Time_CET"] = pd.to_datetime(times, dayfirst=True, errors="coerce")

        # Drop rows that failed to parse
        df = df.dropna(subset=["Time_CET"])

        df = df[["Time_CET", "Day-ahead Total Load Forecast (MW)"]].copy()
        df["Day-ahead Total Load Forecast (MW)"] = pd.to_numeric(
            df["Day-ahead Total Load Forecast (MW)"], errors="coerce"
        )

        dfs.append(df)

    if not dfs:
        print("No valid data frames collected; returning empty result.")
        return pd.DataFrame(columns=["Load Forecast"])

    df_all = pd.concat(dfs, ignore_index=True)
    df_all = df_all.set_index("Time_CET").sort_index()

    # Quarter-hourly → hourly average
    df_hourly = (
        df_all
        .resample("H")
        .mean()
        .rename(columns={
            "Day-ahead Total Load Forecast (MW)": "Load Forecast"
        })
    )

    df_hourly.index.name = "Time_CET"
    return df_hourly

# ======================================================
# Build hourly load forecast
# ======================================================
# df_load = process_load_files("GUI_TOTAL_LOAD_DAYAHEAD_*.csv")
#
# if df_load.empty:
#     print("No hourly load forecast created (empty).")
# else:
#     # Optional: restrict range if needed
#     df_load = df_load.loc["2018-01-01 00:00:00":]
#
#     # ======================================================
#     # Save
#     # ======================================================
#     df_load.to_csv(OUTPUT_PATH)
#     print("Hourly load forecast dataset created successfully.")
#     print(f"Saved to: `{OUTPUT_PATH}`")

def update_load_data():
    LOAD_FILE = "belgium_hourly_load_forecast_2018_2024.csv"
    TARGET_FILES = [
        "Dataset_belgium_fossil_fuels_corrected_prices_with_cet.csv",
        "Dataset_belgium_full_with_cet.csv",
        "Dataset_belgium_thorsten_with_cet.csv",
    ]

    # ======================================================
    # Load authoritative load data
    # ======================================================
    if os.path.exists(LOAD_FILE):
        df_load = pd.read_csv(LOAD_FILE, parse_dates=["Time_CET"]).set_index("Time_CET")
    else:
        print(f"Load file `{LOAD_FILE}` not found; skipping target updates.")
        df_load = pd.DataFrame(columns=["Load Forecast"])

    updated_files = []

    # ======================================================
    # Update target files
    # ======================================================
    for file in TARGET_FILES:
        print(f"Updating Load Forecast in `{file}`...")

        if not os.path.exists(file):
            print(f"Target file `{file}` not found; skipping.")
            continue

        df = pd.read_csv(file, parse_dates=["Time_CET"]).set_index("Time_CET")
        print(df.head())
        if "Load Forecast" not in df.columns:
            df["Load Forecast"] = pd.NA

        common_index = df.index.intersection(df_load.index)
        if not common_index.empty:
            df.loc[common_index, "Load Forecast"] = df_load.loc[common_index, "Load Forecast"]

        df = add_analysis_variables.add_residual_load(df)
        print(df.head())
        df.to_csv(file, index=True, float_format="%.2f")

        print(f"Load Forecast updated in `{file}`")
        updated_files.append(file)

    print("All files updated successfully.")
    return updated_files

# preserve previous behavior by invoking the function


TARGET_FILES = [
    "Dataset_belgium_fossil_fuels_corrected_prices_with_cet.csv",
    "Dataset_denmark 1_fossil_fuels_corrected_prices_with_cet.csv",
    "Dataset_germany_fossil_fuels_harshit_with_cet.csv",
]

TARGET_COLUMNS = [
    "Brent Crude Oil[EUR/Barrel]",
    "Carbon Permits [EUR/tCO2]",
    "TTF NG [EUR/MWh]",
    "Amsterdam Coal Futures [USD/Ton]",
]

for file in TARGET_FILES:
    if not os.path.exists(file):
        print(f"Target file `{file}` not found; skipping.")
        continue

    print(f"Processing {file}")

    df = pd.read_csv(file, parse_dates=["Time_CET"]).set_index("Time_CET")

    # Ensure we only touch columns that actually exist
    cols = [c for c in TARGET_COLUMNS if c in df.columns]
    if not cols:
        print(f"  No target columns found in {file}; skipping.")
        continue

    # Group by calendar day
    for date, daily_df in df.groupby(df.index.date):
        ts_0500 = pd.Timestamp(date) + pd.Timedelta(hours=5)

        if ts_0500 not in daily_df.index:
            # No 05:00 value → leave this day unchanged
            continue

        ref_values = daily_df.loc[ts_0500, cols]

        # Assign the 05:00 value to all hours of that day
        df.loc[daily_df.index, cols] = ref_values.values

    # df = add_analysis_variables.add_marginal_cost_ccgt(df)
    # df = add_analysis_variables.add_marginal_cost_oil_peak(df)
    # df = add_analysis_variables.add_marginal_cost_coal_baseline(df)
    # df = add_analysis_variables.add_marginal_cost_ocgt(df)
    # df = add_analysis_variables.add_marginal_cost_monthly_and_rolling(df)
    # Write back exactly the same file (same name, same structure)
    df.reset_index().to_csv(file, index=False,float_format="%.2f")

    print(f"  Updated and overwritten {file}")
