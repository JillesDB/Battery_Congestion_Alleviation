import sqlite3
import pandas as pd
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
DB_PATH   = Path.home() / ".open-MaStR/data/sqlite/open-mastr.db"
PPM_PATH  = Path.home() / "PycharmProjects/pypsa-eur/resources/powerplants_s_256.csv"
OUT_PATH  = Path.home() / "PycharmProjects/pypsa-eur/data/custom_powerplants.csv"

# ── Fuel mapping (NO HYDRO) ───────────────────────────────────────────────────
FUELTYPE_MAP = {
    "Kernenergie":        "Nuclear",
    "Steinkohle":         "Hard Coal",
    "Braunkohle":         "Lignite",
    "Erdgas":             "Natural Gas",
    "Mineralölprodukte":  "Oil",
    "Biomasse":           "Bioenergy",
    "Deponiegas":         "Bioenergy",
    "Klärgas":            "Bioenergy",
    "Grubengas":          "Natural Gas",
    "Wasserstoff":        "Hydrogen Storage",
    "AndereGase":         "Natural Gas",
}

# ── Technology classification ─────────────────────────────────────────────────
def classify_technology(row):
    """Classify plant technology from fuel label and capacity."""
    fuel = str(row.get("Fueltype", "")).strip().lower()
    cap = float(row.get("Capacity", 0.0) or 0.0)

    # Accept both normalized labels (after FUELTYPE_MAP) and potential raw labels.
    gas_labels = {
        "gas", "natural gas", "erdgas", "anderegase", "grubengas", "wasserstoff"
    }
    steam_labels = {
        "coal", "hard coal", "steinkohle",
        "lignite", "braunkohle",
        "biomass", "bioenergy", "biomasse", "deponiegas", "klärgas",
        "oil", "mineralölprodukte",
    }

    if fuel in gas_labels:
        return "OCGT" if cap <= 200.0 else "CCGT"
    if fuel in steam_labels:
        return "steam turbine"
    return "other"

# ── MaStR loading ─────────────────────────────────────────────────────────────
def load_table(conn, table_name, fuel_filter=None):
    cols = [
        "EinheitMastrNummer", "Nettonennleistung", "Bruttoleistung",
        "Laengengrad", "Breitengrad",
        "Inbetriebnahmedatum", "DatumEndgueltigeStilllegung",
        "Energietraeger",
    ]

    available = pd.read_sql(f"PRAGMA table_info({table_name})", conn)["name"].tolist()
    cols_to_use = [c for c in cols if c in available]

    df = pd.read_sql(f"SELECT {', '.join(cols_to_use)} FROM {table_name}", conn)

    if fuel_filter:
        df = df[df["Energietraeger"].isin(fuel_filter)]

    return df

def parse_dates(df):
    df["DateIn"] = pd.to_datetime(df["Inbetriebnahmedatum"], errors="coerce").dt.year
    df["DateOut"] = pd.to_datetime(df["DatumEndgueltigeStilllegung"], errors="coerce").dt.year
    return df

def filter_operational_2025(df):
    return df[
        (df["DateIn"].fillna(9999) < 2025) &
        (df["DateOut"].isna() | (df["DateOut"] > 2024))
        ].copy()

# ── MaStR → PyPSA ─────────────────────────────────────────────────────────────
def to_ppm_format(df):
    df["Fueltype"] = df["Energietraeger"].map(FUELTYPE_MAP)
    df = df.dropna(subset=["Fueltype"])

    cap_col = "Nettonennleistung" if "Nettonennleistung" in df.columns else "Bruttoleistung"
    df["Capacity"] = pd.to_numeric(df[cap_col], errors="coerce") / 1000.0

    df["lat"] = pd.to_numeric(df["Breitengrad"], errors="coerce")
    df["lon"] = pd.to_numeric(df["Laengengrad"], errors="coerce")

    df = df.dropna(subset=["Capacity", "lat", "lon"])
    df = df[(df["Capacity"] >= 2) & (df["Capacity"] <= 5000)]
    df = df[(df["lat"].between(45, 56)) & (df["lon"].between(5, 16))]

    df["Technology"] = df.apply(classify_technology, axis=1)
    df["Set"] = "PP"
    df["Country"] = "DE"

    df = df.rename(columns={"EinheitMastrNummer": "id"})
    df["id"] = "mastr_" + df["id"].astype(str)
    df["Name"] = df["id"]

    return df[[
        "id", "Name", "Fueltype", "Technology",
        "Set", "Country", "Capacity", "lat", "lon",
        "DateIn", "DateOut"
    ]]

# ── FIXED: Load hydro correctly ───────────────────────────────────────────────
def load_ppm_hydro(path):
    df = pd.read_csv(path)

    # STRICT: only DE hydro units
    hydro = df[(df["Country"] == "DE") & (df["Fueltype"] == "Hydro")].copy()

    # Clean
    hydro = hydro.dropna(subset=["Capacity", "lat", "lon"])
    hydro = hydro[hydro["Capacity"] > 1.0]

    # Canonicalize Technology names to values expected by pypsa-eur:
    #   Run-Of-River -> ror, Reservoir -> hydro, Pumped Storage -> PHS
    tech_raw = hydro["Technology"].fillna("").astype(str).str.strip().str.lower()

    hydro["Technology"] = "Reservoir"  # safe default for generic hydro
    hydro.loc[
        tech_raw.str.contains("pumped|pump|phs", regex=True, na=False),
        "Technology",
    ] = "Pumped Storage"
    hydro.loc[
        tech_raw.str.contains("run|river|ror", regex=True, na=False),
        "Technology",
    ] = "Run-Of-River"

    # Keep/repair Set column without forcing storage semantics
    if "Set" not in hydro.columns:
        hydro["Set"] = "PP"
    else:
        hydro["Set"] = hydro["Set"].fillna("PP")

    # IDs
    hydro["id"] = "ppm_" + hydro.index.astype(str)
    hydro["Name"] = hydro["id"]


    return hydro[[
        "id", "Name", "Fueltype", "Technology",
        "Set", "Country", "Capacity", "lat", "lon",
        "DateIn", "DateOut",
    ]]


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    conn = sqlite3.connect(DB_PATH)

    frames = []

    print("Loading MaStR combustion...")
    df = load_table(conn, "combustion_extended", list(FUELTYPE_MAP.keys()))
    df = parse_dates(df)
    df = filter_operational_2025(df)
    frames.append(to_ppm_format(df))

    print("Loading MaStR biomass...")
    df = load_table(conn, "biomass_extended")
    df["Energietraeger"] = "Biomasse"
    df = parse_dates(df)
    df = filter_operational_2025(df)
    frames.append(to_ppm_format(df))

    mastr = pd.concat(frames, ignore_index=True).drop_duplicates("id")
    print(f"MaStR plants: {len(mastr)}")

    print("Loading hydro from PPM...")
    hydro = load_ppm_hydro(PPM_PATH)
    print(f"Hydro plants: {len(hydro)}")

    result = pd.concat([mastr, hydro], ignore_index=True)

    print("\nFinal summary:")
    print(result.groupby("Fueltype")["Capacity"].agg(["count", "sum"]))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUT_PATH, index=False)

    print(f"\nSaved to {OUT_PATH}")

if __name__ == "__main__":
    main()