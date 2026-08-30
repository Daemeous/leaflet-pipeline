"""
constituency_config.py  (TEMPLATE)
====================================
Fill this in for a new constituency, then run:
    python run_pipeline.py
    python estimate_residences_uprn.py

See ../WORKFLOW.md for the full process, including how to figure out
`boundary_line_file_name` and `district_filter`, and when you need
`parish_exclusions`.

FIELD NOTES
-----------
display_name / output_prefix
    display_name is just for printout. output_prefix drives every output
    filename (<prefix>_Leafletting.xlsx etc) — keep it short, no spaces.

boundary_line_file_name
    The Boundary-Line `File_Name` value covering this area's wards/parishes.
    Find it with:
        import sqlite3
        con = sqlite3.connect("data/Boundary-Line.gpkg")
        con.execute("SELECT DISTINCT File_Name FROM district_borough_unitary_ward "
                     "WHERE File_Name LIKE '%YOURDISTRICT%'").fetchall()
    Counties (e.g. Staffordshire) group ALL their districts' wards under one
    File_Name. Unitary/metropolitan boroughs (e.g. St Helens) usually have
    their own File_Name per borough.
    Can also be a LIST of File_Names, for an area spanning several unitaries/
    boroughs plus a county (e.g. a branch covering wards from a city unitary,
    a county's districts, AND a separate borough unitary all at once) —
    e.g. ["CITY_OF_PLYMOUTH_(B)", "DEVON_COUNTY", "TORBAY_(B)"]. Check for
    ward-name collisions across the combined set first (count matches per
    name) before relying on this without district_filter.

district_filter
    Used ONLY to disambiguate ward names that aren't unique within the
    File_Name (e.g. "Town Ward" exists in several districts). Set it to a
    prefix of the parent district's name in the `district_borough_unitary`
    layer. If every ward name in your list is already unique within
    boundary_line_file_name, this still doesn't hurt to set.

wards
    List of (ward_name_as_it_appears_administratively, district_label).
    The " Ward" suffix is tried automatically if the bare name doesn't
    match — you don't need to add it yourself.
    IMPORTANT: verify the supplied ward list against Boundary-Line BEFORE
    running anything (see WORKFLOW.md "Ward name pitfalls"). A branch/user
    may hand you pre-reorganisation ward names that no longer exist.

parish_exclusions
    {ward_display_name: [civil_parish_name, ...]}. Use when a ward is only
    PARTIALLY in the constituency and the excluded part is a whole civil
    parish (check the `parish` layer, same File_Name). Leave as {} if no
    ward needs this.

authorised_emails
    Used only by build_tracker.py, which runs after estimate_residences_uprn.py
    and adds the Dashboard/Changelog/Checksum/Authorised sheets needed to
    host the result as a Google Sheet. List of email addresses that go on
    the Authorised sheet (accounts allowed to bypass the bound Apps Script's
    edit locks). Leave as [] if there's no Apps Script for this one yet.
"""

ACTIVE_CONSTITUENCY = "REPLACE_ME"

CONSTITUENCIES = {
    "REPLACE_ME": {
        "display_name":  "Replace With Full Constituency Name",
        "output_prefix": "ReplaceMe",

        "district_filter": "Replace With District Name Prefix",
        "boundary_line_file_name": "REPLACE_WITH_FILE_NAME",

        "bbox": None,  # auto-computed from matched ward geometries — leave as None

        "wards": [
            ("Ward One",  "District Name"),
            ("Ward Two",  "District Name"),
        ],

        "parish_exclusions": {
            # "Ward Name": ["Parish Name"],
        },

        "authorised_emails": [],
    },
}


def get_config(key=None):
    k = key or ACTIVE_CONSTITUENCY
    if k not in CONSTITUENCIES:
        raise ValueError(f"Unknown constituency '{k}'. Known keys: {list(CONSTITUENCIES)}")
    return CONSTITUENCIES[k]
