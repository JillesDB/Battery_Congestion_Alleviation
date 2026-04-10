# shell_scripts/create_correct_stubs.py
"""
Builds correctly-structured availability matrix stubs by reading
the actual cutout grid coordinates and region index from geojson.
Output matches exactly what atlite.availabilitymatrix() produces.
"""

import xarray as xr
import numpy as np
import geopandas as gpd
import atlite
from pathlib import Path

PYPSA_DIR   = Path("/zhome/26/e/209460/PycharmProjects/pypsa-eur")
CUTOUT_PATH = PYPSA_DIR / "data/cutout/archive/v1.0/europe-2013-sarah3-era5.nc"
CLUSTERS    = [256]
TECHS = {
    "onwind":       "onshore",
    "offwind-ac":   "offshore",
    "offwind-dc":   "offshore",
    "offwind-float":"offshore",
    "solar":        "onshore",
    "solar-hsat":   "onshore",
}

def make_stub(regions_path: Path, cutout_path: Path, out_path: Path, is_offshore: bool):
    if out_path.exists():
        print(f"  EXISTS — skip: {out_path.name}")
        return

    regions = gpd.read_file(regions_path)
    cutout  = atlite.Cutout(cutout_path)

    grid = cutout.grid
    xs   = np.sort(grid["x"].unique())
    ys   = np.sort(grid["y"].unique())[::-1]

    data = np.ones((len(regions), len(ys), len(xs)), dtype=np.float32)

    # Build coords dict
    # 'name' dim uses index; for offshore, also add 'bus' coord
    # The regions GeoJSON index IS the bus name in PyPSA-Eur
    name_values = regions.index.astype(str).values

    coords = {
        "name": name_values,
        "y":    ys,
        "x":    xs,
    }

    # offshore regions have a 'bus' coordinate = same as 'name'
    # (build_renewable_profiles reads availability.coords["bus"] for offshore)
    if is_offshore:
        coords["bus"] = ("name", name_values)

    da = xr.DataArray(
        data,
        dims=["name", "y", "x"],
        coords=coords,
        attrs={
            "units": "1",
            "note":  "uniform stub — no land exclusion applied",
        }
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    da.to_netcdf(out_path)
    print(f"  Written: {out_path.name}  shape={data.shape}  offshore={is_offshore}")

if __name__ == "__main__":
    for clusters in CLUSTERS:
        for tech, side in TECHS.items():
            is_offshore  = (side == "offshore")
            regions_path = PYPSA_DIR / f"resources/regions_{side}_base_s_{clusters}.geojson"
            out_path     = PYPSA_DIR / f"resources/availability_matrix_{clusters}_{tech}.nc"

            if not regions_path.exists():
                print(f"  MISSING regions — skip: {regions_path.name}")
                continue

            # Force regeneration to pick up bus coord fix
            if out_path.exists():
                out_path.unlink()
                print(f"  Removed stale stub: {out_path.name}")

            print(f"  Building: {clusters} / {tech}")
            make_stub(regions_path, CUTOUT_PATH, out_path, is_offshore)

    print("\nAll stubs written.")

if __name__ == "__main__":
    for clusters in CLUSTERS:
        for tech, side in TECHS.items():
            regions_path = PYPSA_DIR / f"resources/regions_{side}_base_s_{clusters}.geojson"
            out_path     = PYPSA_DIR / f"resources/availability_matrix_{clusters}_{tech}.nc"

            if not regions_path.exists():
                print(f"  MISSING regions — skip: {regions_path.name}")
                continue

            print(f"  {clusters} / {tech}")
            make_stub(regions_path, CUTOUT_PATH, out_path)

    print("\nDone.")