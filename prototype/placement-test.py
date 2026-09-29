#!/usr/bin/env python3
"""Compare the three candidate placements for the intersect panel.

This exists because FINDINGS.md §5(E) recorded the baseline failure — appended
after every accordion, the panel sits at top=487..bottom=629 while the popover
paper ends at 539, so it is entirely below the fold and needs a 182px scroll.
That was measured, but only for one placement. Before committing to a UI
structure we need the same measurement for the alternatives:

    after-accordion  baseline; sibling of the Genres accordion
    prepend          first child of the paper; always visible
    inside-genres    inside the Genres accordion body, under the native list

The placement is chosen in the browser by setting window.__JF_PLACEMENT__
before inject.js runs, so all three are measured against the *same* payload —
no divergent copies of the panel code to keep in sync.

Metrics per placement: whether the panel is fully visible on open, where it
sits relative to the paper, how much scroll it needs, and whether it survives
a popover close/reopen (the animation-frame re-mount must still work when the
panel is nested differently).

Writes prototype/out/placement-report.json and one screenshot per placement.
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

MODES = ["after-accordion", "prepend", "inside-genres"]
OUT.mkdir(exist_ok=True)


def login_and_open(page) -> None:
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


def open_filter(page) -> None:
    for sel in ['svg[data-testid="FilterAltIcon"]', 'button[title="Filter"]',
                '[aria-describedby="filter-popover"]']:
        if page.locator(sel).count():
            page.locator(sel).first.click()
            return
    page.evaluate("""() => {
        const b=[...document.querySelectorAll('button')]
          .find(x=>x.querySelector('svg[data-testid*="Filter"]'));
        if(b) b.click();
    }""")


def expand_genres(page) -> bool:
    """Open the native Genres accordion.

    This is not incidental: the accordions are exclusive and collapsed by
    default, so with nothing expanded the popover content is ~437px and fits
    without scrolling — which makes every placement look fine. The realistic
    state for someone picking genres is Genres *expanded*, and that is the
    state in which the 182px overflow was observed. Measure there.
    """
    page.evaluate("""() => { const h=document.getElementById('filtersGenres-header');
                             if(h && h.getAttribute('aria-expanded')!=='true') h.click(); }""")
    page.wait_for_timeout(1200)
    return page.evaluate(
        """() => { const h=document.getElementById('filtersGenres-header');
                   return !!h && h.getAttribute('aria-expanded')==='true'; }""")


def measure(page) -> dict | None:
    return page.evaluate(
        """() => {
            const panel=document.querySelector('.jf-intersect-panel');
            if(!panel) return null;
            const paper=panel.closest('.MuiPopover-paper');
            if(!paper) return { error: 'panel exists but is not inside a paper' };
            const pr=paper.getBoundingClientRect(), nr=panel.getBoundingClientRect();
            // Which ancestor holds the panel tells us the placement took effect.
            const inDetails = !!panel.closest('.MuiAccordionDetails-root');
            // Walk up to the paper recording each ancestor, so an unexpected
            // placement can be diagnosed from the report instead of guessed at.
            const chain=[]; let n=panel;
            while(n && n!==paper){ chain.push(n.tagName.toLowerCase()+
                (n.className&&typeof n.className==='string' ?
                 '.'+n.className.split(' ').filter(Boolean).join('.') : '')); n=n.parentElement; }
            const accordion=document.getElementById('filtersGenres-header');
            const acc=accordion && accordion.closest('.MuiAccordion-root');
            const ar=acc ? acc.getBoundingClientRect() : null;
            return {
                paperTop: Math.round(pr.top),
                ancestorChain: chain,
                genresAccordion: ar ? { top: Math.round(ar.top),
                                        bottom: Math.round(ar.bottom) } : null,
                placement: (window.__JF_PROBE__||{}).placement,
                paperWidth: Math.round(pr.width),
                paperHeight: Math.round(pr.height),
                scrollHeight: paper.scrollHeight,
                clientHeight: paper.clientHeight,
                overflowY: getComputedStyle(paper).overflowY,
                panelTop: Math.round(nr.top),
                panelBottom: Math.round(nr.bottom),
                panelHeight: Math.round(nr.height),
                paperBottom: Math.round(pr.bottom),
                fullyVisible: nr.bottom <= pr.bottom + 1 && nr.top >= pr.top - 1,
                scrollNeeded: Math.max(0, Math.round(paper.scrollHeight - paper.clientHeight)),
                inAccordionDetails: inDetails,
                firstChildOfPaper: paper.firstElementChild === panel
            };
        }""")
def run_mode(p, mode: str) -> dict:
    print(f"\n=== placement: {mode} ===")
    browser = p.chromium.launch(args=["--no-sandbox"])
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_init_script(f"window.__JF_PLACEMENT__ = {json.dumps(mode)};")
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

    result: dict = {"mode": mode}
    try:
        login_and_open(page)
        open_filter(page)
        page.wait_for_timeout(2000)

        expanded = expand_genres(page)
        result["genres_expanded"] = expanded
        print(f"   genres accordion expanded: {expanded}")
        geo = measure(page)
        result["on_open"] = geo
        if not geo:
            result["error"] = "panel did not mount"
            print("   panel did not mount")
        else:
            print(f"   placement reported : {geo.get('placement')}")
            print(f"   paper {geo['paperWidth']}x{geo['paperHeight']} "
                  f"top={geo['paperTop']} scrollH={geo['scrollHeight']} "
                  f"overflowY={geo['overflowY']}")
            print(f"   genres accordion   : {geo.get('genresAccordion')}")
            print(f"   panel top={geo['panelTop']} bottom={geo['panelBottom']} "
                  f"paperBottom={geo['paperBottom']}")
            print(f"   fullyVisible={geo['fullyVisible']} scrollNeeded={geo['scrollNeeded']} "
                  f"inAccordionDetails={geo['inAccordionDetails']} "
                  f"firstChildOfPaper={geo['firstChildOfPaper']}")
            print(f"   ancestors          : {geo.get('ancestorChain')}")

            # Select two genres, then close and reopen: the re-mount path must
            # keep working now that the panel nests differently.
            for idx in (0, 1):
                page.evaluate("""(i) => {
                    const b=document.querySelectorAll('.jf-intersect-panel input[type=checkbox]');
                    if(b[i]) b[i].click();
                }""", idx)
                page.wait_for_timeout(350)
            selected = page.evaluate("() => (window.__JF_INTERSECT__||{}).selected")
            result["selected"] = selected

            page.keyboard.press("Escape")
            page.wait_for_timeout(1800)
            result["after_close"] = page.evaluate(
                "() => ({panel: !!document.querySelector('.jf-intersect-panel')})")

            open_filter(page)
            page.wait_for_timeout(2200)
            reopen = page.evaluate(
                """() => {
                    const checked=[...document.querySelectorAll(
                        '.jf-intersect-panel input[type=checkbox]')].filter(c=>c.checked)
                        .map(c=>c.value);
                    return { panelPresent: !!document.querySelector('.jf-intersect-panel'),
                             checked: checked,
                             mounts: (window.__JF_PROBE__||{}).mounts,
                             count: document.querySelectorAll('.jf-intersect-panel').length };
                }""")
            result["after_reopen"] = reopen
            result["state_survived"] = sorted(reopen["checked"]) == sorted(selected or [])
            print(f"   reopen: present={reopen['panelPresent']} "
                  f"state_survived={result['state_survived']} "
                  f"mounts={reopen['mounts']} count={reopen['count']}")

            page.screenshot(path=str(OUT / f"placement-{mode}.png"))

            # Second measurement after reopen — the re-mounted panel may land
            # somewhere different from the first mount.
            result["on_reopen"] = measure(page)
    except Exception as e:  # noqa: BLE001 — report and keep going to next mode
        result["error"] = f"{type(e).__name__}: {e}"
        print(f"   ERROR: {e}")

    result["console_tail"] = logs[-15:]
    browser.close()
    return result


def main() -> int:
    results = []
    with sync_playwright() as p:
        for mode in MODES:
            results.append(run_mode(p, mode))

    (OUT / "placement-report.json").write_text(json.dumps(results, indent=2))

    print("\n\n=== summary ===")
    header = f"{'placement':16} {'mounts':>6} {'top':>5} {'bottom':>6} {'paperBot':>8} " \
             f"{'visible':>7} {'scroll':>6} {'survives':>8}"
    print(header)
    for r in results:
        geo = r.get("on_open") or {}
        re_geo = r.get("on_reopen") or {}
        print(f"{r['mode']:16} "
              f"{(r.get('after_reopen') or {}).get('mounts', '-'):>6} "
              f"{geo.get('panelTop', '-'):>5} {geo.get('panelBottom', '-'):>6} "
              f"{geo.get('paperBottom', '-'):>8} "
              f"{str(geo.get('fullyVisible', '-')):>7} "
              f"{geo.get('scrollNeeded', '-'):>6} "
              f"{str(r.get('state_survived', '-')):>8}")
        if r.get("on_reopen") and re_geo.get("fullyVisible") != geo.get("fullyVisible"):
            print(f"{'':16} (on reopen: visible={re_geo.get('fullyVisible')} "
                  f"top={re_geo.get('panelTop')} bottom={re_geo.get('panelBottom')})")
    print(f"\nreport -> {OUT / 'placement-report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
