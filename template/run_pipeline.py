"""
run_pipeline.py  (TEMPLATE)
=============================
End-to-end pipeline for building a constituency's leafletting spreadsheet.
Copy this whole `template/` folder to a new `<Name>/` folder, fill in
`constituency_config.py`, and run this unchanged. See ../WORKFLOW.md for
the full step-by-step process and things that reliably go wrong.

Steps
-----
  1  Load & validate constituency config
  2  Match ward names against Boundary-Line, disambiguating duplicate names
     against a district polygon, applying any parish exclusions
     -> <prefix>_wards.geojson / <prefix>_constituency.geojson
  3  Fetch all named roads from Overpass API            -> roads_raw.json
  4  Clip roads to wards, compute assignments            -> <prefix>_Leafletting.xlsx
     ('Residences' left as '-' — run estimate_residences_uprn.py next)

Reads the shared Boundary-Line.gpkg from ../data/Boundary-Line.gpkg — do
NOT copy that file (it's 1.8GB); every constituency folder shares the one
copy in data/.

Usage
-----
    python run_pipeline.py
"""

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString
from shapely.ops import unary_union

from constituency_config import get_config

# ══════════════════════════════════════════════════════════════════════════════
#  Constants
# ══════════════════════════════════════════════════════════════════════════════

GPKG_FILE        = Path("../data/Boundary-Line.gpkg")
WARD_LAYER       = "district_borough_unitary_ward"
DISTRICT_LAYER   = "district_borough_unitary"
PARISH_LAYER     = "parish"
ROADS_JSON       = "roads_raw.json"
OVERPASS_URL     = "https://overpass-api.de/api/interpreter"

BBOX_MARGIN = 0.05   # degrees, added around the auto-computed ward bbox

DOMINANT_WARD_THRESHOLD = 0.80
MIN_FRAGMENT_RATIO = 0.02

BOUNDARY_ROAD_SECONDARY_MAX = 0.15
BOUNDARY_ROAD_TOLERANCE     = 0.0003

# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _norm_name(name: str) -> str:
    return name.strip().lower()


def _geometry_to_text(geom) -> str:
    if geom is None or geom.is_empty:
        return ""

    def _norm(s: str) -> str:
        return (
            s.replace("LINESTRING (", "LINESTRING(")
             .replace("MULTILINESTRING (", "MULTILINESTRING(")
        )

    if geom.geom_type == "LineString":
        return _norm(geom.wkt)
    if geom.geom_type == "MultiLineString":
        return "|".join(_norm(g.wkt) for g in geom.geoms)
    return _norm(geom.wkt)


def _representative_latlon(geom):
    c = geom.centroid
    return round(c.y, 6), round(c.x, 6)


def _is_boundary_artefact(fragment_geom, dominant_ward_polygon, other_ward_polygon) -> bool:
    try:
        shared_boundary = dominant_ward_polygon.boundary.intersection(other_ward_polygon.boundary)
        if shared_boundary.is_empty:
            return False
        return fragment_geom.distance(shared_boundary) < BOUNDARY_ROAD_TOLERANCE
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  Step 1 — Validate config
# ══════════════════════════════════════════════════════════════════════════════

def step1_load_config():
    print()
    print("=" * 60)
    print("STEP 1 — Load configuration")
    print("=" * 60)

    cfg = get_config()

    print(f"Constituency : {cfg['display_name']}")
    print(f"Output prefix: {cfg['output_prefix']}")
    print(f"Wards        : {len(cfg['wards'])}")

    for name, district in cfg["wards"]:
        print(f"  {name:30s} [{district}]")

    if cfg.get("parish_exclusions"):
        print("\nParish exclusions:")
        for ward, parishes in cfg["parish_exclusions"].items():
            print(f"  {ward}: excludes {', '.join(parishes)}")

    return cfg


# ══════════════════════════════════════════════════════════════════════════════
#  Step 2 — Fetch ward boundaries
# ══════════════════════════════════════════════════════════════════════════════

def step2_fetch_boundaries(cfg):
    print()
    print("=" * 60)
    print("STEP 2 — Fetch ward boundaries from Boundary-Line")
    print("=" * 60)

    if not GPKG_FILE.exists():
        sys.exit(f"ERROR: {GPKG_FILE} not found.")

    file_name_filter = cfg["boundary_line_file_name"]
    file_names = [file_name_filter] if isinstance(file_name_filter, str) else list(file_name_filter)

    print(f"Loading {GPKG_FILE} …")
    gdf = gpd.read_file(GPKG_FILE, layer=WARD_LAYER)
    gdf = gdf[gdf["File_Name"].isin(file_names)].copy()
    print(f"  Wards in {file_names}: {len(gdf)}")

    # ── Locate the disambiguation district polygon ────────────────────────────
    district_filter = cfg.get("district_filter")
    district_poly = None
    if district_filter:
        dist_gdf = gpd.read_file(GPKG_FILE, layer=DISTRICT_LAYER)
        dist_gdf["Name"] = dist_gdf["Name"].fillna("")
        cand = dist_gdf[dist_gdf["Name"].str.lower().str.startswith(district_filter.lower())]
        if len(cand) == 0:
            sys.exit(f"ERROR: could not find district matching '{district_filter}'")
        district_poly = cand.geometry.iloc[0]
        print(f"  Disambiguation district: {cand['Name'].iloc[0]}")

    # ── Match ward names (with/without " Ward" suffix), disambiguating on
    #    district containment when a name matches more than one row ─────────
    gdf["Name"] = gdf["Name"].fillna("")
    by_norm_name = {}
    for idx, row in gdf.iterrows():
        by_norm_name.setdefault(_norm_name(row["Name"]), []).append(idx)

    matched_idx  = []   # row indices into gdf
    district_map = {}   # gdf row index -> district label
    missing      = []

    for user_name, district in cfg["wards"]:
        norm = _norm_name(user_name)
        candidates = by_norm_name.get(norm) or by_norm_name.get(norm + " ward")

        if not candidates:
            missing.append(user_name)
            continue

        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            # Disambiguate using the district polygon
            in_district = [
                i for i in candidates
                if district_poly is not None and gdf.loc[i, "geometry"].centroid.within(district_poly)
            ]
            if len(in_district) == 1:
                chosen = in_district[0]
            else:
                sys.exit(
                    f"ERROR: ward '{user_name}' matched {len(candidates)} rows and "
                    f"district disambiguation did not resolve to exactly one "
                    f"(got {len(in_district)}). Fix district_filter / ward name."
                )

        matched_idx.append(chosen)
        district_map[chosen] = district

    if missing:
        print("\nERROR: Could not match the following ward names:")
        for m in missing:
            print(f"  '{m}'")
        print("\nAvailable ward names in this File_Name:")
        for n in sorted(gdf["Name"]):
            print(f"  {n}")
        sys.exit("Fix ward names in constituency_config.py and retry.")

    matched = gdf.loc[matched_idx].copy()
    matched = matched.to_crs(4326)

    print("\nMatched wards:")
    for idx in matched_idx:
        print(f"  {gdf.loc[idx, 'Name']}")

    # ── Apply parish exclusions ────────────────────────────────────────────────
    parish_exclusions = cfg.get("parish_exclusions") or {}
    if parish_exclusions:
        parish_gdf = gpd.read_file(GPKG_FILE, layer=PARISH_LAYER)
        parish_gdf = parish_gdf[parish_gdf["File_Name"].isin(file_names)].copy()
        parish_gdf["Name"] = parish_gdf["Name"].fillna("")
        parish_gdf = parish_gdf.to_crs(4326)

        # display name (without " Ward") -> gdf row index
        display_to_idx = {}
        for idx in matched_idx:
            disp = gdf.loc[idx, "Name"]
            if disp.endswith(" Ward"):
                disp = disp[:-5]
            display_to_idx[disp] = idx

        for ward_display, parish_names in parish_exclusions.items():
            if ward_display not in display_to_idx:
                print(f"  WARNING: parish exclusion target ward '{ward_display}' not in matched set — skipping")
                continue
            idx = display_to_idx[ward_display]
            ward_geom = matched.loc[idx, "geometry"]

            for pname in parish_names:
                norm_p = _norm_name(pname)
                cand = parish_gdf[
                    parish_gdf["Name"].str.lower().str.startswith(norm_p)
                ]
                if len(cand) == 0:
                    sys.exit(f"ERROR: parish '{pname}' not found in Boundary-Line parish layer")
                if len(cand) > 1:
                    sys.exit(f"ERROR: parish '{pname}' matched {len(cand)} rows — ambiguous")

                parish_geom = cand.geometry.iloc[0]
                before_area = ward_geom.area
                ward_geom = ward_geom.difference(parish_geom)
                after_area = ward_geom.area
                print(
                    f"  Subtracted '{cand['Name'].iloc[0]}' from '{ward_display}' Ward "
                    f"(area {before_area:.6f} -> {after_area:.6f} deg^2)"
                )

            matched.loc[idx, "geometry"] = ward_geom

    # ── Auto-compute bbox from the (possibly clipped) ward geometries ────────
    minx, miny, maxx, maxy = matched.total_bounds
    bbox = (
        f"{miny - BBOX_MARGIN:.4f}, {minx - BBOX_MARGIN:.4f}, "
        f"{maxy + BBOX_MARGIN:.4f}, {maxx + BBOX_MARGIN:.4f}"
    )
    cfg["bbox"] = bbox
    print(f"\nAuto-computed bbox: {bbox}")

    # ── Write outputs ──────────────────────────────────────────────────────────
    wards_out = f"{cfg['output_prefix']}_wards.geojson"
    matched.to_file(wards_out, driver="GeoJSON")
    print(f"\nWrote {wards_out}")

    const_out = f"{cfg['output_prefix']}_constituency.geojson"
    matched.dissolve().reset_index(drop=True).to_file(const_out, driver="GeoJSON")
    print(f"Wrote {const_out}")

    return matched, district_map


# ══════════════════════════════════════════════════════════════════════════════
#  Step 3 — Fetch roads from Overpass
# ══════════════════════════════════════════════════════════════════════════════

def step3_fetch_roads(cfg):
    print()
    print("=" * 60)
    print("STEP 3 — Fetch roads from Overpass API")
    print("=" * 60)

    out = Path(ROADS_JSON)
    if out.exists():
        print(f"{ROADS_JSON} already exists — skipping fetch.")
        return

    bbox = cfg["bbox"]
    query = f"""
[out:json][timeout:120];
way[highway][name]({bbox});
out geom;
"""
    headers = {"User-Agent": f"LeaflettingMapper/2.0 ({cfg['display_name']})"}

    print(f"Querying Overpass for bbox: {bbox}")
    t0 = time.time()
    try:
        resp = requests.post(OVERPASS_URL, data={"data": query}, headers=headers, timeout=180)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        sys.exit(f"ERROR fetching roads: {exc}")

    elapsed = time.time() - t0
    elements = data.get("elements", [])
    print(f"Received {len(elements):,} road segments in {elapsed:.0f}s")

    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print(f"Saved {ROADS_JSON}")


# ══════════════════════════════════════════════════════════════════════════════
#  Step 4 — Clip roads to wards, build spreadsheet
# ══════════════════════════════════════════════════════════════════════════════

def step4_build_spreadsheet(cfg, wards_gdf, district_map):
    print()
    print("=" * 60)
    print("STEP 4 — Clip roads to wards & build spreadsheet")
    print("=" * 60)

    ward_records = []
    for idx, row in wards_gdf.iterrows():
        gpkg_name = row["Name"]
        display   = gpkg_name[:-5] if gpkg_name.endswith(" Ward") else gpkg_name
        district  = district_map.get(idx, "Unknown")
        ward_records.append((gpkg_name, display, district, row.geometry))

    print(f"Ward polygons loaded: {len(ward_records)}")

    print(f"Loading {ROADS_JSON} …")
    with open(ROADS_JSON, "r", encoding="utf-8") as f:
        osm = json.load(f)

    elements = osm.get("elements", [])
    print(f"  {len(elements):,} OSM ways")

    # Node IDs are kept alongside coords (not just the coordinate list) so
    # that same-named-but-physically-disconnected roads can be told apart
    # below — see the clustering step.
    raw_roads: dict[str, list[tuple[tuple, list]]] = {}
    for el in elements:
        if el.get("type") != "way":
            continue
        name = el.get("tags", {}).get("name")
        if not name:
            continue
        pts = el.get("geometry", [])
        coords = [(p["lon"], p["lat"]) for p in pts]
        nodes = tuple(el.get("nodes", []))
        if len(coords) >= 2:
            raw_roads.setdefault(name, []).append((nodes, coords))

    del osm, elements
    import gc
    gc.collect()

    print(f"  Grouped into {len(raw_roads):,} road names")

    # ── Cluster each name's ways into connected components (shared node IDs)
    # Generic rural lane names (Chapel Lane, Park Lane, Manor Road, ...)
    # commonly recur as several totally unconnected physical roads in
    # different villages/wards. Grouping purely by name string and summing
    # their lengths inflates the denominator MIN_FRAGMENT_RATIO /
    # DOMINANT_WARD_THRESHOLD are computed against, so a real village's
    # instance can fall under the noise threshold relative to a much longer
    # same-named road elsewhere — silently dropping (or mis-assigning) an
    # entire real street. Clustering by connectivity first fixes this: each
    # physically distinct road is ward-assigned independently, and clusters
    # that land in the same (name, ward) are merged back into one row.
    def _cluster_by_nodes(entries):
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
        sub_clusters = _cluster_by_nodes(entries)
        if len(sub_clusters) > 1:
            n_split += 1
        for cluster in sub_clusters:
            named_clusters.append((name, [coords for _nodes, coords in cluster]))

    print(f"  {n_split:,} road name(s) split into multiple physically-disconnected clusters")

    print("  Building constituency union polygon for pre-clipping …")
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

    print(f"  {n_outside:,} cluster(s) fell entirely outside all wards and were dropped")
    print(f"  {len(clipped_clusters):,} clusters remain after pre-clip")

    del raw_roads, named_clusters, constituency_union

    # merged[(name, ward)] -> geometries to union together at the end, so two
    # disconnected same-name clusters landing in the same ward still produce
    # exactly one output row, while ones in different wards now correctly
    # produce separate rows instead of one clobbering the other.
    merged: dict[tuple[str, str], dict] = {}
    n_dominant  = 0
    n_multiward = 0
    n_no_ward   = 0

    total = len(clipped_clusters)

    for i, (road_name, coord_lists) in enumerate(clipped_clusters, 1):
        if i % 200 == 0:
            print(f"  {i:,}/{total:,} clusters processed …")

        segments    = [LineString(c) for c in coord_lists]
        merged_road = unary_union(segments)
        del segments, coord_lists

        total_length = merged_road.length
        if total_length == 0:
            del merged_road
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
            del merged_road
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

        for w in ward_hits:
            del w["geometry"]
        del merged_road, ward_hits

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

    output_file = f"{cfg['output_prefix']}_Leafletting.xlsx"
    col_order = ["Street", "@lat", "@lon", "Ward", "Local Authority District",
                 "Status", "Residences", "road_geometry", "partial_geometry"]

    df = pd.DataFrame(output_rows)
    ordered = [c for c in col_order if c in df.columns]
    ordered += [c for c in df.columns if c not in ordered]
    df = df[ordered]

    print(f"\nWriting {len(df):,} rows to {output_file} …")
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
    print("\nNOTE: 'Residences' column is set to '-' throughout.")
    print("Run estimate_residences_uprn.py next to populate it.")


if __name__ == "__main__":
    cfg             = step1_load_config()
    wards_gdf, dmap = step2_fetch_boundaries(cfg)
    step3_fetch_roads(cfg)
    step4_build_spreadsheet(cfg, wards_gdf, dmap)
