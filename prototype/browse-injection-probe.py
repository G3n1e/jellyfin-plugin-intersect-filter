#!/usr/bin/env python3
"""Stage 1 of the §12.1 prototype: prove the injection mechanism, then find a
mount point in the browse view that survives a React re-render.

The `/web/index.html` intercept below deliberately mirrors, byte for byte, what
the planned `IStartupFilter` does server-side (kickoff §6):

    1. run ahead of the static-file handler
    2. strip Accept-Encoding / Range / If-Range
    3. buffer, insert <script defer> before </body>
    4. strip ETag / Last-Modified, recompute Content-Length

Doing it in the browser instead of via a real plugin means the §12.1 question
("does a second selector survive a filter change?") can be answered before
committing to the server-side design.

Outputs a DOM report to prototype/out/ so mount-point candidates can be chosen
from evidence rather than guesswork.
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

OUT.mkdir(exist_ok=True)
report: dict = {"stages": {}}


def handle_index(route):
    """Serve index.html with our <script> spliced in — the IStartupFilter transform."""
    resp = route.fetch()
    body = resp.body().decode("utf-8", errors="replace")
    if "</body>" not in body:
        report["stages"].setdefault("injection", {})["error"] = "no </body> found"
        route.fulfill(response=resp)
        return
    patched = body.replace("</body>", '<script defer src="/proto-inject.js"></script></body>', 1)
    headers = {
        k: v for k, v in resp.headers.items()
        # §6: strip these or the browser may reuse a cached/encoded body
        if k.lower() not in ("content-encoding", "content-length", "etag",
                             "last-modified", "accept-ranges", "vary")
    }
    route.fulfill(status=resp.status, headers=headers, body=patched)
    report["stages"].setdefault("injection", {})["html_patched"] = True


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: console.append(f"PAGEERROR: {e}"))

        routed = {"index": 0, "script": 0}

        def route_handler(route):
            url = route.request.url
            if url.endswith("/proto-inject.js"):
                routed["script"] += 1
                route.fulfill(status=200, content_type="application/javascript", body=INJECT)
            elif url.rstrip("/").endswith("/web/index.html") or url.rstrip("/").endswith("/web"):
                routed["index"] += 1
                handle_index(route)
            else:
                route.continue_()

        ctx.route("**/*", route_handler)

        # ---- Stage A: does the injected script execute? --------------------
        print("A. loading client with injected script...")
        page.goto(f"{BASE}/web/", wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(3000)
        probe = page.evaluate("() => window.__JF_PROBE__ || null")
        report["stages"]["A_injection"] = {
            "index_html_routed": routed["index"],
            "inject_script_served": routed["script"],
            "probe_present": probe is not None,
            "probe": probe,
        }
        print(f"   index.html intercepted : {routed['index']}")
        print(f"   inject.js served       : {routed['script']}")
        print(f"   window.__JF_PROBE__    : {probe}")

        # ---- Stage B: log in -------------------------------------------------
        print("\nB. logging in...")
        page.wait_for_timeout(2000)
        inputs = page.eval_on_selector_all(
            "input", "els => els.map(e => ({name:e.name, id:e.id, type:e.type, "
                     "ph:e.placeholder, cls:e.className}))")
        report["stages"]["B_login_inputs"] = inputs
        print(f"   inputs found: {[(i['name'] or i['id'] or i['type']) for i in inputs]}")

        filled = False
        for sel in ['input[name="username"]', "#txtManualName", 'input[autocomplete="username"]']:
            if page.locator(sel).count():
                page.fill(sel, USER)
                filled = True
                break
        for sel in ['input[name="password"]', "#txtManualPassword", 'input[type="password"]']:
            if page.locator(sel).count():
                page.fill(sel, PASSWORD)
                break
        report["stages"]["B_filled"] = filled

        if filled:
            for sel in ['button[type="submit"]', 'button:has-text("Sign In")',
                        'button:has-text("Sign in")', ".btnSubmit"]:
                if page.locator(sel).count():
                    page.click(sel)
                    break
            page.wait_for_timeout(6000)

        report["stages"]["B_after_login"] = {
            "url": page.url,
            "has_react_root": page.locator("#reactRoot").count() > 0,
            "title": page.title(),
        }
        print(f"   url after login: {page.url}")

        # ---- Stage C: reach the Movies library and find mount candidates -----
        print("\nC. navigating to Movies library...")
        lib_id = None
        for _ in range(3):
            try:
                libs = page.evaluate(
                    """async () => {
                        const r = await fetch('/Library/VirtualFolders', {headers:{
                            'Authorization': 'MediaBrowser Token="' +
                                (window.ApiClient && window.ApiClient.accessToken ?
                                 window.ApiClient.accessToken() : '') + '"'}});
                        return r.ok ? await r.json() : null;
                    }"""
                )
                if libs:
                    lib_id = next((f["ItemId"] for f in libs if f["Name"] == "Movies"), None)
                    break
            except Exception:
                pass
            page.wait_for_timeout(1500)

        print(f"   Movies library id: {lib_id}")
        report["stages"]["C_lib_id"] = lib_id

        for url in (f"{BASE}/web/#/movies?topParentId={lib_id}",
                    f"{BASE}/web/index.html#/movies.html?topParentId={lib_id}",
                    f"{BASE}/web/#/movies"):
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(4000)
            if page.locator('[data-type="Movie"], .cardBox, .card').count() > 0:
                print(f"   library rendered at: {url}")
                report["stages"]["C_url"] = url
                break
        else:
            print("   could not confirm a rendered library grid")

        # dump candidate anchors for mounting our UI
        candidates = page.evaluate(
            """() => {
                const out = {};
                const q = (k, sel) => { const e = document.querySelector(sel);
                    out[k] = e ? {tag:e.tagName, cls:e.className, id:e.id,
                                  text:(e.innerText||'').slice(0,40)} : null; };
                q('reactRoot', '#reactRoot');
                q('libraryPage', '.libraryPage');
                q('filterButton', '.filterButton, [class*="filterButton"]');
                q('viewSettingsButton', '[class*="viewSettings"]');
                q('toolbar', '.libraryToolbar, [class*="toolbar"]');
                q('itemsView', '.itemsView, [class*="itemsView"]');
                out.allButtons = [...document.querySelectorAll('button')]
                    .map(b => (b.innerText||b.getAttribute('aria-label')||'').trim())
                    .filter(Boolean).slice(0, 30);
                out.reactRootChildren = document.querySelector('#reactRoot')
                    ? [...document.querySelector('#reactRoot').children].map(c=>c.className) : [];
                return out;
            }"""
        )
        report["stages"]["C_candidates"] = candidates
        print("\n   mount candidates:")
        for k, v in candidates.items():
            if k not in ("allButtons", "reactRootChildren"):
                print(f"     {k:20} {v}")
        print(f"     reactRoot children : {candidates.get('reactRootChildren')}")
        print(f"     buttons            : {candidates.get('allButtons')}")
        print(f"   probe still present: {page.evaluate('() => window.__JF_PROBE__ || null') is not None}")

        report["stages"]["console"] = console[-40:]
        (OUT / "probe-report.json").write_text(json.dumps(report, indent=2))
        print(f"\nreport -> {OUT / 'probe-report.json'}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
