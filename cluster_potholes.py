"""
cluster_potholes.py  (Stafford)
=================================
Batch job for the Pothole Watch app (../Pothole App/). Pulls the Reports
sheet's published CSV, snaps each report to Stafford's existing road
network, groups nearby reports on the same road into repair "clusters"
(sections), and ranks clusters by priority.

WHY THIS EXISTS: the app lets residents report individual potholes at a
GPS point. Fixing each one separately means repeatedly closing the same
stretch of road. This groups reports into sections a council can fix in
one visit, and ranks which section to prioritise first.

Reuses the exact road-snapping technique from estimate_residences_uprn.py
(project to EPSG:27700, buffer roads, gpd.sjoin, sindex.nearest for
orphans) — same reasoning applies: it doesn't depend on OSM tagging
quality, just the road centreline geometry already extracted by
run_pipeline.py.

Follows this repo's existing convention (see WORKFLOW.md,
merge_into_google_sheet.py): produces local output files for a human to
paste into the live Sheet, rather than writing to Google Sheets directly.
Automating that round trip via gspread is a noted follow-up, not built here.

Requirements: geopandas, pandas, shapely, pyproj, requests

Inputs:
    REPORTS_CSV_URL (or --input <path/url>) — the Reports sheet's
        published CSV (../Pothole App/index.html's SHEET_ID/REPORTS_GID)
    Stafford_Leafletting_residences_uprn.xlsx — road_geometry/Street/Ward/
        Residences, from the existing leafletting pipeline

Outputs:
    pothole_clusters_output.csv       — one row per cluster, ranked
    pothole_reports_with_clusters.csv — every report + its road_name/ward/cluster_id
"""

import argparse
import math
import re
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString, MultiLineString, Point

# ── Config ───────────────────────────────────────────────────────────────
# Fill in once the Pothole Watch Google Sheet exists and is published to
# the web (File -> Share -> Publish to web -> the Reports sheet, CSV) —
# same publishing step the leafletting map's SHEET_CSV_URL relies on.
REPORTS_CSV_URL = "REPLACE_WITH_PUBLISHED_REPORTS_SHEET_CSV_URL"

ROAD_XLSX = "Stafford_Leafletting_residences_uprn.xlsx"

SNAP_BUFFER_METRES = 15       # tighter than the 40m UPRN buffer — a pothole IS the road, not near a building
CLUSTER_DISTANCE_METRES = 120  # reports on the same road within this distance are one repair section

OUTPUT_CLUSTERS = "pothole_clusters_output.csv"
OUTPUT_REPORTS  = "pothole_reports_with_clusters.csv"


# ── Prioritisation factors — generic and pluggable ──────────────────────
# See ../Pothole App's plan for the research behind this: UK highway
# authorities prioritise repairs via a risk-based approach (DfT
# "Well-Managed Highway Infrastructure" Code of Practice) combining defect
# severity/impact with probability of harm — which scales with traffic/
# pedestrian volume and a road's position in the network hierarchy
# (Strategic Route > Main Distributor > Secondary Distributor > Link Road >
# Local Access Road). None of that council-specific data (road hierarchy,
# DfT traffic counts, inspected defect depth, budget banding) exists yet —
# only citizen reports and the Residences-per-road figure the leafletting
# pipeline already computes. So scoring is a small FACTOR REGISTRY, not a
# fixed formula: each factor is skipped (weight redistributed across the
# rest) if its column isn't present for a given run, so this script keeps
# working today and gains new inputs later just by adding a column + a
# config line here — no logic rewrite needed.
#
# "normalise" - how a factor's raw values are scaled to 0-1 before weighting:
#   "minmax" - (v - min) / (max - min) across this run's clusters
#   "log"    - minmax applied to log1p(v) — for skewed counts like traffic volume
PRIORITY_FACTORS = {
    # Available now, computed by this script from the reports/road data:
    "report_count":       {"weight": 1.0, "normalise": "minmax", "optional": False},
    "worst_severity":     {"weight": 1.0, "normalise": "minmax", "optional": False},
    "oldest_report_days": {"weight": 0.5, "normalise": "minmax", "optional": False},
    "residences":         {"weight": 1.0, "normalise": "minmax", "optional": False},
    # Optional — only applied if a cluster DataFrame column of this name
    # exists when score_clusters() is called. None of these are populated
    # by this script today; wire one up by joining the column in before
    # calling score_clusters(), e.g. from a council-supplied road hierarchy
    # table or DfT AADT traffic-count data, keyed by road name/ward.
    "road_hierarchy_rank": {"weight": 1.5, "normalise": "minmax", "optional": True},  # Strategic..Local Access, higher = more strategic
    "aadt_traffic_volume": {"weight": 1.0, "normalise": "log",    "optional": True},  # DfT annual average daily traffic count
    "defect_depth_mm":     {"weight": 1.0, "normalise": "minmax", "optional": True},  # inspector-measured, vs the 40mm investigatory level most UK councils use
    "budget_band":         {"weight": 1.0, "normalise": "minmax", "optional": True},  # council-supplied, e.g. remaining budget for that ward/route this year
}

SEVERITY_RANK = {"Minor": 1, "Moderate": 2, "Severe": 3, "Hazardous": 4, "Unknown": 0}


def parse_linestrings(geom_str):
    """Same parser as estimate_residences_uprn.py — road_geometry is one or
    more '|'-separated WKT LINESTRINGs."""
    if pd.isna(geom_str) or not str(geom_str).strip():
        return None
    segments = []
    for part in str(geom_str).split("|"):
        m = re.match(r"LINESTRING\((.+)\)", part.strip())
        if not m:
            continue
        coords = []
        for pair in m.group(1).split(","):
            pts = pair.strip().split()
            if len(pts) == 2:
                try:
                    coords.append((float(pts[0]), float(pts[1])))
                except ValueError:
                    continue
        if len(coords) >= 2:
            segments.append(LineString(coords))
    if not segments:
        return None
    return segments[0] if len(segments) == 1 else MultiLineString(segments)


def load_reports(source):
    print(f"Step 1: Loading reports from '{source}'...")
    df = pd.read_csv(source)
    before = len(df)
    df = df.dropna(subset=["lat", "lon"])
    df = df[(df["lat"] != "") & (df["lon"] != "")]
    df["lat"] = df["lat"].astype(float)
    df["lon"] = df["lon"].astype(float)
    print(f"  {len(df)} reports with a valid location (of {before} rows)")
    if "status" in df.columns:
        # Fixed reports don't need a repair section planned for them.
        active = df[df["status"] != "Fixed"].copy()
        print(f"  {len(active)} still active (excluding {len(df) - len(active)} already Fixed)")
        return active
    return df


def load_roads():
    print(f"\nStep 2: Loading road network from '{ROAD_XLSX}'...")
    df = pd.read_excel(ROAD_XLSX, sheet_name="Data")
    geoms = [parse_linestrings(row.get("road_geometry")) for _, row in df.iterrows()]
    roads_gdf = gpd.GeoDataFrame(df.copy(), geometry=geoms, crs="EPSG:4326")
    roads_valid = roads_gdf[roads_gdf.geometry.notna()].copy()
    print(f"  {len(roads_valid)} roads with geometry (of {len(df)} total)")
    return roads_valid


def snap_reports_to_roads(reports_df, roads_valid):
    """Assigns each report a road_idx (row position in roads_valid) using
    the same buffer+sjoin+nearest-neighbour-fallback technique as
    estimate_residences_uprn.py's UPRN-to-road matching."""
    print(f"\nStep 3: Snapping reports to roads (buffer={SNAP_BUFFER_METRES}m)...")
    reports_gdf = gpd.GeoDataFrame(
        reports_df.copy(),
        geometry=gpd.points_from_xy(reports_df["lon"], reports_df["lat"]),
        crs="EPSG:4326",
    ).reset_index(drop=True)

    roads_proj = roads_valid.reset_index(drop=True).to_crs("EPSG:27700")
    reports_proj = reports_gdf.to_crs("EPSG:27700")

    roads_buffered = roads_proj.copy()
    roads_buffered["geometry"] = roads_proj.geometry.buffer(SNAP_BUFFER_METRES)

    joined = gpd.sjoin(
        reports_proj[["geometry"]],
        roads_buffered[["geometry"]].reset_index().rename(columns={"index": "road_idx"}),
        how="left",
        predicate="within",
    )

    multi_matched = joined[joined.index.duplicated(keep=False) & joined["road_idx"].notna()]
    if len(multi_matched):
        print(f"  {multi_matched.index.nunique()} reports matched multiple roads — resolving to nearest...")
        resolved = {}
        for report_i, group in multi_matched.groupby(level=0):
            pt = reports_proj.loc[report_i, "geometry"]
            best_road = min(group["road_idx"], key=lambda ri: pt.distance(roads_proj.loc[ri, "geometry"]))
            resolved[report_i] = best_road
        joined = joined[~joined.index.duplicated(keep="first")].copy()
        for report_i, road_idx in resolved.items():
            joined.loc[report_i, "road_idx"] = road_idx

    orphans = joined[joined["road_idx"].isna()]
    if len(orphans):
        print(f"  {len(orphans)} reports with no road within {SNAP_BUFFER_METRES}m — assigning to nearest road...")
        sindex = roads_proj.sindex
        for report_i in orphans.index:
            pt = reports_proj.loc[report_i, "geometry"]
            result = sindex.nearest(pt, return_all=False)
            if result.shape[1] > 0:
                joined.loc[report_i, "road_idx"] = roads_proj.index[result[1][0]]

    reports_gdf["road_idx"] = joined["road_idx"].reindex(reports_gdf.index).astype("Int64")
    reports_gdf["_proj_geom"] = reports_proj.geometry.values  # kept for cluster distance calc below
    # Written into the Reports sheet's own 'road_name'/'ward' columns (not
    # new ones) so pothole_reports_with_clusters.csv pastes straight into
    # the live sheet without a column-rename step.
    reports_gdf["road_name"] = reports_gdf["road_idx"].map(roads_valid["Street"])
    reports_gdf["ward"] = reports_gdf["road_idx"].map(roads_valid["Ward"])
    reports_gdf["road_residences"] = reports_gdf["road_idx"].map(
        pd.to_numeric(roads_valid["Residences"], errors="coerce")
    ).fillna(0)

    print(f"  Matched {reports_gdf['road_idx'].notna().sum()} of {len(reports_gdf)} reports to a road")
    return reports_gdf


def cluster_reports(reports_gdf):
    """Union-find over reports sharing a road_idx: any two reports within
    CLUSTER_DISTANCE_METRES (in projected metres) join the same cluster,
    transitively — so a chain of reports spaced closer than the threshold
    all along one road still merges into a single section, not one cluster
    per pair."""
    print(f"\nStep 4: Clustering reports (same road, within {CLUSTER_DISTANCE_METRES}m)...")
    parent = {}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    matched = reports_gdf[reports_gdf["road_idx"].notna()]
    for i in matched.index:
        parent[i] = i

    for road_idx, group in matched.groupby("road_idx"):
        idxs = list(group.index)
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                pa, pb = reports_gdf.loc[idxs[a], "_proj_geom"], reports_gdf.loc[idxs[b], "_proj_geom"]
                if pa.distance(pb) <= CLUSTER_DISTANCE_METRES:
                    union(idxs[a], idxs[b])

    reports_gdf["cluster_key"] = pd.NA
    for i in matched.index:
        reports_gdf.loc[i, "cluster_key"] = find(i)

    n_clusters = reports_gdf["cluster_key"].nunique()
    print(f"  {len(matched)} matched reports grouped into {n_clusters} clusters")
    return reports_gdf


def _normalise(series, method):
    s = series.astype(float)
    if method == "log":
        s = np.log1p(s.clip(lower=0))
    lo, hi = s.min(), s.max()
    if hi <= lo:
        return pd.Series(0.5, index=s.index)  # everything tied — neutral mid-score, not an arbitrary winner
    return (s - lo) / (hi - lo)


def score_clusters(clusters_df):
    """Applies PRIORITY_FACTORS: each present factor is min-max (or log)
    normalised across this run's clusters, then combined by weight. A
    factor whose column isn't present in clusters_df is skipped and its
    weight is simply not part of the sum — so scores from runs with
    different available factors aren't on a directly comparable scale, but
    the RANKING within one run is always meaningful."""
    print("\nStep 5: Scoring clusters...")
    used = []
    weighted_sum = pd.Series(0.0, index=clusters_df.index)
    total_weight = 0.0
    for name, spec in PRIORITY_FACTORS.items():
        if name not in clusters_df.columns:
            if not spec.get("optional", True):
                raise KeyError(f"Required priority factor column '{name}' missing from clusters_df")
            continue
        weighted_sum = weighted_sum + _normalise(clusters_df[name], spec["normalise"]) * spec["weight"]
        total_weight += spec["weight"]
        used.append(name)
    print(f"  Factors used: {', '.join(used)}")
    clusters_df["priority_score"] = (weighted_sum / total_weight) if total_weight else 0.0
    clusters_df = clusters_df.sort_values("priority_score", ascending=False).reset_index(drop=True)
    clusters_df["priority_rank"] = clusters_df.index + 1
    return clusters_df


def build_clusters_table(reports_gdf):
    matched = reports_gdf[reports_gdf["cluster_key"].notna()].copy()
    now = datetime.now(timezone.utc)
    rows = []
    for cluster_key, group in matched.groupby("cluster_key"):
        timestamps = pd.to_datetime(group["timestamp"], errors="coerce", utc=True)
        oldest_days = (now - timestamps.min()).days if timestamps.notna().any() else 0
        severities = group["severity"].map(lambda s: SEVERITY_RANK.get(s, 0)) if "severity" in group else pd.Series([0])
        rows.append({
            "cluster_id": f"c{int(cluster_key)}",
            "road_name": group["road_name"].mode().iat[0] if not group["road_name"].mode().empty else "",
            "ward": group["ward"].mode().iat[0] if not group["ward"].mode().empty else "",
            "centroid_lat": group["lat"].mean(),
            "centroid_lon": group["lon"].mean(),
            "report_count": len(group),
            "worst_severity": severities.max(),
            "oldest_report_days": oldest_days,
            "residences": group["road_residences"].iloc[0],
            "status": "Under_Review",
            "planned_date": "",
            "notes": "",
        })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=REPORTS_CSV_URL, help="Reports CSV path or URL (default: the configured published Sheet URL)")
    args = parser.parse_args()

    if args.input == REPORTS_CSV_URL and "REPLACE_WITH" in REPORTS_CSV_URL:
        sys.exit(
            "REPORTS_CSV_URL isn't configured yet — either edit that constant at the top "
            "of this file once the Pothole Watch Sheet is published, or pass --input "
            "<path-or-url> to test against a local CSV."
        )

    reports_df = load_reports(args.input)
    if reports_df.empty:
        print("No active reports to process.")
        return

    roads_valid = load_roads()
    reports_gdf = snap_reports_to_roads(reports_df, roads_valid)
    reports_gdf = cluster_reports(reports_gdf)

    clusters_df = build_clusters_table(reports_gdf)
    if clusters_df.empty:
        print("No clusters formed (no reports matched a road).")
        return
    clusters_df = score_clusters(clusters_df)

    # Map each report's cluster_key to the same "c{key}" id build_clusters_table
    # used, so the per-report output carries the same section identity as the
    # clusters table (the id is a pure function of cluster_key, so no lookup
    # into clusters_df is needed to keep the two in sync).
    reports_gdf["cluster_id"] = reports_gdf["cluster_key"].map(lambda k: f"c{int(k)}" if pd.notna(k) else "")

    reports_out = reports_gdf.drop(columns=["geometry", "_proj_geom", "road_idx", "cluster_key"], errors="ignore")
    reports_out.to_csv(OUTPUT_REPORTS, index=False)
    clusters_df.to_csv(OUTPUT_CLUSTERS, index=False)

    print(f"\nDone.")
    print(f"  {OUTPUT_CLUSTERS} — {len(clusters_df)} clusters, ranked by priority")
    print(f"  {OUTPUT_REPORTS} — {len(reports_out)} reports with road_name/ward/cluster_id filled in")
    print("  Paste these into the live Reports/Clusters sheets (see WORKFLOW.md's")
    print("  'produce a file, human applies it' convention) — this script never")
    print("  writes to Google Sheets directly.")
    print(f"\n  Top 5 priority sections:")
    print(clusters_df[["priority_rank", "road_name", "ward", "report_count", "priority_score"]].head(5).to_string(index=False))


if __name__ == "__main__":
    main()
