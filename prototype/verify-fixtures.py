#!/usr/bin/env python3
"""Verify the generated fixture library against exact expected counts.

This is the seed of the §13 verification harness. It asserts *exact* equality
(counts, item names) rather than approximate matches, and it deliberately
exercises the delimiter trap from §3.

Run after setup-server.py.
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.parse
import urllib.request

CREDS = json.loads((pathlib.Path(__file__).parent / "out" / "credentials.json").read_text())
BASE, TOKEN, USER_ID = CREDS["base"], CREDS["token"], CREDS["userId"]

FAILURES: list[str] = []


def api(path: str, **params):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url)
    req.add_header("Authorization", f'MediaBrowser Token="{TOKEN}"')
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def count(**params) -> int:
    """TotalRecordCount for an /Items query scoped to the Movies library."""
    params.setdefault("parentId", LIB_ID)
    params.setdefault("recursive", "true")
    params.setdefault("includeItemTypes", "Movie")
    return api("/Items", **params).get("TotalRecordCount", 0)


def check(label: str, actual, expected) -> None:
    ok = actual == expected
    if not ok:
        FAILURES.append(f"{label}: got {actual!r}, expected {expected!r}")
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {actual!r}" + ("" if ok else f" (expected {expected!r})"))


# --- locate the library -------------------------------------------------
folders = api("/Library/VirtualFolders")
LIB_ID = next(f["ItemId"] for f in folders if f["Name"] == "Movies")
print(f"Movies library id: {LIB_ID}\n")

# --- 1. what is actually in the library? --------------------------------
# NOTE: limit=0 does NOT mean "unlimited" in 12.1 — it returns TotalRecordCount
# but ZERO items. Use a real limit to enumerate. (See the limit=0 assertion in
# section 6 and docs/FINDINGS.md.)
print("1. Library contents")
items = api("/Items", parentId=LIB_ID, recursive="true", limit="100",
            includeItemTypes="Movie", fields="Path,Genres")
print(f"   TotalRecordCount = {items.get('TotalRecordCount')}")
rows = items.get("Items", [])
print(f"   Items returned   = {len(rows)}")
for it in sorted(rows, key=lambda x: x.get("Name", "")):
    print(f"     - {it.get('Name')!r:24} genres={it.get('Genres')}")

names = sorted(it.get("Name") for it in rows)
EXPECTED_NAMES = sorted([
    "Alpha Beta One", "Alpha Beta Two", "Alpha Gamma",
    "Beta Gamma", "All Three", "Gamma Delta",
])
print()
check("library item count", len(rows), 6)
check("library item names", names, EXPECTED_NAMES)

# --- 2. genre inventory + ids ------------------------------------------
print("\n2. Genre inventory")
genres = api("/Genres", parentId=LIB_ID, userId=USER_ID)
gid = {g["Name"]: g["Id"] for g in genres.get("Items", [])}
for name in sorted(gid):
    print(f"     {name:8} {gid[name]}")
check("genre set", sorted(gid), ["Alpha", "Beta", "Delta", "Gamma"])

# --- 3. OR vs AND semantics (§2) ---------------------------------------
print("\n3. OR vs AND semantics")
check("genres=Alpha                    (single)", count(genres="Alpha"), 4)
check("genres=Beta                     (single)", count(genres="Beta"), 4)
check("genres=Gamma                    (single)", count(genres="Gamma"), 4)
check("genres=Alpha|Beta               (OR / union)", count(genres="Alpha|Beta"), 5)
check("genres=Alpha&genreIds=Beta      (AND / intersect)",
      count(genres="Alpha", genreIds=gid["Beta"]), 3)
check("genres=Alpha&genreIds=Gamma     (AND / intersect)",
      count(genres="Alpha", genreIds=gid["Gamma"]), 2)

# --- 4. the delimiter trap (§3) ----------------------------------------
print("\n4. Delimiter trap — genreIds must be COMMA separated")
comma = count(genreIds=f"{gid['Alpha']},{gid['Beta']}")
print(f"   genreIds=Alpha,Beta (comma)  -> {comma}")
check("genreIds=A,B is a union", comma, 5)

# A pipe here makes the GUID parse fail and the server SILENTLY drops the
# whole filter, returning an unfiltered page. Assert that failure mode exists
# so a regression that "fixes" it silently is still caught.
unfiltered = count()
piped = count(genreIds=f"{gid['Alpha']}|{gid['Beta']}")
print(f"   unfiltered                   -> {unfiltered}")
print(f"   genreIds=Alpha|Beta (pipe)   -> {piped}")
check("genreIds with pipe is silently dropped (== unfiltered)", piped, unfiltered)

# --- 5. the ceiling (§3): 2 groups max via raw API ----------------------
print("\n5. The 2-group ceiling")
check("Beta AND Gamma                 == 2",
      count(genres="Beta", genreIds=gid["Gamma"]), 2)
# {Alpha Gamma, Beta Gamma, All Three} = 3.
# "Gamma Delta" is excluded: it has neither Alpha nor Beta.
check("(Alpha|Beta) AND (Gamma|Delta)  == 3",
      count(genres="Alpha|Beta", genreIds=f"{gid['Gamma']},{gid['Delta']}"), 3)
print("   Alpha AND Beta AND Gamma would need 3 groups; the API has only")
print("   `genres` and `genreIds`, so its best-effort is (A OR B) AND C = "
      f"{count(genres='Alpha|Beta', genreIds=gid['Gamma'])}")
print("   true 3-way intersection = 1  ({'All Three'})  <- the plugin's reason to exist")

# --- 6. pagination bounds (§7) -----------------------------------------
print("\n6. Pagination bounds")
check("limit=2 returns 2 items", len(api("/Items", parentId=LIB_ID, recursive="true",
      limit="2", includeItemTypes="Movie").get("Items", [])), 2)

# §7 warns "do not assume limit=0 means unlimited". Measured on 12.1:
# limit=0 still computes TotalRecordCount but returns NO items. This is the
# likely explanation for the unexplained unfiltered-with-parentId baseline
# (1,356 vs ~5,614) recorded as an open question in the kickoff.
zero = api("/Items", parentId=LIB_ID, recursive="true", limit="0", includeItemTypes="Movie")
check("limit=0 TotalRecordCount is still correct", zero.get("TotalRecordCount"), 6)
check("limit=0 returns NO items (not 'unlimited')", len(zero.get("Items") or []), 0)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
