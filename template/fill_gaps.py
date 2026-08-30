"""
fill_gaps.py
============
Extends run_pipeline.py's output to catch three classes of roads the plain
`way[highway][name]` Overpass query + name-string grouping misses:

  1. "Missing sections" — a real, already-named road that OSM has split into
     several `way` segments at junctions, where one or more of those segments
     lost the `name` tag along the way. The un-named segment(s) never match
     `[name]` so they're invisible to run_pipeline.py, leaving a visible gap
     in an otherwise-tracked road. Fixed here by node-ID adjacency: if an
     unnamed way's endpoint node is shared with exactly one already-named
     way, that unnamed segment is absorbed into that road's geometry before
     ward-clipping runs.

  2. "Missing roads" — entire residential/unclassified streets that have no
     `name` tag in OSM at all (common for small rural estates). These can't
     be safely auto-named (no addr:street data reliably available), so they
     are written to unnamed_roads_report.csv with a Google Maps link, ward,
     and OSM way id for manual review/naming rather than silently guessed.

  3. "Roads that vanish because of a same-name collision elsewhere in the
     county" — run_pipeline.py (and the original archived pipeline) groups
     ALL OSM ways sharing an exact name string across the whole bbox into
     one `unary_union`'d geometry before computing ward ratios. Generic
     rural lane names (Chapel Lane, Park Lane, Manor Road, Pinfold Lane,
     Mount Pleasant, Tunstall Lane, ...) recur as several totally
     unconnected physical roads in different villages/wards. Summing their
     lengths together inflates the denominator MIN_FRAGMENT_RATIO /
     DOMINANT_WARD_THRESHOLD are computed against, so a real village's
     instance of "Chapel Lane" can fall under the 2% noise threshold (or
     get absorbed as a false "boundary artefact") relative to a much
     longer same-named road in a different ward — silently dropping an
     entire real street, or moving which ward it's attributed to between
     runs depending on which instance happens to be longer. Fixed here by
     clustering each name's ways into connected components (shared OSM
     node IDs) BEFORE computing lengths/ratios, so each physically distinct
     road is ward-assigned independently; same-name clusters that land in
     the same ward are merged back into one row afterwards.

Inputs (already produced by run_pipeline.py in this folder):
    <prefix>_wards.geojson
    <prefix>_constituency.geojson

Additional input (fetched by this script if missing):
    all_highways_raw.json   — way[highway](bbox), i.e. named AND unnamed

Outputs:
    <prefix>_Leafletting_gapfilled.xlsx   (same schema as run_pipeline.py's output)
    unnamed_roads_report.csv
"""

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from constituency_config import get_config
from run_pipeline import (
    _geometry_to_text,
    _representative_latlon,
    _is_boundary_artefact,
    DOMINANT_WARD_THRESHOLD,
    MIN_FRAGMENT_RATIO,
    BOUNDARY_ROAD_SECONDARY_MAX,
)

_cfg = get_config()
_prefix = _cfg["output_prefix"]

ALL_HIGHWAYS_JSON = "all_highways_raw.json"
WARDS_GEOJSON     = f"{_prefix}_wards.geojson"
OUTPUT_XLSX       = f"{_prefix}_Leafletting_gapfilled.xlsx"
OVERPASS_URL      = "https://overpass-api.de/api/interpreter"

# ward display name (normalised) -> district label, from constituency_config.py
_DISTRICT_BY_WARD = {name.strip().lower(): district for name, district in _cfg["wards"]}

# Highway types worth reporting as a standalone "needs a name" street.
# Excludes footway/path/track/steps/cycleway/bridleway/service/motorway/
# construction/proposed — not doorstep leafletting targets.
LEAFLETABLE_TYPES = {
    "residential", "unclassified", "tertiary", "secondary", "living_street",
    "tertiary_link", "secondary_link",
}

# Highway types eligible for gap-fill absorption into a same-node-touching
# named road. Deliberately excludes footway/path/track/steps/cycleway/
# bridleway/service — an unnamed driveway or footpath sharing a junction
# node with "Foo Close" is not a missing *section* of Foo Close, and
# absorbing it would distort the road's tracked geometry.
ROAD_LIKE_TYPES = LEAFLETABLE_TYPES | {
    "primary", "trunk", "primary_link", "trunk_link",
}


def fetch_all_highways(bbox):
    query = f"""
[out:json][timeout:180];
way[highway]({bbox});
out geom;
"""
    print(f"Querying Overpass for ALL highways (named + unnamed) in bbox: {bbox}")
    t0 = time.time()
    resp = requests.post(OVERPASS_URL, data={"data": query},
                          headers={"User-Agent": "LeafletGapFill/1.0"}, timeout=220)
    resp.raise_for_status()
    data = resp.json()
    print(f"  {len(data.get('elements', [])):,} elements in {time.time()-t0:.0f}s")
    with open(ALL_HIGHWAYS_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def main():
    cfg = get_config()

    out = Path(ALL_HIGHWAYS_JSON)
    if out.exists():
        print(f"Loading cached {ALL_HIGHWAYS_JSON} ...")
        with open(out, encoding="utf-8") as f:
            osm = json.load(f)
    else:
        wards_gdf = gpd.read_file(WARDS_GEOJSON)
        minx, miny, maxx, maxy = wards_gdf.to_crs(4326).total_bounds
        margin = 0.05
        bbox = f"{miny-margin:.4f}, {minx-margin:.4f}, {maxy+margin:.4f}, {maxx+margin:.4f}"
        osm = fetch_all_highways(bbox)

    elements = [e for e in osm["elements"] if e.get("type") == "way"]
    print(f"{len(elements):,} ways total")

    named_elements   = [e for e in elements if e.get("tags", {}).get("name", "").strip()]
    unnamed_elements = [e for e in elements if not e.get("tags", {}).get("name", "").strip()]
    print(f"  {len(named_elements):,} named, {len(unnamed_elements):,} unnamed")

    # ── raw_roads: name -> list of (node_id_tuple, coords) ───────────────────
    # Node IDs are kept alongside coords so we can later cluster each name's
    # ways into connected components instead of blindly unioning everything
    # that shares a name string.
    raw_roads: dict[str, list[tuple[tuple, list]]] = defaultdict(list)
    for el in named_elements:
        name = el["tags"]["name"].strip()
        pts = el.get("geometry", [])
        coords = [(p["lon"], p["lat"]) for p in pts]
        nodes = tuple(el.get("nodes", []))
        if len(coords) >= 2:
            raw_roads[name].append((nodes, coords))

    # ── node_id -> set of names touching that node (from named ways only) ────
    node_to_names: dict[int, set[str]] = defaultdict(set)
    for el in named_elements:
        name = el["tags"]["name"].strip()
        for nid in el.get("nodes", []):
            node_to_names[nid].add(name)

    # ── Absorb unnamed ways whose endpoints unambiguously touch one name ─────
    n_absorbed = 0
    standalone = []  # report rows for unnamed ways that couldn't be absorbed

    for el in unnamed_elements:
        nodes = el.get("nodes", [])
        pts = el.get("geometry", [])
        if len(nodes) < 2 or len(pts) < 2:
            continue

        ht = el.get("tags", {}).get("highway")

        endpoint_names = set()
        if ht in ROAD_LIKE_TYPES:
            for nid in (nodes[0], nodes[-1]):
                endpoint_names |= node_to_names.get(nid, set())

        if len(endpoint_names) == 1:
            (name,) = endpoint_names
            coords = [(p["lon"], p["lat"]) for p in pts]
            raw_roads[name].append((tuple(nodes), coords))
            n_absorbed += 1
        elif ht in LEAFLETABLE_TYPES:
            standalone.append(el)

    # ── Drop standalone ways that fall entirely outside the constituency ─────
    # (the bbox has a 0.05deg margin beyond the ward union, so unrelated
    # roads just outside the constituency would otherwise pollute the
    # review list)
    wards_gdf_pre = gpd.read_file(WARDS_GEOJSON).to_crs(4326)
    constituency_union_pre = unary_union(list(wards_gdf_pre.geometry))
    standalone = [
        el for el in standalone
        if LineString([(p["lon"], p["lat"]) for p in el.get("geometry", [])])
               .intersects(constituency_union_pre)
    ]

    print(f"\nAbsorbed {n_absorbed:,} unnamed way(s) into existing named roads (gap-fill)")
    print(f"{len(standalone):,} standalone unnamed '{'/'.join(sorted(LEAFLETABLE_TYPES))}' "
          f"way(s) could not be matched to any named road")

    # ── Ward polygons ─────────────────────────────────────────────────────────
    wards_gdf = gpd.read_file(WARDS_GEOJSON).to_crs(4326)
    ward_records = []
    for _, row in wards_gdf.iterrows():
        gpkg_name = row["Name"]
        display = gpkg_name[:-5] if gpkg_name.endswith(" Ward") else gpkg_name
        district = _DISTRICT_BY_WARD.get(display.strip().lower(), "Unknown")
        ward_records.append((gpkg_name, display, district, row.geometry))

    ward_polys = {display: poly for _, display, _, poly in ward_records}

    # ── Cluster standalone unnamed ways that connect to EACH OTHER ───────────
    # A single unnamed estate street is often split into several OSM ways;
    # without this they'd otherwise be reported as separate "streets".
    parent = {el["id"]: el["id"] for el in standalone}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    node_to_standalone_ids: dict[int, list[int]] = defaultdict(list)
    for el in standalone:
        for nid in el.get("nodes", []):
            node_to_standalone_ids[nid].append(el["id"])

    for ids in node_to_standalone_ids.values():
        for other_id in ids[1:]:
            union(ids[0], other_id)

    clusters: dict[int, list] = defaultdict(list)
    for el in standalone:
        clusters[find(el["id"])].append(el)

    # Rural unclassified lanes often chain into one continuous network across
    # a whole ward via shared nodes — that's not "one street", so split any
    # cluster whose combined length implies it isn't a single estate road.
    MAX_CLUSTER_LENGTH_M = 800
    final_clusters: list[list] = []
    for els in clusters.values():
        length = sum(
            LineString([(p["lon"], p["lat"]) for p in el.get("geometry", [])]).length * 111_320
            for el in els
        )
        if length <= MAX_CLUSTER_LENGTH_M or len(els) == 1:
            final_clusters.append(els)
        else:
            final_clusters.extend([el] for el in els)

    # ── Report one row per cluster (ward guess via centroid) ─────────────────
    report_rows = []
    for els in final_clusters:
        root_id = els[0]["id"]
        all_pts = [p for el in els for p in el.get("geometry", [])]
        lats = [p["lat"] for p in all_pts]
        lons = [p["lon"] for p in all_pts]
        clat, clon = sum(lats) / len(lats), sum(lons) / len(lons)
        pt = Point(clon, clat)
        ward_guess = next((w for w, poly in ward_polys.items() if poly.contains(pt)), "")
        total_length_m = sum(
            LineString([(p["lon"], p["lat"]) for p in el.get("geometry", [])]).length * 111_320
            for el in els
        )
        report_rows.append({
            "cluster_id": root_id,
            "n_osm_ways": len(els),
            "highway_types": ",".join(sorted({el.get("tags", {}).get("highway") for el in els})),
            "approx_length_m": round(total_length_m),
            "ward_guess": ward_guess,
            "centroid_lat": round(clat, 6),
            "centroid_lon": round(clon, 6),
            "google_maps_link": f"https://www.google.com/maps?q={clat:.6f},{clon:.6f}",
            "osm_link": f"https://www.openstreetmap.org/way/{root_id}",
        })

    report_df = pd.DataFrame(report_rows).sort_values(
        ["ward_guess", "approx_length_m"], ascending=[True, False]
    )
    report_df.to_csv("unnamed_roads_report.csv", index=False)
    print(f"\nWrote unnamed_roads_report.csv ({len(report_df)} rows)")

    # ── Cluster each name's ways into connected components (shared node IDs) ─
    # This is the fix for generic rural lane names (Chapel Lane, Park Lane,
    # Manor Road, ...) that recur as several unconnected physical roads in
    # different villages — see module docstring point 3.
    def cluster_by_nodes(entries):
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        node_to_idx: dict[int, list[int]] = defaultdict(list)
        for idx, (nodes, _coords) in enumerate(entries):
            parent.setdefault(idx, idx)
            for nid in nodes:
                node_to_idx[nid].append(idx)
        for idxs in node_to_idx.values():
            for other in idxs[1:]:
                union(idxs[0], other)

        groups: dict[int, list] = defaultdict(list)
        for idx, entry in enumerate(entries):
            groups[find(idx)].append(entry)
        return list(groups.values())

    named_clusters: list[tuple[str, list[list[tuple[float, float]]]]] = []
    n_split = 0
    for name, entries in raw_roads.items():
        sub_clusters = cluster_by_nodes(entries)
        if len(sub_clusters) > 1:
            n_split += 1
        for cluster in sub_clusters:
            named_clusters.append((name, [coords for _nodes, coords in cluster]))

    print(f"\n{n_split:,} road name(s) split into multiple physically-disconnected clusters")

    # ── Pre-clip each cluster to constituency union (same defence as run_pipeline.py)
    constituency_union = unary_union([poly for _, _, _, poly in ward_records])
    clipped_clusters: list[tuple[str, list[list[tuple[float, float]]]]] = []
    n_outside = 0
    for name, coord_lists in named_clusters:
        kept = []
        for coords in coord_lists:
            seg = LineString(coords)
            try:
                clipped = seg.intersection(constituency_union)
            except Exception:
                continue
            if clipped.is_empty:
                continue
            if clipped.geom_type == "LineString":
                c = list(clipped.coords)
                if len(c) >= 2:
                    kept.append([(x, y) for x, y in c])
            elif clipped.geom_type == "MultiLineString":
                for part in clipped.geoms:
                    c = list(part.coords)
                    if len(c) >= 2:
                        kept.append([(x, y) for x, y in c])
        if kept:
            clipped_clusters.append((name, kept))
        else:
            n_outside += 1
    print(f"{n_outside:,} cluster(s) fell entirely outside all wards and were dropped")
    print(f"{len(clipped_clusters):,} clusters remain after pre-clip "
          f"({len(set(n for n, _ in clipped_clusters)):,} distinct names)")

    # ── Ward-clip + dominant-ward assignment, per cluster ─────────────────────
    # merged[(name, ward)] -> list of geometries to union together at the end,
    # so two disconnected same-name clusters that land in the same ward still
    # produce exactly one output row (as before), while ones in different
    # wards now correctly produce separate rows instead of one clobbering
    # the other.
    merged: dict[tuple[str, str], dict] = {}
    n_dominant = n_multiward = n_no_ward = 0
    total = len(clipped_clusters)

    for i, (road_name, coord_lists) in enumerate(clipped_clusters, 1):
        if i % 200 == 0:
            print(f"  {i:,}/{total:,} clusters processed ...")

        segments = [LineString(c) for c in coord_lists]
        merged_road = unary_union(segments)
        del segments, coord_lists

        total_length = merged_road.length
        if total_length == 0:
            n_no_ward += 1
            continue

        ward_hits = []
        for gpkg_name, display_name, district, polygon in ward_records:
            clipped = merged_road.intersection(polygon)
            if clipped.is_empty:
                continue
            ratio = clipped.length / total_length
            if ratio < MIN_FRAGMENT_RATIO:
                continue
            ward_hits.append({
                "gpkg_name": gpkg_name, "display_name": display_name,
                "district": district, "geometry": clipped, "ratio": ratio,
            })

        if not ward_hits:
            n_no_ward += 1
            continue

        ward_hits.sort(key=lambda x: x["ratio"], reverse=True)

        if (len(ward_hits) == 2
                and ward_hits[0]["ratio"] >= DOMINANT_WARD_THRESHOLD
                and ward_hits[1]["ratio"] <= BOUNDARY_ROAD_SECONDARY_MAX):
            dominant_poly = next(poly for gn, dn, di, poly in ward_records if gn == ward_hits[0]["gpkg_name"])
            secondary_poly = next(poly for gn, dn, di, poly in ward_records if gn == ward_hits[1]["gpkg_name"])
            if _is_boundary_artefact(ward_hits[1]["geometry"], dominant_poly, secondary_poly):
                ward_hits = [ward_hits[0]]
                ward_hits[0]["ratio"] = 1.0

        if ward_hits[0]["ratio"] >= DOMINANT_WARD_THRESHOLD:
            n_dominant += 1
            w = ward_hits[0]
            key = (road_name, w["display_name"])
            entry = merged.setdefault(key, {"district": w["district"], "geoms": []})
            entry["geoms"].append(merged_road)
        else:
            n_multiward += 1
            for w in ward_hits:
                key = (road_name, w["display_name"])
                entry = merged.setdefault(key, {"district": w["district"], "geoms": []})
                entry["geoms"].append(w["geometry"])

    output_rows = []
    for (road_name, ward_display), entry in merged.items():
        geom = entry["geoms"][0] if len(entry["geoms"]) == 1 else unary_union(entry["geoms"])
        lat, lon = _representative_latlon(geom)
        output_rows.append({
            "Street": road_name, "@lat": lat, "@lon": lon,
            "Ward": ward_display, "Local Authority District": entry["district"],
            "Status": "Not_Started", "Residences": "-",
            "road_geometry": _geometry_to_text(geom), "partial_geometry": "-",
        })

    output_file = OUTPUT_XLSX
    col_order = ["Street", "@lat", "@lon", "Ward", "Local Authority District",
                 "Status", "Residences", "road_geometry", "partial_geometry"]
    df = pd.DataFrame(output_rows)
    ordered = [c for c in col_order if c in df.columns]
    df = df[ordered]

    print(f"\nWriting {len(df):,} rows to {output_file} ...")
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Data", index=False)

    print()
    print("=" * 60)
    print("COMPLETE")
    print("=" * 60)
    print(f"Road names processed:    {total:,}")
    print(f"  Dominant-ward roads:   {n_dominant:,}")
    print(f"  Multi-ward roads:      {n_multiward:,}")
    print(f"  Outside all wards:     {n_no_ward:,}")
    print(f"Output rows (Data):      {len(df):,}")
    print(f"\nSaved: {output_file}")
    print(f"Unnamed-road review list: unnamed_roads_report.csv ({len(report_df)} rows)")


if __name__ == "__main__":
    main()
