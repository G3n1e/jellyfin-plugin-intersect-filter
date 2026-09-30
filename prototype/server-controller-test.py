#!/usr/bin/env python3
"""Verify the server-side N-way intersection against ground truth.

FINDINGS section 5 proved the 3-group intersection in the browser, with the
intersection computed client-side. This harness verifies the same claim against
the *server* endpoint the plugin ships, where it belongs.

Ground truth comes from prototype/make-fixtures.sh, and is re-derived from the
stock API at run time rather than hardcoded where possible, so a stale expectation
cannot make a wrong answer pass:

    Alpha = {Alpha Beta One, Alpha Beta Two, Alpha Gamma, All Three}   (4)
    Beta  = {Alpha Beta One, Alpha Beta Two, Beta Gamma, All Three}   (4)
    Gamma = {Alpha Gamma, Beta Gamma, Gamma Delta, All Three}         (4)
    Alpha AND Beta AND Gamma = {All Three}                            (1)

The stock API can express (Alpha|Beta) AND Gamma = 3 but NOT Alpha AND Beta AND
Gamma = 1 (FINDINGS section 2). That gap is the product.

Beyond the happy path this asserts the four hazards the kickoff names:

  * the delimiter asymmetry - '|' groups names (OR), it is not a list separator
  * AND vs OR per category
  * empty-means-all: an empty selection must NOT silently become "match nothing",
    and an empty *intersection* must NOT silently become "match everything"
  * pagination bounds: a page must report the count of the whole intersection,
    not the size of the page

It also asserts that malformed input is rejected rather than ignored, because a
silently dropped filter is the failure mode this plugin exists to remove.

Usage:  python3 server-controller-test.py
Needs:  prototype/out/credentials.json (written by setup-server.py) and the
        plugin deployed (prototype/deploy-plugin.sh).
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "out"
CREDS = json.loads((OUT / "credentials.json").read_text())
BASE = CREDS["base"]
TOKEN = CREDS["token"]
USER_ID = CREDS["userId"]
LIB = "f137a2dd21bbc1b99aa5c0f6bf02a805"

ALL_THREE = "All Three"
AB1, AB2 = "Alpha Beta One", "Alpha Beta Two"
AG, BG, GD = "Alpha Gamma", "Beta Gamma", "Gamma Delta"

checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    checks.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def get(path: str, params: list[tuple[str, str]] | None = None) -> tuple[int, dict]:
    """GET with the user token. Returns (status, parsed-json-or-{})."""
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url)
    req.add_header("Authorization", f'MediaBrowser Token="{TOKEN}"')
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body}


# --------------------------------------------------------------------------
# thin wrappers for the two endpoints under comparison
# --------------------------------------------------------------------------

def ours(groups: list[str], **kw) -> dict:
    """Call the plugin endpoint. `groups` is one entry per AND-group."""
    params: list[tuple[str, str]] = [
        ("parentId", kw.pop("parentId", LIB)),
        ("includeItemTypes", kw.pop("includeItemTypes", "Movie")),
        ("limit", str(kw.pop("limit", 100))),
    ]
    if "startIndex" in kw:
        params.append(("startIndex", str(kw.pop("startIndex"))))
    params += [("groups", g) for g in groups]
    params += [(k, str(v)) for k, v in kw.items()]
    status, body = get("/BetterJellyfinFiltering/Intersect", params)
    body["_status"] = status
    return body


def stock(**kw) -> dict:
    """Call the stock /Items endpoint with the same scoping."""
    params: list[tuple[str, str]] = [
        ("userId", USER_ID),
        ("parentId", kw.pop("parentId", LIB)),
        ("recursive", "true"),
        ("includeItemTypes", kw.pop("includeItemTypes", "Movie")),
        ("limit", str(kw.pop("limit", 100))),
    ]
    params += [(k, str(v)) for k, v in kw.items()]
    status, body = get("/Items", params)
    body["_status"] = status
    return body


def names(result: dict) -> list[str]:
    return sorted(i["Name"] for i in (field(result, "Items") or []))


def total(result: dict) -> int:
    return field(result, "TotalRecordCount", -1)


def field(result: dict, key: str, default=None):
    """Read a response field case-insensitively.

    Jellyfin's REST API serialises PascalCase (the stock /Items returns
    `TotalRecordCount` and `Items`), so the plugin matches that. Reading
    case-insensitively keeps this harness from breaking on that detail rather
    than on a real regression.
    """
    if key in result:
        return result[key]
    for k, v in result.items():
        if k.lower() == key.lower():
            return v
    return default


# --------------------------------------------------------------------------

def main() -> int:
    print("setup: confirm the server is the plugin host and the token works")
    status, me = get("/Users/Me")
    check("user token authenticates", status == 200, f"status={status}")
    if status != 200:
        return 1
    print(f"   signed in as {me.get('Name')} ({me.get('Id')})")

    # ---- 0. ground truth from the stock API -----------------------------
    print("\n0. ground truth, re-derived from the stock API")
    base = stock()
    check("stock unfiltered sees the 6 fixtures", total(base) == 6,
          f"got {total(base)}: {names(base)}")

    truth: dict[str, set[str]] = {}
    for g in ("Alpha", "Beta", "Gamma", "Delta"):
        r = stock(genres=g)
        truth[g] = set(names(r))
        print(f"   {g:6} = {sorted(truth[g])}")
    check("fixture shape is the expected one",
          truth["Alpha"] == {AB1, AB2, AG, ALL_THREE}
          and truth["Beta"] == {AB1, AB2, BG, ALL_THREE}
          and truth["Gamma"] == {AG, BG, GD, ALL_THREE},
          "genre overlaps match make-fixtures.sh")

    def expected(*gs: str) -> set[str]:
        out = set(truth[gs[0]])
        for g in gs[1:]:
            out &= truth[g]
        return out

    # ---- 1. the thesis: a 3-way intersection ----------------------------
    print("\n1. the 3-group intersection — not expressible via the stock API")
    r = ours(["Alpha", "Beta", "Gamma"])
    check("endpoint returns 200", r["_status"] == 200, f"got {r['_status']}")
    check("Alpha AND Beta AND Gamma = 1", total(r) == 1, f"got {total(r)}: {names(r)}")
    check("...and it is 'All Three'", names(r) == [ALL_THREE], f"got {names(r)}")
    check("...matching the independently computed intersection",
          set(names(r)) == expected("Alpha", "Beta", "Gamma"))

    # the stock API cannot express this: prove the gap in the same run
    stock_union = stock(genres="Alpha|Beta|Gamma")
    check("stock union of the same genres = 6 (the whole library)",
          total(stock_union) == 6, f"got {total(stock_union)}")
    check("so the same three genres go 6 (union) vs 1 (intersection)",
          total(stock_union) == 6 and total(r) == 1)

    # ---- 2. pairwise, and the 2-group ceiling ---------------------------
    print("\n2. pairwise cases and the stock ceiling")
    for a, b, want in (("Alpha", "Beta", 3), ("Alpha", "Gamma", 2), ("Beta", "Gamma", 2)):
        got = ours([a, b])
        check(f"{a} AND {b} = {want}", total(got) == want, f"got {total(got)}: {names(got)}")
        check(f"{a} AND {b} has the right members",
              set(names(got)) == expected(a, b), f"got {names(got)}")

    try:
        gamma_id = _genre_id("Gamma")
    except RuntimeError as e:
        check("stock 2-group ceiling reproduced via the stock API", False, str(e))
    else:
        ceiling = stock(genres="Alpha|Beta", genreIds=gamma_id)
        check("stock 2-group ceiling (Alpha|Beta) AND Gamma = 3",
              total(ceiling) == 3, f"got {total(ceiling)}")

    # the plugin expresses the ceiling case with one query shape
    same = ours(["Alpha|Beta", "Gamma"])
    check("the plugin expresses that same case as groups=Alpha|Beta&groups=Gamma",
          total(same) == 3, f"got {total(same)}: {names(same)}")

    # ---- 3. '|' means OR within a group ---------------------------------
    print("\n3. '|' inside a group ORs; separate groups AND")
    one_group = ours(["Alpha|Beta|Gamma"])
    check("groups=Alpha|Beta|Gamma (one group) = union = 6",
          total(one_group) == 6, f"got {total(one_group)}")
    check("...and that equals the stock union",
          set(names(one_group)) == set(names(stock_union)))

    # ---- 4. empty intersection is a real, empty answer ------------------
    # This is the landmine: the plugin pages the intersected id set, and an empty
    # id array handed to the query layer disables the id filter entirely rather
    # than matching nothing (TranslateQuery guards `if (filter.ItemIds.Length > 0)`).
    # A regression there returns the WHOLE LIBRARY while reporting a total of 0.
    print("\n4. an empty intersection is empty, not the whole library")
    four = ours(["Alpha", "Beta", "Gamma", "Delta"])
    check("4-way intersection = 0", total(four) == 0, f"got {total(four)}")
    check("...and returns NO items (not the entire library)",
          len(field(four, "Items") or []) == 0, f"got {names(four)}")
    check("...while still reporting the filter was applied",
          field(four, "FilterApplied") is True)

    # ---- 5. empty selection means 'no filter', not 'match nothing' ------
    print("\n5. empty selection: 'no filter', stated explicitly")
    none = ours([])
    check("no groups returns the unfiltered library", total(none) == 6,
          f"got {total(none)}")
    check("...and says plainly that no filter was applied",
          field(none, "FilterApplied") is False)
    check("...and carries a warning so a client cannot misread it",
          bool(field(none, "Warnings")), f"warnings={field(none, "Warnings")}")

    # ---- 6. pagination bounds -------------------------------------------
    # A page must report the size of the whole intersection. Reporting the page
    # size instead is the classic way this looks correct and is not.
    print("\n6. pagination reports the whole intersection, not the page")
    p1 = ours(["Alpha", "Beta"], limit=1, startIndex=0)
    check("limit=1 returns 1 item", len(field(p1, "Items") or []) == 1,
          f"got {names(p1)}")
    check("...but TotalRecordCount is still 3", total(p1) == 3, f"got {total(p1)}")

    p2 = ours(["Alpha", "Beta"], limit=1, startIndex=1)
    check("startIndex=1 returns a different item", len(field(p2, "Items") or []) == 1
          and names(p2) != names(p1), f"page0={names(p1)} page1={names(p2)}")
    check("...with the same total", total(p2) == 3, f"got {total(p2)}")

    paged = set(names(p1)) | set(names(p2))
    p3 = ours(["Alpha", "Beta"], limit=1, startIndex=2)
    paged |= set(names(p3))
    check("paging over the intersection yields the full set exactly once",
          paged == expected("Alpha", "Beta"), f"got {sorted(paged)}")

    # ---- 7. order independence ------------------------------------------
    print("\n7. criterion order does not change the answer")
    rev = ours(["Gamma", "Beta", "Alpha"])
    check("reversed criterion order gives the same result",
          set(names(rev)) == set(names(r)), f"got {names(rev)}")
    check("...and reports one statistic per criterion",
          len(field(rev, "Criteria") or []) == 3, f"got {field(rev, "Criteria")}")

    # the narrowing is visible: each criterion reports its own match count and
    # the running intersection, which is how a reviewer sees the AND happening
    crit = (field(r, "Criteria") or [])
    check("criteria show the intersection narrowing monotonically",
          all(field(crit[i], "RunningIntersection") >= field(crit[i + 1], "RunningIntersection")
              for i in range(len(crit) - 1)),
          f"got {[(field(c, 'Matched'), field(c, 'RunningIntersection')) for c in crit]}")

    # ---- 8. malformed input is rejected, never ignored -------------------
    print("\n8. malformed input is an error, never a silently dropped filter")
    bad = ours(["Alpha||Beta"])
    check("an empty name inside a group is rejected", bad["_status"] == 400,
          f"status={bad['_status']}")
    check("...with a message naming the problem",
          any("groups" in e.lower() for e in (field(bad, "Errors") or [])),
          f"got {field(bad, 'Errors')}")

    st, body = get("/BetterJellyfinFiltering/Intersect",
                   [("parentId", LIB), ("groups", "Alpha"), ("limit", "0")])
    check("limit=0 is rejected rather than treated as unlimited", st == 400,
          f"status={st}")

    st, body = get("/BetterJellyfinFiltering/Intersect",
                   [("parentId", LIB), ("groups", "Alpha"), ("sortOrder", "Sideways")])
    check("an unknown sortOrder is rejected", st == 400, f"status={st}")

    st, body = get("/BetterJellyfinFiltering/Intersect",
                   [("parentId", LIB), ("groups", "Alpha"), ("includeItemTypes", "NotAType")])
    check("an unknown includeItemTypes is rejected rather than dropped", st == 400,
          f"status={st}")

    st, body = get("/BetterJellyfinFiltering/Intersect",
                   [("parentId", LIB), ("groups", "Alpha|")])
    check("a trailing '|' is rejected rather than trimmed away", st == 400, f"status={st}")

    # A parent that does not exist must not be treated as "no parent": that would answer a
    # question about one folder with the whole library.
    st, body = get("/BetterJellyfinFiltering/Intersect",
                   [("parentId", "deadbeefdeadbeefdeadbeefdeadbeef"), ("groups", "Alpha")])
    check("an unresolvable parentId is rejected, not ignored", st == 400, f"status={st}")

    # ---- 9. unauthenticated access is refused ---------------------------
    print("\n9. authorization")
    req = urllib.request.Request(
        f"{BASE}/BetterJellyfinFiltering/Intersect?parentId={LIB}&groups=Alpha")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            st = resp.status
    except urllib.error.HTTPError as e:
        st = e.code
    check("an unauthenticated request is refused", st in (401, 403), f"status={st}")

    # a signed-in caller must not be able to read another user's view
    st, body = get("/BetterJellyfinFiltering/Intersect",
                   [("parentId", LIB), ("groups", "Alpha"),
                    ("userId", "00000000000000000000000000000000")])
    check("a signed-in caller cannot request another user's view", st == 403,
          f"status={st}")

    # ---- report ---------------------------------------------------------
    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("\nfailures:")
        for label, _, detail in failed:
            print(f"  - {label}" + (f"  ({detail})" if detail else ""))
    return 1 if failed else 0


def _genre_id(name: str) -> str:
    """Look up a genre's real GUID, needed for the stock genreIds parameter."""
    status, body = get("/Genres", [("userId", USER_ID), ("parentId", LIB),
                                   ("includeItemTypes", "Movie")])
    for g in body.get("Items", []):
        if g.get("Name") == name:
            return g["Id"]
    raise RuntimeError(f"genre {name!r} not found")


if __name__ == "__main__":
    sys.exit(main())
