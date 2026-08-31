# leaflet-pipeline

The setup/build tooling behind the [leaflet-map](https://github.com/Daemeous/leaflet-map) canvassing-tracker family and [stafford-potholes](https://github.com/Daemeous/stafford-potholes). The site repos contain nothing but the frontend (`index.html` + shared `core.js`/`styles.css`) — everything that builds a constituency's road/residence data and creates its Google Sheet + Apps Script backend lives here instead.

**Not included** (too large / licensed, kept local only): `Boundary-Line.gpkg` (~1.8GB, [OS Data Hub](https://osdatahub.os.uk/downloads/open/BoundaryLine)) and `osopenuprn_*.csv` (~2.2GB, [OS Open UPRN](https://osdatahub.os.uk/downloads/open/OpenUPRN)), both Open Government Licence. Put them in a `data/` folder alongside `template/`.

---

## Live deployments this pipeline builds

**Leafletting Map:**

| Constituency / area | Site |
|---|---|
| Stafford | https://daemeous.github.io/leaflet-map/ |
| Demo | https://daemeous.github.io/leaflet-map-demo/ |
| South Hams | https://daemeous.github.io/south-hams/ |
| Burton & Uttoxeter | https://daemeous.github.io/burton-uttoxeter/ |
| Stone, Great Wyrley & Penkridge | https://daemeous.github.io/stone/ |
| Barnsley, Penistone & Stocksbridge | https://daemeous.github.io/barnsley/ |
| St Helens | https://daemeous.github.io/sthelens/ |
| Shipley + Keighley and Ilkley | https://daemeous.github.io/shipley/ |

**Pothole Watch:**

| Area | Site |
|---|---|
| Stafford | https://daemeous.github.io/stafford-potholes/ |

---

## Repository contents

| Path | Purpose |
|---|---|
| `WORKFLOW.md` | The full step-by-step for building a leafletting spreadsheet for a new constituency, ward-name verification pitfalls, refreshing an existing constituency, and a running log of bugs already found and fixed — **read this before touching the pipeline**, it exists specifically so mistakes aren't repeated |
| `template/constituency_config.py` | Fill-in-the-blanks config for one constituency/area (wards, Boundary-Line `File_Name`, parish exclusions, authorised editor emails) |
| `template/run_pipeline.py` | Fetches ward boundaries + OSM roads, clips roads to wards, writes `<prefix>_Leafletting.xlsx` |
| `template/fill_gaps.py` | Recovers roads OSM's `way[highway][name]` query misses (split/unnamed segments) and reports genuinely unnamed streets for manual naming |
| `template/estimate_residences_uprn.py` | UPRN-buffer residence counting per road — the only method verified not to silently undercount (see `WORKFLOW.md` step 7 for why the OSM-building-footprint approach was abandoned) |
| `template/add_unnamed_roads.py` | Appends unnamed-road clusters as placeholder rows |
| `template/build_tracker.py` | Builds the full Google-Sheets-ready tracker workbook (`Data`/`Dashboard`/`Changelog`/`Checksum`/`Authorised` sheets, dashboard charts) from the pipeline's output |
| `template/finalize_output.py` | Merges a fresh pipeline run into an existing constituency's *live* tracker without disturbing in-flight `Status`/row-position data — use this instead of re-running `estimate_residences_uprn.py` directly on a constituency with real progress |
| `cluster_potholes.py` | Batch job for Pothole Watch: snaps reports to the road network, groups nearby reports into repair sections, ranks them by priority |
| `apps-script/leaflet-map.gs.txt` | Apps Script backend for a leafletting-map deployment (status writes, pending-change queue, editor revert/history) |
| `apps-script/pothole-watch.gs.txt` | Apps Script backend for a Pothole Watch deployment (public report submission with Drive photo upload, admin status/cluster updates) |

---

## Setting up a new deployment's Sheet + Apps Script backend

This is the part that used to be done by hand (open Apps Script editor, paste code, Deploy → New deployment → …). It's now driven by Google's [`clasp`](https://github.com/google/clasp) CLI end to end except for two steps Google doesn't expose an API for.

**One-time setup (per machine):**
```bash
npm install -g @google/clasp
clasp login          # opens a browser OAuth prompt
```
The Google account you sign in as must have the Apps Script API turned on once, at https://script.google.com/home/usersettings (`clasp` will tell you if it isn't, with a link).

**Per new deployment:**
```bash
mkdir my-deployment && cd my-deployment

# 1. Create the Sheet + a bound Apps Script project together
clasp create --type sheets --title "My Deployment Name"
#    -> prints the new Spreadsheet's Drive URL and the script's editor URL

# 2. Set web-app deployment config in the generated appsscript.json:
#    { "webapp": { "executeAs": "USER_DEPLOYING", "access": "ANYONE_ANONYMOUS" }, ... }

# 3. Copy the relevant apps-script/*.gs.txt content in as Code.gs, then:
clasp push
clasp deploy --description "initial"
#    -> prints a deployment ID; the web app's exec URL is
#       https://script.google.com/macros/s/<deploymentId>/exec

# 4. Updating later (same exec URL, no re-share needed):
clasp push
clasp deploy --deploymentId <the same id> --description "..."
```

**Manual steps that remain** (both are one-off per new deployment, not per update):
1. **Publish to web**: open the Sheet → File → Share → Publish to web → format CSV → Publish. This is what produces the `docs.google.com/spreadsheets/d/e/<id>/pub?...` URL `index.html`'s `SHEET_ID` expects — the Drive API technically has a `revisions.update({published: true})` call that can do this, but flipping a file to publicly readable isn't something this pipeline calls automatically; do it by hand.
2. **Google sign-in origin**: if the new deployment lives on a GitHub Pages origin (`https://<user>.github.io`) already authorized for other deployments' OAuth client, sign-in works immediately — origins are checked at the scheme+host level, not per-path. A genuinely new origin needs adding under the OAuth client's "Authorized JavaScript origins" in Cloud Console.

Both Apps Script templates default `GOOGLE_CLIENT_ID` to the existing shared OAuth client and (Pothole Watch only) lazily create their own Drive photos folder, specifically so a fresh deployment works without a Script Properties paste — see the comments at the top of each `.gs.txt` file.

Reading a deployment's sheet gids without opening the Sheets UI: `pothole-watch.gs.txt` exposes a `sheetInfo` POST action that returns `{spreadsheetId, reportsGid, clustersGid}` — useful for scripting the `index.html` fill-in step right after first deploy.

---

## License

This project's own code (the pipeline scripts and Apps Script templates) is licensed under the **[PolyForm Noncommercial License 1.0.0](LICENSE)**: free to use, share, and modify for any non-commercial purpose, with attribution. See [`LICENSE`](LICENSE) for the full text.

Copyright © Daniel Hodgkins.

That covers this code only. The geographic data it processes comes from sources under their own separate licenses that explicitly permit commercial use (see Attributions below) — this project's non-commercial restriction doesn't, and legally can't, extend to that underlying data.

## Attributions

| Dependency | License | Notes |
|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org/copyright) | [ODbL](https://opendatacommons.org/licenses/odbl/) | Road data, fetched via the Overpass API. Permits commercial use; requires attribution and share-alike for derivative databases. |
| OS Boundary-Line & OS Open UPRN | [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) | © Crown copyright and database right, Ordnance Survey. Permits commercial use; requires attribution. Not redistributed in this repo — see "Not included" above. |
| [Overpass API](https://overpass-api.de) | [Usage policy](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html) | OSM data queries |
| [GeoPandas](https://geopandas.org) / [Shapely](https://shapely.readthedocs.io) / [pyproj](https://pyproj4.github.io/pyproj/) | BSD-3-Clause | Geospatial processing |
| [pandas](https://pandas.pydata.org) / [NumPy](https://numpy.org) | BSD-3-Clause | Data handling |
| [openpyxl](https://openpyxl.readthedocs.io) | MIT | Excel/Sheets-ready workbook output |
| [Requests](https://requests.readthedocs.io) | Apache-2.0 | HTTP calls (Overpass, Boundary-Line lookups) |
| [OSMnx](https://osmnx.readthedocs.io) | MIT | Referenced by earlier pipeline iterations |
| [clasp](https://github.com/google/clasp) | Apache-2.0 | Google's CLI, used (not bundled) to create/push/deploy the Apps Script backends |
| Google Sheets, Drive & Apps Script | [Google Terms of Service](https://policies.google.com/terms) | The hosted backend every deployment reads/writes |
