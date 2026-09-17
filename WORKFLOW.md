# Leafletting pipeline — how to build a dataset for a new area

This is the practical build guide: clone the repo, get the two source
datasets, fill in one config file, run five scripts in order, get back a
spreadsheet and/or a GIS file for a UK constituency or council area. If
you're setting up the Google Sheet + Apps Script tracker on top of this,
see the README's "Setting up a new deployment" section instead — this file
only covers building the underlying road/residence data.

(There's also a `CLAUDE.md` in this repo — that's instructions for an AI
coding agent working on the pipeline itself, not a guide for running it.
This file is the human version.)

## What this actually does

For a given set of wards, the pipeline:

1. Finds the ward boundary polygons in Ordnance Survey's Boundary-Line
   dataset.
2. Fetches every named road inside those wards from OpenStreetMap
   (via the Overpass API).
3. Clips each road to whichever ward(s) it actually falls in.
4. Counts residences per road by matching OS Open UPRN address points
   (buffered 40m around each road centreline) — not OSM building
   footprints, which undercount badly (see "Why UPRN, not OSM buildings"
   below).
5. Writes the result as an Excel workbook (one row per road, columns for
   Street/Ward/Residences/Status), and optionally as a GIS file.

## Prerequisites

- **Python 3.10+** with `geopandas`, `pandas`, `shapely`, `pyproj`,
  `openpyxl`, `requests`, `numpy` installed (`pip install geopandas pandas
  shapely pyproj openpyxl requests numpy`).
- **OS Boundary-Line** (`Boundary-Line.gpkg`, ~1.8GB) — free download from
  [OS Data Hub](https://osdatahub.os.uk/downloads/open/BoundaryLine),
  Open Government Licence.
- **OS Open UPRN** (`osopenuprn_*.csv`, ~2.2GB, GB-wide) — free download
  from [OS Data Hub](https://osdatahub.os.uk/downloads/open/OpenUPRN),
  same licence.

Put both files in a `data/` folder next to `template/`:

```
your-workspace/
  data/
    Boundary-Line.gpkg
    osopenuprn_202605.csv        (filename has the release date baked in —
                                   adjust UPRN_CSV in estimate_residences_uprn.py
                                   if you download a different release)
  template/                       (this repo's template/, copy it per area)
```

Both datasets are shared across every area you build — don't duplicate
them per constituency, just point each area's copy of the scripts at the
same `data/` folder (the templates assume it's at `../data/` relative to
wherever you copy `template/` to).

## Step 1 — Get the ward list, verify it against Boundary-Line

Get the list of wards (and the constituency/council area name) from
whoever's asking for the data. **Don't assume ward names as given are
current** — local government wards get renamed, split, or merged
periodically, and Boundary-Line only carries the current geography. Check
before writing any config:

```python
import sqlite3
con = sqlite3.connect("data/Boundary-Line.gpkg")
cur = con.cursor()

# find the File_Name covering your area
cur.execute("SELECT DISTINCT File_Name FROM district_borough_unitary_ward "
            "WHERE File_Name LIKE '%YOURAREA%'").fetchall()

# list the actual current ward names in that File_Name
cur.execute("SELECT Name FROM district_borough_unitary_ward "
            "WHERE File_Name='...' ORDER BY Name").fetchall()
```

If a name you were given doesn't appear, it's usually a rename or a
split — figure out the current equivalent rather than guessing, since a
wrong match silently assigns roads to the wrong ward with no error.

## Step 2 — Copy the template, fill in the config

```bash
cp -r template my-area
cd my-area
```

Edit `constituency_config.py`:

- `output_prefix` / `display_name` — used to name every output file.
- `boundary_line_file_name` — the `File_Name` value from Step 1 (a list,
  if your area spans more than one).
- `wards` — the verified `(name, district_label)` list.
- `district_filter` — only needed if a ward name isn't unique within its
  `File_Name` (check by counting matches first).
- `parish_exclusions` — only if a ward is "part — excluding X parish".
- `westminster_const_name` — only if a ward is split by a constituency
  boundary along something other than a whole parish (rare — a 2023
  boundary review sometimes did this). Leave unset unless you've checked
  a ward lands at neither ~0% nor ~100% inside the constituency polygon.

Before burning an Overpass fetch, sanity-check the ward matching alone:

```python
from run_pipeline import step1_load_config, step2_fetch_boundaries
cfg = step1_load_config()
wards_gdf, dmap = step2_fetch_boundaries(cfg)
```

This hard-fails and prints the available names if anything's unmatched —
use that instead of guessing.

## Step 3 — Run the pipeline

```bash
python run_pipeline.py
```

Fetches roads from Overpass (a single bbox query, ~5–60s depending on
area size) and writes `<prefix>_Leafletting.xlsx` with `Residences` left
as `"-"`.

```bash
python fill_gaps.py
```

Optional but recommended — every area built without it turned out to be
missing real roads. Fixes two gaps in a plain `way[highway][name]` fetch:
OSM sometimes splits one physical road into several ways at junctions
with the `name` tag missing from some segments (recovered by checking
shared endpoint nodes), and genuinely unnamed estate/rural roads (written
to `unnamed_roads_report.csv` with a map link, for a human to name later
— not auto-named, since there's no reliable address-tag data to name them
from). Writes `<prefix>_Leafletting_gapfilled.xlsx` — **copy this over
`<prefix>_Leafletting.xlsx`** before continuing, so residence counts get
computed against the extended road set.

```bash
python estimate_residences_uprn.py
```

Reads the ~40M-row UPRN CSV in chunks (~20s) then spatially joins it
against 40m road buffers (~1 min for a whole borough). Writes
`<prefix>_Leafletting_residences_uprn.xlsx`. **This overwrites that file
if one already exists** — if you're refreshing an area that has real
tracked progress in a live tracker, copy the existing file aside first
(see "Refreshing an existing area" below).

```bash
python add_unnamed_roads.py
```

Appends every cluster from `unnamed_roads_report.csv` onto the residences
file as `Street = "Unknown Road"` placeholder rows, so nothing gets lost
before a human assigns real names. Uses a standalone 40m buffer, not
jointly disambiguated against the named roads — fine for spotting missing
streets, worth a caveat if exact totals matter.

### Why UPRN, not OSM buildings

An earlier version of this pipeline assigned OSM building footprints
(filtered to `building=house`/`detached`/etc) to the nearest road. It
undercounts badly wherever a building is tagged generically as
`building=yes` — very common — which measured at roughly an 8x
undercount on a real run. UPRNs are actual Royal Mail/OS address points
and don't depend on OSM tagging quality, so buffer-matching against those
directly is the only method that's been verified not to silently drop
residences.

## Step 4 — Sanity-check the output

This costs one query and catches "ran fine, silently wrong":

```python
import pandas as pd
df = pd.read_excel("<prefix>_Leafletting_residences_uprn.xlsx")
df["Residences"].sum()                                   # plausible for the area's population?
df.groupby("Ward")["Residences"].sum()                    # roughly proportionate across wards?
df.sort_values("Residences", ascending=False).head(10)    # real, recognisable street names?
df[df["Residences"] == 0][["Street", "Ward"]]             # should mostly be bridges/footpaths/farm tracks
```

A total wildly below the area's known household count (roughly
electorate ÷ 1.5–2) is the strongest signal something's wrong. That said,
it's a floor, not a ceiling — dense terraced/flatted housing and rural
areas (where every farm outbuilding gets its own UPRN with no resident
behind it) both legitimately land above that range. Compare against a
previously-built area of similar character before concluding either way.

## Step 5 — Export a GIS file (new — for QGIS or similar)

Every stage above keeps the road geometry as WKT text in the
`road_geometry` column, so turning it back into a real GIS layer is just
a parsing step:

```bash
python export_gis.py                  # writes <prefix>.gpkg (default)
python export_gis.py --format geojson # writes <prefix>_roads.geojson
```

The GeoPackage contains three layers — `roads` (Street/Ward/Local
Authority District/Status/Residences + geometry), `wards`, and
`constituency` — all in EPSG:4326. Open it in QGIS with **Layer → Add
Layer → Add Vector Layer**, or just drag the `.gpkg` file into the map
canvas. It picks up whichever xlsx is furthest along the pipeline
(residences file if it exists, gap-filled if not, raw output otherwise),
so run it any time after Step 3, ideally after Step 3's `add_unnamed_roads.py`
so the export includes residence counts and placeholder rows.

## Step 6 — Deliver

Hand over `<prefix>_Leafletting_residences_uprn.xlsx` (and/or
`<prefix>.gpkg`), `unnamed_roads_report.csv` for manual road naming, and
say which method was used (UPRN-buffer) and the total residence count, so
whoever's checking it has a number to sanity-check against their own
knowledge of the area.

If you also want a hosted, trackable version (Google Sheet with a
Dashboard/Changelog, deployed as a live canvassing map), run
`build_tracker.py` next and see the README's "Setting up a new
deployment" section.

## Refreshing an existing area

Re-running `run_pipeline.py` on an area built before the
connected-component road-clustering fix (see "Known issues already
fixed" below) is worth doing — it's found real missing roads and wrong
residence counts every time it's been tried.

1. Copy the current `run_pipeline.py`, `fill_gaps.py`,
   `estimate_residences_uprn.py`, and `add_unnamed_roads.py` over the
   area's old copies if they predate the fix (check for `from collections
   import defaultdict` near the top of `run_pipeline.py` — if it's
   missing, the file is a pre-fix copy).
2. Delete the area's `roads_raw.json` for a fresh Overpass fetch (picks
   up OSM edits since last time) — otherwise the cached fetch is reused
   and just re-clipped with the fixed logic, which is faster and still
   gets the fix.
3. Run Steps 3–5 above.
4. **Before overwriting anything with real tracked progress**: check
   whether `Status` is 100% `Not_Started` in the live/reference sheet. If
   it isn't, don't run `estimate_residences_uprn.py` pointed at that file
   directly — copy it aside first, then use `finalize_output.py` (set
   `BASE_XLSX` to the real sheet, `OUTPUT_XLSX` to a new name) to merge:
   it matches rows by `(Street, Ward)`, updates every column except
   `Status`, appends genuinely new rows at the end (never reorders — row
   *position* is used as row identity by the live tracker apps, so
   reordering silently corrupts in-flight edits), and logs anything with
   no fresh match to `review_no_fresh_match.csv` for a human to check.

## Known issues already fixed (don't reintroduce these)

- **Same-name road collisions**: a name like "Chapel Lane" or "Park Lane"
  commonly recurs as several physically unconnected roads across a rural
  area. Grouping by name string alone before computing ward assignment
  ratios silently drops or misattributes the shorter one(s) — fixed by
  clustering each name's OSM ways by shared node IDs first, so each
  physically distinct road is assigned independently.
- **Dashboard formula ranges**: `build_tracker.py`'s `Dashboard` sheet
  must be regenerated per area, not copied from a different area's
  tracker with the ward names edited — an 18-ward Dashboard reused for a
  21-ward area once shipped with `Overall` row `SUM` ranges still
  hard-coded to the old sheet size, silently summing the wrong rows.
  Every range in `build_tracker.py`'s output is derived from the actual
  ward count and row count at generation time — always regenerate via
  the script.

## A note on scope

A ward list that maps roughly evenly across two constituencies probably
means the request is for the whole council area, not one constituency —
confirm rather than picking one. Likewise, "the Parliamentary
Constituency" without a name isn't enough to proceed if the wards given
span more than one — check via Boundary-Line's `westminster_const` layer
(centroid-in-polygon test) and ask if it's not a clean single match.

## Rate limits

Don't run more than one `fill_gaps.py` Overpass fetch at a time across
multiple areas — its "every highway, not just named ones" query is heavy
enough that a few in parallel reliably triggers `429 Too Many Requests`
from the public Overpass instance. `run_pipeline.py`'s fetch is smaller
and tolerates a few in parallel fine.
