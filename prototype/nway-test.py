#!/usr/bin/env python3
"""Prove the N-way intersection — the entire justification for the plugin.

FINDINGS §2 established the ceiling: /Items ANDs across `genres` and `genreIds`
and those are the only two genre groups, so `(A|B) AND (C|D)` is the most the
raw API can express. `Alpha AND Beta AND Gamma` is not expressible at all. This
harness checks that the injected payload computes it anyway, and that the answer
is right rather than merely plausible.

Ground truth comes from prototype/make-fixtures.sh:

    Alpha            = {Alpha Beta One, Alpha Beta Two, Alpha Gamma, All Three}
    Beta             = {Alpha Beta One, Alpha Beta Two, Beta Gamma, All Three}
    Gamma            = {Alpha Gamma, Beta Gamma, Gamma Delta, All Three}
    Alpha AND Beta AND Gamma = {All Three}          (1)
    Alpha AND Beta           = {.. One, .. Two, All Three}  (3)
    (Alpha|Beta) AND Gamma   = 3    <- the best the raw API can do

Also exercises the trap that makes a naive implementation silently wrong:
intersecting *pages* instead of *sets*. It resolves Alpha AND Beta twice, once
with a limit that truncates a genre set and once without, and asserts the
truncated answer is both wrong and flagged.
"""

from __future__ import annotations

import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "out"
INJECT = (ROOT / "inject.js").read_text()
CREDS = json.loads((OUT / "credentials.json").read_text())
BASE, USER, PASSWORD = CREDS["base"], CREDS["user"], CREDS["password"]
LIB = "f137a2dd21bbc1b99aa5c0f6bf02a805"

ALL_THREE = "All Three"
AB1, AB2 = "Alpha Beta One", "Alpha Beta Two"
AG = "Alpha Gamma"

checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    checks.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def names(res: dict) -> list[str]:
    return sorted(i["name"] for i in (res.get("items") or []))


def main() -> int:
    results: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        logs: list[str] = []
        page.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: logs.append(f"PAGEERROR: {e}"))

        def route_handler(route):
            url = route.request.url
            if url.endswith("/proto-inject.js"):
                route.fulfill(status=200, content_type="application/javascript", body=INJECT)
            elif url.rstrip("/").endswith("/web/index.html") or url.rstrip("/").endswith("/web"):
                resp = route.fetch()
                body = resp.body().decode("utf-8", errors="replace")
                patched = body.replace(
                    "</body>", '<script defer src="/proto-inject.js"></script></body>', 1)
                headers = {k: v for k, v in resp.headers.items()
                           if k.lower() not in ("content-encoding", "content-length",
                                                "etag", "last-modified", "vary")}
                route.fulfill(status=resp.status, headers=headers, body=patched)
            else:
                route.continue_()

        ctx.route("**/*", route_handler)

        print("setup: login + open Movies library")
        page.goto(f"{BASE}/web/", wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(3000)
        for sel in ['#txtManualName', 'input[name="username"]']:
            if page.locator(sel).count():
                page.fill(sel, USER)
                break
        for sel in ['#txtManualPassword', 'input[type="password"]']:
            if page.locator(sel).count():
                page.fill(sel, PASSWORD)
                break
        for sel in ['button[type="submit"]', 'button:has-text("Sign In")', '.btnSubmit']:
            if page.locator(sel).count():
                page.click(sel)
                break
        page.wait_for_timeout(5000)
        page.goto(f"{BASE}/web/#/movies?topParentId={LIB}", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

        page.evaluate("(lib) => { window.__JF_LIB__ = lib; }", LIB)
        ready = page.evaluate("() => typeof window.__JF_RESOLVE__ === 'function'")
        check("resolve hook present in page", ready)
        if not ready:
            print("  console:", logs[-10:])
            browser.close()
            return 1

        def resolve(genres, limit=None):
            opts = {"limit": limit} if limit else {}
            return page.evaluate(
                "([g,o]) => window.__JF_RESOLVE__(g,o)", [genres, opts])

        # ---- 1. the baseline pairwise cases --------------------------------
        print("\n1. pairwise intersections (sanity against ground truth)")
        ab = resolve(["Alpha", "Beta"])
        results["alpha_and_beta"] = ab
        check("Alpha AND Beta = 3", len(ab.get("items") or []) == 3,
              f"got {names(ab)}")
        check("Alpha AND Beta has the right members",
              names(ab) == sorted([AB1, AB2, ALL_THREE]), f"got {names(ab)}")
        check("no genre set reported truncated at the default limit",
              ab.get("truncated") is False, f"perGenre={ab.get('perGenre')}")

        ag = resolve(["Alpha", "Gamma"])
        results["alpha_and_gamma"] = ag
        check("Alpha AND Gamma = 2", names(ag) == sorted([AG, ALL_THREE]),
              f"got {names(ag)}")

        # ---- 2. THE THESIS: the 3-group case -------------------------------
        print("\n2. the 3-group case — not expressible via the raw API")
        three = resolve(["Alpha", "Beta", "Gamma"])
        results["three_way"] = three
        check("Alpha AND Beta AND Gamma = 1", len(three.get("items") or []) == 1,
              f"got {names(three)}")
        check("...and it is 'All Three'", names(three) == [ALL_THREE],
              f"got {names(three)}")

        four = resolve(["Alpha", "Beta", "Gamma", "Delta"])
        results["four_way"] = four
        check("Alpha AND Beta AND Gamma AND Delta = 0 (empty is a real answer)",
              len(four.get("items") or []) == 0, f"got {names(four)}")

        # ---- 3. contrast with what the stock API can express ---------------
        # Alpha, Beta and Gamma each hold 4 of the 6 fixtures, and between them
        # they cover the whole library (Gamma Delta has Gamma). So the native
        # union shows 6 — everything — while the intersection is 1. That gap is
        # the product.
        print("\n3. contrast: what the stock API can express")
        ceiling = page.evaluate(
            """async () => {
                const c = window.ApiClient;
                const uid = c.getCurrentUserId();
                const q = (s) => c.getJSON(c.getUrl(
                    '/Items?userId=' + uid + '&recursive=true&includeItemTypes=Movie' +
                    '&enableImages=false&limit=100' +
                    (window.__JF_LIB__ ? '&parentId=' + window.__JF_LIB__ : '') + '&' + s));
                const out = {};
                const orAll = await q('genres=' + encodeURIComponent('Alpha|Beta|Gamma'));
                out.orAllCount = orAll.TotalRecordCount;
                out.orAllNames = (orAll.Items||[]).map(i=>i.Name).sort();

                // genreIds needs real GUIDs; /Genres supplies them.
                try {
                    const gs = await c.getJSON(c.getUrl(
                        '/Genres?userId=' + uid + '&includeItemTypes=Movie' +
                        (window.__JF_LIB__ ? '&parentId=' + window.__JF_LIB__ : '')));
                    const gamma = (gs.Items||[]).find(g => g.Name === 'Gamma');
                    out.gammaId = gamma ? gamma.Id : null;
                    if (gamma) {
                        // The 2-group ceiling: (Alpha|Beta) AND Gamma.
                        const two = await q('genres=' + encodeURIComponent('Alpha|Beta') +
                                            '&genreIds=' + gamma.Id);
                        out.twoGroupAndCount = two.TotalRecordCount;
                    }
                } catch (e) { out.genresError = String(e); }
                return out;
            }""")
        results["ceiling"] = ceiling
        check("stock OR of all three genres = 6 (the entire library)",
              ceiling["orAllCount"] == 6, f"got {ceiling['orAllCount']}")
        check("the same three genres intersected = 1", len(three.get("items") or []) == 1)
        if ceiling.get("twoGroupAndCount") is not None:
            check("2-group ceiling reproduced live: (Alpha|Beta) AND Gamma = 3",
                  ceiling["twoGroupAndCount"] == 3,
                  f"got {ceiling['twoGroupAndCount']}")
            check("3 groups (1) goes where the 2-group API cannot (3)",
                  len(three.get("items") or []) == 1)
        else:
            print(f"   (skipped 2-group ceiling: {ceiling.get('genresError')})")

        # ---- 4. the paging trap --------------------------------------------
        # Intersecting pages is not intersecting sets. With limit=2 each genre
        # set is truncated; the result looks like a normal answer and is wrong.
        print("\n4. the paging trap: intersecting pages instead of sets")
        naive = resolve(["Alpha", "Beta"], limit=2)
        results["naive_limit2"] = naive
        check("a truncated genre set is FLAGGED", naive.get("truncated") is True,
              f"perGenre={naive.get('perGenre')}")
        check("the truncated answer is silently wrong (2, truth is 3)",
              len(naive.get("items") or []) == 2,
              f"got {names(naive)} vs truth {sorted([AB1, AB2, ALL_THREE])}")
        check("...and it is wrong for the right reason: alpha-beta-two fell "
              "outside the page",
              AB2 not in names(naive), f"got {names(naive)}")
        check("the flag is what makes it safe — same call without a limit is "
              "complete and correct",
              ab.get("truncated") is False and len(ab.get("items") or []) == 3)

        # ---- 5. the panel path drives the same code ------------------------
        print("\n5. the panel UI reaches the same result (not just the hook)")
        page.evaluate("""() => {
            const b=[...document.querySelectorAll('button')]
              .find(x=>x.querySelector('svg[data-testid*="Filter"]'));
            if(b) b.click();
        }""")
        page.wait_for_timeout(2000)
        panel = page.evaluate("() => !!document.querySelector('.jf-intersect-panel')")
        check("panel mounted", panel)
        if panel:
            tri = page.evaluate(
                """async () => {
                    const s = window.__JF_INTERSECT__;
                    s.selected = ['Alpha','Beta','Gamma'];
                    const btn = document.querySelector('[data-jf-intersect=resolve]');
                    if (btn) btn.click();
                    // wait for the resolve to land
                    for (let i=0;i<60 && s.resolving;i++) await new Promise(r=>setTimeout(r,100));
                    await new Promise(r=>setTimeout(r,300));
                    const out = document.querySelector('[data-jf-intersect=result]');
                    return { text: out ? out.textContent : null,
                             items: (s.result && s.result.items) || [],
                             truncated: s.result && s.result.truncated };
                }""")
            results["panel_resolve"] = tri
            print(f"   panel said: {tri['text']!r}")
            check("clicking Resolve in the panel yields the 3-way answer",
                  len(tri["items"]) == 1, f"got {[i['name'] for i in tri['items']]}")
            check("panel reports it is not truncated", tri["truncated"] is False)

        results["console_tail"] = logs[-20:]
        (OUT / "nway-report.json").write_text(json.dumps(results, indent=2))
        print(f"\nreport -> {OUT / 'nway-report.json'}")
        browser.close()

    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
