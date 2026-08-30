# Leafletting spreadsheet workflow

How to build a leafletting spreadsheet (roads grouped by ward, with a
residence count per road) for a new UK Westminster constituency or council
area. Read this whole file before starting a new one — it's short and will
save you from re-discovering a growing list of real bugs (see "Don't
rebuild the wheel" under Token-efficiency notes) the hard way.

## Folder layout

```
Leaflets/
  Leaflets/           venv (Python 3.10) — use Leaflets/Scripts/python.exe,
                      NOT system python (system python has nothing installed)
  data/               shared reference datasets, do not duplicate:
                        Boundary-Line.gpkg        (~1.8GB, OS Boundary-Line)
                        osopenuprn_202605.csv     (~2.2GB, OS Open UPRN, GB-wide)
  template/           canonical scripts — copy this folder to start a new one
                        constituency_config.py    (fill in)
                        run_pipeline.py            (run unchanged)
                        fill_gaps.py               (run unchanged — optional but recommended)
                        estimate_residences_uprn.py (run unchanged)
                        add_unnamed_roads.py       (run unchanged — optional but recommended)
                        build_tracker.py           (run unchanged)
                        finalize_output.py         (only for refreshing an existing constituency)
  <ConstituencyName>/ one folder per finished/in-progress constituency
  _archive/           superseded scripts/outputs, kept for reference only
  WORKFLOW.md         this file
```

## Step-by-step for a new constituency

1. **Get the ward list from whoever's asking**, and the constituency/area
   name. Don't assume it's a single Westminster constituency — confirm scope
   (see "Scope pitfalls" below) if the request is at all ambiguous.

2. **Verify the ward names against Boundary-Line BEFORE writing any config.**
   Local government wards get reorganised (renamed/split/merged) periodically,
   and whoever supplies the list may be using stale names. Check:
   ```python
   import sqlite3
   con = sqlite3.connect("data/Boundary-Line.gpkg")
   cur = con.cursor()
   # find the File_Name covering this area
   cur.execute("SELECT DISTINCT File_Name FROM district_borough_unitary_ward "
               "WHERE File_Name LIKE '%YOURAREA%'").fetchall()
   # list the actual current ward names in that File_Name
   cur.execute("SELECT Name FROM district_borough_unitary_ward "
               "WHERE File_Name='...' ORDER BY Name").fetchall()
   ```
   If names don't match, don't guess a fix yourself — this is worth surfacing
   to the user. A ward existing under a different name is common (rename,
   split into East/West, merged with a neighbour); a ward with genuinely no
   equivalent in current geography is rarer but happens, and either way
   which to use is the user's call, not something to silently substitute.
   (Old ward names from a superseded local government reorganisation aren't
   necessarily wrong for the task — a Westminster constituency boundary
   review may have been built from wards as they stood at a past date — but
   Boundary-Line here only carries current wards, so using it means using
   current names. Getting this wrong burns a full pipeline run silently
   assigning roads to the wrong/nonexistent wards.)

3. **Copy `template/` to a new folder** named after the constituency
   (`cp -r template NewConstituency`), and fill in `constituency_config.py`:
   - `boundary_line_file_name`: the File_Name from step 2.
   - `district_filter`: only needed if a ward name isn't unique within that
     File_Name (rare — check by counting matches per name first).
   - `wards`: the verified (name, district_label) list.
   - `parish_exclusions`: only if a ward is described as "part — excluding
     X parish". Check the `parish` layer (same File_Name) for the parish's
     exact name and confirm it's ~100% inside the ward before subtracting
     (a quick area-ratio sanity check, not just a name match).
   - `authorised_emails`: only needed if this constituency's tracker will be
     hosted as a Google Sheet with a bound Apps Script (see step 10) — the
     email addresses allowed to bypass its edit locks. Leave as `[]`
     otherwise.

4. **Sanity-check ward matching before burning an Overpass fetch.** Run just
   steps 1–2 first:
   ```python
   from run_pipeline import step1_load_config, step2_fetch_boundaries
   cfg = step1_load_config()
   wards_gdf, dmap = step2_fetch_boundaries(cfg)
   ```
   Check the printed bbox is a sane size for the area, and that every ward
   you expected got matched (the script hard-fails and lists available names
   if any are missing — use that, don't guess ward names by trial and error).

5. **Run the full pipeline**: `python run_pipeline.py` — fetches roads from
   Overpass (a bbox query, ~5–60s depending on area size), clips to wards,
   writes `<prefix>_Leafletting.xlsx` with `Residences` left as `"-"`.

6. **Gap-fill and unnamed-road detection**: `python fill_gaps.py` — optional,
   but every constituency built without it so far turned out to be missing
   real roads, so treat it as a normal step, not a special case. Fixes two
   things a plain `way[highway][name]` fetch misses:
     - **Missing sections**: OSM sometimes splits one physical named road
       into several `way`s at junctions, and some segments lose the `name`
       tag. `fill_gaps.py` fetches ALL highways (not just named ones,
       cached as `all_highways_raw.json`) and absorbs an unnamed segment
       into a named road when its endpoint node is unambiguously shared
       with exactly one named road (restricted to real-road highway types —
       residential/unclassified/tertiary/secondary/living_street/primary/
       trunk + `_link` variants — so a driveway or footpath touching a
       junction never gets pulled into the tracked geometry).
     - **Genuinely unnamed streets**: small estate roads with no `name` tag
       at all (common in rural villages). Not safe to auto-name (no
       reliable address-tag data at hand), so these are written to
       `unnamed_roads_report.csv` (clustered by shared nodes, capped at
       800m per cluster — otherwise rural unclassified lanes chain into one
       "cluster" spanning a whole ward) with a Google Maps link + OSM link
       + ward guess, for a human to name later.
   Writes `<prefix>_Leafletting_gapfilled.xlsx` — copy this over
   `<prefix>_Leafletting.xlsx` before continuing to step 7, so residence
   counts get computed against the *extended* geometry, not the original
   gappy one.

7. **Run the residence estimator**: `python estimate_residences_uprn.py`.
   For a whole borough this reads ~40M UPRN rows in chunks (~20s) then does
   a spatial join (~1 min) — run it in the background, don't block on it.
   Do NOT use an OSM-building-footprint approach for this — see
   `_archive/flawed_osm_building_method/README.md` for why it silently
   undercounts by roughly 8x. UPRN-buffer matching is the only method that's
   been verified to work. **This overwrites
   `<prefix>_Leafletting_residences_uprn.xlsx` if one already exists** — if
   this constituency has real tracked progress (check `Status` isn't 100%
   `Not_Started` first), copy that file aside before rerunning.

8. **Add the unnamed roads as placeholder rows**: `python add_unnamed_roads.py`
   — appends every cluster from `unnamed_roads_report.csv` onto
   `<prefix>_Leafletting_residences_uprn.xlsx` as `Street = "Unknown Road"`
   rows (real names can replace that text later without touching geometry),
   with a standalone 40m-buffer UPRN count. That count is **not** jointly
   disambiguated against the named roads the way step 7 disambiguates
   overlapping buffers against each other — a UPRN near both a named road
   and one of these could in principle be counted by both — fine for
   spotting where real streets are missing, worth a caveat if the exact
   totals matter downstream.

9. **Sanity-check the output** before sending it — this costs one query and
   catches the entire class of "silently ran, silently wrong" failures:
   ```python
   import pandas as pd
   df = pd.read_excel("<prefix>_Leafletting_residences_uprn.xlsx")
   df["Residences"].sum()                          # plausible for area population?
   df.groupby("Ward")["Residences"].sum()           # roughly proportionate across wards?
   df.sort_values("Residences", ascending=False).head(10)  # real, recognisable street names?
   df[df["Residences"]==0][["Street","Ward"]]        # should mostly be bridges/footpaths/farm tracks
   ```
   A total residence count wildly below the area's known household count
   (roughly electorate / 1.5–2) is the single strongest signal something's
   wrong — check that first.

10. **Build the Google-Sheets-ready tracker**: `python build_tracker.py`.
   Takes `<prefix>_Leafletting_residences_uprn.xlsx` and writes
   `<prefix>_Tracker.xlsx` with the full sheet set a hosted tracker needs:
   - `Data` — the same road/ward/residence rows, plus the finishing touches
     a live tracker needs: frozen header row, hidden `@lat` column, and a
     dropdown data validation on `Status` (`Not_Started` / `Planned` /
     `In_Progress` / `Complete`).
   - `Dashboard` — one formula-driven row per ward (roads by status,
     residences reached, % complete), an `Overall` total row, a "Borough
     Summary" KPI block (residences reached/remaining, wards fully done,
     wards not started), a `Residences Remaining` column, and four charts:
     % complete by ward, status-by-ward stacked bar, a borough-wide status
     donut, and a residences-remaining-by-ward bar (for spotting where the
     next leaflet run has the most impact).
   - `Changelog`, `Checksum`, `Authorised` — support sheets for a bound
     Google Apps Script to log edits and gate who can override them
     (`Authorised` is filled from `authorised_emails` in the config; the
     Apps Script itself isn't part of this repo — it lives on the Google
     Sheet once uploaded).
   Every range on `Dashboard` is generated from the actual ward count and
   actual `Data` row count — **don't hand-copy a Dashboard sheet from a
   different constituency's tracker and just edit the ward names**, that's
   exactly how a real bug got shipped once (a Dashboard built for an
   18-ward borough got reused for a 21-ward one; the per-ward formulas used
   `Data!$D:$D`-style full-column references so those still worked, but the
   `Overall` row's `SUM` ranges and the residences-served formula's row
   bound stayed hard-coded to the old sheet's size and silently summed the
   wrong rows). Always regenerate via `build_tracker.py` instead.

11. **Deliver the file** and mention which method was used (UPRN-buffer) and
   the total residence count, so whoever's checking it has a number to
   sanity-check against their own knowledge of the area.

## Refreshing an existing constituency

Re-running `run_pipeline.py` on a constituency built before 2026-08-27 will
pick up the connected-component clustering fix (see "Don't rebuild the
wheel" under Token-efficiency notes below) and is worth doing — it's found
real missing roads and badly wrong residence counts every time it's been
tried so far. To refresh one:

1. Copy the fixed `run_pipeline.py`, `fill_gaps.py`, `estimate_residences_uprn.py`,
   and `add_unnamed_roads.py` into the constituency's folder if they predate
   the fix (check for `from collections import defaultdict` near the top of
   `run_pipeline.py` — if it's missing, the file is a pre-fix copy).
   `constituency_config.py` doesn't need touching unless it's missing
   `boundary_line_file_name` (an older field added partway through the
   project — add it if absent, using the `File_Name` from step 2).
2. Delete the constituency's `roads_raw.json` if you want a fresh Overpass
   fetch (picks up OSM edits since the last fetch) — otherwise `run_pipeline.py`
   reuses the cached one and just re-runs the (now-fixed) clipping logic,
   which is much faster and still gets the clustering fix.
3. Run steps 5–8 as above (`run_pipeline.py` → `fill_gaps.py` → copy
   gapfilled over `_Leafletting.xlsx` → `estimate_residences_uprn.py` →
   `add_unnamed_roads.py`).
4. **Before overwriting anything real**: check whether the constituency has
   actual tracked progress (`Status` not 100% `Not_Started` in whatever the
   live/reference sheet is). If it does, do NOT run `estimate_residences_uprn.py`
   pointed at that file directly — it overwrites `<prefix>_Leafletting_residences_uprn.xlsx`
   in place. Copy the existing file aside first, then use `finalize_output.py`
   (set `BASE_XLSX` to the real sheet, `OUTPUT_XLSX` to a new name) to merge:
   it matches rows by (Street, Ward) and updates every column except
   `Status`, appends genuinely new rows at the end (never reorders/inserts —
   several apps built on these sheets use row *position* as a row's
   identity for tracking in-flight edits, so reordering silently corrupts
   that), and leaves anything with no fresh match untouched, logged to
   `review_no_fresh_match.csv` for a human to look at. If the constituency
   has zero real progress, skip the merge — the fresh
   `_Leafletting_residences_uprn.xlsx` (with `add_unnamed_roads.py`'s
   placeholder rows already appended at the end) is the deliverable as-is.
5. Deliver the file(s) plus `unnamed_roads_report.csv` for manual naming.

Full case study — the road-collision bug that motivated this, and what it
did to Stafford's live tracker — plus which constituencies have and
haven't been refreshed yet, is in Claude's memory (search for "Stafford
missing roads fix" / "Pipeline fix rollout") rather than here, since
that's a point-in-time status record, not a repeatable procedure. As of
2026-08-27, Stafford, Stone, Barnsley, Burton_Uttoxeter, and St_Helens
have all been refreshed with the fixed pipeline.

## Scope pitfalls (ask, don't guess)

- A ward list that maps roughly evenly across two constituencies probably
  means the requester wants the whole council area, not one constituency —
  confirm rather than picking one.
- "The Parliamentary Constituency" without a name given isn't enough to
  proceed if the area covers more than one — check which constituency(ies)
  the wards actually fall in (via Boundary-Line's `westminster_const` layer,
  centroid-in-polygon test) and ask if it's not a clean single match.

## Token-efficiency notes for future sessions

- The venv already has everything installed (geopandas, pandas, shapely,
  openpyxl, requests, pyproj, numpy, osmnx). Don't reinstall — just use
  `Leaflets/Scripts/python.exe`.
- Don't re-derive `File_Name` / `district_filter` values by trial and error
  across multiple failed pipeline runs — one sqlite query (step 2 above)
  answers it directly and is far cheaper than a failed Overpass fetch +
  re-run.
- Steps 5, 6, and 7 are genuinely slow (Overpass fetch, another Overpass
  fetch, 2.2GB CSV scan) — launch them as background commands and keep
  working rather than blocking on them synchronously.
- `roads_raw.json` in a constituency folder is a cache of the Overpass
  response — `run_pipeline.py` skips re-fetching if it already exists.
  Delete it to force a fresh fetch (e.g. if the ward list changed).
  `all_highways_raw.json` is `fill_gaps.py`'s equivalent cache (a broader
  fetch — every highway, not just named ones) — same deal.
- **Don't run more than one `fill_gaps.py` Overpass fetch at a time.**
  Firing off 3–4 constituencies' `fill_gaps.py` in parallel background
  commands hit `429 Too Many Requests` from the public Overpass instance
  on every one of them; run them one at a time (or with a real delay
  between launches) instead. `run_pipeline.py`'s fetch is smaller/faster
  and tolerated a few in parallel fine — it's specifically the
  all-highways fetch that's heavy enough to trip the rate limit.
- Don't rebuild the wheel: `template/` already encodes every fix found so
  far — ward-name disambiguation, parish exclusion, per-district
  `File_Name`, the UPRN duplicate-resolution bug, the Dashboard range bug,
  connected-component road clustering (a name like "Chapel Lane" or "Park
  Lane" recurs as several unconnected physical roads across a rural
  constituency; grouping by name string alone before computing ward ratios
  silently drops or misattributes them — fixed by clustering each name's
  OSM ways by shared node IDs first), and the gap-fill/unnamed-road
  handling in `fill_gaps.py` + `add_unnamed_roads.py`. Copy `template/`
  rather than reconstructing pipeline logic from first principles or from
  an older constituency folder.
- `build_tracker.py` (step 10) is cheap/fast — no need to background it.
