"""
export_gis.py  (TEMPLATE)
===========================
Writes the road/ward data out as a real GIS file, so it can be opened
directly in QGIS (or any other GIS tool) instead of just Excel. Run this
LAST, after estimate_residences_uprn.py and add_unnamed_roads.py, so the
export includes residence counts and the unnamed-road placeholder rows.

Every pipeline stage from run_pipeline.py onward keeps the road geometry
as WKT text in the 'road_geometry' column of the xlsx — this script is
just the first thing to turn it back into real geometry and write it out
in a GIS-native format.

Input (whichever of these exists, in priority order):
    <prefix>_Leafletting_residences_uprn.xlsx   (preferred — has residence
                                                  counts + unnamed roads)
    <prefix>_Leafletting_gapfilled.xlsx
    <prefix>_Leafletting.xlsx
Plus, if present:
    <prefix>_wards.geojson
    <prefix>_constituency.geojson

Output:
    --format gpkg (default): one <prefix>.gpkg with three layers
        (roads, wards, constituency)
    --format geojson: <prefix>_roads.geojson alongside the existing
        <prefix>_wards.geojson / <prefix>_constituency.geojson

Usage
-----
    python export_gis.py
    python export_gis.py --format geojson
"""

import argparse
from pathlib import Path

import geopandas as gpd

from constituency_config import get_config
from estimate_residences_uprn import parse_linestrings

_cfg = get_config()
_prefix = _cfg["output_prefix"]

CANDIDATE_INPUTS = [
    f"{_prefix}_Leafletting_residences_uprn.xlsx",
    f"{_prefix}_Leafletting_gapfilled.xlsx",
    f"{_prefix}_Leafletting.xlsx",
]
WARDS_GEOJSON = f"{_prefix}_wards.geojson"
CONSTITUENCY_GEOJSON = f"{_prefix}_constituency.geojson"

ROAD_COLUMNS = ["Street", "Ward", "Local Authority District", "Status", "Residences"]


def _pick_input():
    for name in CANDIDATE_INPUTS:
        if Path(name).exists():
            return name
    raise SystemExit(
        "No Leafletting xlsx found — run run_pipeline.py first.\n"
        f"Looked for: {', '.join(CANDIDATE_INPUTS)}"
    )


def _load_roads_gdf(xlsx_path):
    import pandas as pd

    df = pd.read_excel(xlsx_path, sheet_name="Data")
    geoms = [parse_linestrings(v) for v in df.get("road_geometry", [])]
    gdf = gpd.GeoDataFrame(df, geometry=geoms, crs="EPSG:4326")

    n_missing = gdf.geometry.isna().sum()
    if n_missing:
        print(f"  {n_missing} row(s) with no usable geometry — dropped")
    gdf = gdf[gdf.geometry.notna()].copy()

    keep = [c for c in ROAD_COLUMNS if c in gdf.columns] + ["geometry"]
    return gdf[keep]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=["gpkg", "geojson"], default="gpkg")
    args = parser.parse_args()

    xlsx_path = _pick_input()
    print(f"Reading roads from {xlsx_path} …")
    roads_gdf = _load_roads_gdf(xlsx_path)
    print(f"  {len(roads_gdf)} roads with geometry")

    wards_gdf = gpd.read_file(WARDS_GEOJSON) if Path(WARDS_GEOJSON).exists() else None
    const_gdf = gpd.read_file(CONSTITUENCY_GEOJSON) if Path(CONSTITUENCY_GEOJSON).exists() else None

    if args.format == "gpkg":
        out_path = f"{_prefix}.gpkg"
        roads_gdf.to_file(out_path, layer="roads", driver="GPKG")
        print(f"Wrote layer 'roads' -> {out_path}")
        if wards_gdf is not None:
            wards_gdf.to_file(out_path, layer="wards", driver="GPKG")
            print(f"Wrote layer 'wards' -> {out_path}")
        if const_gdf is not None:
            const_gdf.to_file(out_path, layer="constituency", driver="GPKG")
            print(f"Wrote layer 'constituency' -> {out_path}")
        print(f"\nOpen {out_path} in QGIS: Layer > Add Layer > Add Vector Layer.")
    else:
        out_path = f"{_prefix}_roads.geojson"
        roads_gdf.to_file(out_path, driver="GeoJSON")
        print(f"Wrote {out_path}")
        if not Path(WARDS_GEOJSON).exists():
            print(f"  (no {WARDS_GEOJSON} to accompany it)")
        if not Path(CONSTITUENCY_GEOJSON).exists():
            print(f"  (no {CONSTITUENCY_GEOJSON} to accompany it)")


if __name__ == "__main__":
    main()
