#!/usr/bin/env python3
"""§12.1 experiment: does an injected selector survive a filter change?

Answers three questions with evidence rather than reasoning:

  Q1  Does a node appended inside the native filter Popover survive a React
      re-render caused by changing a native filter?
  Q2  Does it survive the Popover closing (which unmounts React's subtree)?
  Q3  Does a MutationObserver re-mount it on reopen, with module-level state
      intact?

Writes a screenshot and a JSON report to prototype/out/.
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

OUT.mkdir(exist_ok=True)
results: dict = {}
checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    checks.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
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
                page.fill(sel, USER); break
        for sel in ['#txtManualPassword', 'input[type="password"]']:
            if page.locator(sel).count():
                page.fill(sel, PASSWORD); break
        for sel in ['button[type="submit"]', 'button:has-text("Sign In")', '.btnSubmit']:
            if page.locator(sel).count():
                page.click(sel); break
        page.wait_for_timeout(5000)
        page.goto(f"{BASE}/web/#/movies?topParentId={LIB}", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

        def open_filter():
            found = False
            for sel in ['svg[data-testid="FilterAltIcon"]', 'button[title="Filter"]',
                        '[aria-describedby="filter-popover"]']:
                if page.locator(sel).count():
                    page.locator(sel).first.click()
                    found = True
                    break
            if not found:
                page.evaluate("""() => {
                    const b=[...document.querySelectorAll('button')]
                      .find(x=>x.querySelector('svg[data-testid*="Filter"]'));
                    if(b) b.click();
                }""")
            page.wait_for_timeout(1500)
            return page.locator("#filter-popover, .MuiPopover-paper").count() > 0

        # ---- Q1: mount + survive a native filter change --------------------
        print("\nQ1. mount inside popover, then change a native filter")
        opened = open_filter()
        check("filter popover opens", opened)
        results["popover_opened"] = opened

        panel_in_popover = page.evaluate(
            """() => { const p=document.getElementById('filter-popover');
                       return !!(p && p.querySelector('.jf-intersect-panel')); }""")
        check("injected panel mounted inside popover", panel_in_popover)

        dupes = page.evaluate(
            """() => ({ count: document.querySelectorAll('.jf-intersect-panel').length,
                        probe: window.__JF_PROBE__ })""")
        check("exactly one panel exists (no duplicates)", dupes["count"] == 1,
              f"count={dupes['count']} mounts={dupes['probe'].get('mounts')} "
              f"dupesRemoved={dupes['probe'].get('duplicatesRemoved')}")

        # expand the native Genres accordion and toggle a native genre
        page.evaluate("""() => { const h=document.getElementById('filtersGenres-header');
                                 if(h) h.click(); }""")
        page.wait_for_timeout(1200)
        native_genres = page.eval_on_selector_all(
            "#filtersGenres-content input[type=checkbox]",
            "els => els.map(e => e.value)")
        results["native_genres"] = native_genres
        print(f"   native genre options: {native_genres}")

        if native_genres:
            page.evaluate("""() => {
                const cb=document.querySelector('#filtersGenres-content input[type=checkbox]');
                if(cb) cb.click();
            }""")
            page.wait_for_timeout(2500)

        after = page.evaluate(
            """() => { const p=document.getElementById('filter-popover');
                       return { panelPresent: !!(p && p.querySelector('.jf-intersect-panel')),
                                popoverPresent: !!p,
                                probe: window.__JF_PROBE__ }; }""")
        results["q1"] = after
        check("panel survives a native filter change (React re-render)",
              after.get("panelPresent") is True,
              f"disappearances={after['probe'].get('disappearances')}")

        # ---- Q2: select state, close the popover ---------------------------
        print("\nQ2. select 2 genres in our panel, then close the popover")
        # Stamp the node so we can prove it is never replaced during a state change.
        page.evaluate("""() => {
            const p=document.querySelector('.jf-intersect-panel');
            if(p) p.setAttribute('data-jf-node-id','stamped');
        }""")
        # Click one checkbox at a time, re-querying each time: the panel must
        # update in place, but the harness must not depend on that to pass.
        for idx in (0, 1):
            page.evaluate("""(i) => {
                const b=document.querySelectorAll('.jf-intersect-panel input[type=checkbox]');
                if(b[i]) b[i].click();
            }""", idx)
            page.wait_for_timeout(400)

        identity = page.evaluate(
            """() => { const p=document.querySelector('.jf-intersect-panel');
                       return { stamped: !!(p && p.getAttribute('data-jf-node-id')),
                                count: document.querySelectorAll('.jf-intersect-panel').length }; }""")
        check("panel node identity preserved across state change (updated in place)",
              identity["stamped"] is True and identity["count"] == 1,
              f"stamped={identity['stamped']} count={identity['count']}")
        selected_before = page.evaluate("() => (window.__JF_INTERSECT__||{}).selected")
        results["selected_before"] = selected_before
        print(f"   module state selected = {selected_before}")
        check("module state holds 2 selections", len(selected_before or []) == 2)

        page.keyboard.press("Escape")
        page.wait_for_timeout(2000)
        closed = page.evaluate(
            """() => ({ popover: !!document.getElementById('filter-popover'),
                        panel: !!document.querySelector('.jf-intersect-panel'),
                        probe: window.__JF_PROBE__ })""")
        results["q2"] = closed
        check("popover closed", closed["popover"] is False)
        check("panel destroyed with popover (expected: MUI unmounts on close)",
              closed["panel"] is False,
              f"disappearances={closed['probe'].get('disappearances')}")

        # ---- Q3: reopen; does the observer re-mount with state intact? -----
        print("\nQ3. reopen the popover")
        reopened = open_filter()
        check("popover reopens", reopened)
        back = page.evaluate(
            """() => {
                const p=document.getElementById('filter-popover');
                const panel=p && p.querySelector('.jf-intersect-panel');
                const checked=[...document.querySelectorAll(
                    '.jf-intersect-panel input[type=checkbox]')].filter(c=>c.checked)
                    .map(c=>c.value);
                return { panelPresent: !!panel, checked: checked,
                         probe: window.__JF_PROBE__,
                         count: document.querySelectorAll('.jf-intersect-panel').length,
                         queryText: panel && panel.querySelector('[data-jf-intersect-query]')
                             ? panel.querySelector('[data-jf-intersect-query]').textContent
                             : null };
            }""")
        results["q3"] = back
        check("panel auto-remounted on reopen", back["panelPresent"] is True,
              f"mounts={back['probe'].get('mounts')}")
        check("module state survived unmount (selections restored)",
              sorted(back["checked"]) == sorted(selected_before or []),
              f"checked={back['checked']}")
        print(f"   panel text: {back.get('queryText')}")

        page.screenshot(path=str(OUT / "filter-panel.png"))

        # ---- Q4: is the panel actually REACHABLE? --------------------------
        # The native popover paper is width:250 / maxHeight:50%. Appending our
        # panel beneath every accordion can push it out of the visible area.
        print("\nQ4. is the panel reachable inside the 250px / 50%-height popover?")
        geo = page.evaluate(
            """() => {
                const panel=document.querySelector('.jf-intersect-panel');
                const paper=panel && panel.closest('.MuiPopover-paper');
                if(!panel||!paper) return null;
                const pr=paper.getBoundingClientRect(), nr=panel.getBoundingClientRect();
                return {
                    paperWidth: Math.round(pr.width),
                    paperHeight: Math.round(pr.height),
                    scrollHeight: paper.scrollHeight,
                    clientHeight: paper.clientHeight,
                    overflowY: getComputedStyle(paper).overflowY,
                    panelTop: Math.round(nr.top),
                    panelBottom: Math.round(nr.bottom),
                    paperBottom: Math.round(pr.bottom),
                    fullyVisible: nr.bottom <= pr.bottom + 1
                };
            }""")
        results["q4_geometry"] = geo
        print(f"   {geo}")
        if geo:
            check("panel initially fully visible in popover",
                  geo["fullyVisible"] is True,
                  f"panelBottom={geo['panelBottom']} paperBottom={geo['paperBottom']} "
                  f"overflowY={geo['overflowY']}")

            # scroll the paper to the bottom and re-measure
            after_scroll = page.evaluate(
                """() => {
                    const panel=document.querySelector('.jf-intersect-panel');
                    const paper=panel && panel.closest('.MuiPopover-paper');
                    if(!paper) return null;
                    paper.scrollTop = paper.scrollHeight;
                    const pr=paper.getBoundingClientRect(), nr=panel.getBoundingClientRect();
                    return { fullyVisible: nr.bottom <= pr.bottom + 1,
                             scrolledBy: paper.scrollTop };
                }""")
            results["q4_after_scroll"] = after_scroll
            print(f"   after scrolling paper: {after_scroll}")
            check("panel reachable by scrolling the popover",
                  bool(after_scroll and after_scroll["fullyVisible"]),
                  f"scrolledBy={after_scroll.get('scrolledBy') if after_scroll else None}")
            page.screenshot(path=str(OUT / "filter-panel-scrolled.png"))

        results["console_tail"] = logs[-25:]
        (OUT / "survival-report.json").write_text(json.dumps(results, indent=2))

        print("\nconsole (tail):")
        for line in logs[-8:]:
            print("   ", line)
        print(f"\nreport    -> {OUT / 'survival-report.json'}")
        print(f"screenshot-> {OUT / 'filter-panel.png'}")
        browser.close()

    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
