#!/usr/bin/env python3
"""Verify the shipped client: the injection, the builder, and the intersection it produces.

Unlike filter-survival-test.py, placement-test.py and nway-test.py, this harness does NOT
intercept /web/index.html. There is nothing left to simulate: the plugin's middleware
performs the injection, so patching the page here would let a broken plugin pass. What is
asserted is the mechanism that actually ships.

The claim under test is the deliverable: a user picks two or more genres in the browse view
and sees the intersection, live, and three or more groups work - the thing the stock API
cannot do at all (docs/FINDINGS.md 2 and 5).

Ground truth is re-derived from the stock API at run time rather than hardcoded, so a stale
expectation cannot make a wrong answer pass.

Usage:  python3 client-test.py
Needs:  prototype/out/credentials.json and the plugin deployed (deploy-plugin.sh).
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "out"
CREDS = json.loads((OUT / "credentials.json").read_text())
BASE, USER, PASSWORD = CREDS["base"], CREDS["user"], CREDS["password"]
TOKEN, USER_ID = CREDS["token"], CREDS["userId"]
LIB = "f137a2dd21bbc1b99aa5c0f6bf02a805"

ALL_THREE = "All Three"
# Deliberately not a genre any library would carry: this is the name used to prove that a
# genre missing from Items/Filters can still be typed, and that the result says so.
UNLISTED = "Zzz Not A Genre"

checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    checks.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- stock API ground truth

def get(path: str, params: list[tuple[str, str]]) -> dict:
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Authorization", f'MediaBrowser Token="{TOKEN}"')
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_body": e.read().decode("utf-8", errors="replace")}


def stock_genre(name: str) -> set[str]:
    r = get("/Items", [("userId", USER_ID), ("parentId", LIB), ("recursive", "true"),
                       ("includeItemTypes", "Movie"), ("limit", "100"), ("genres", name)])
    return {i["Name"] for i in r.get("Items", [])}


def http_status(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


# ---------------------------------------------------------------- browser helpers

def sign_in(page) -> None:
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


def click_filter_button(page) -> None:
    """Opens the native filter popover."""
    for sel in ['svg[data-testid="FilterAltIcon"]', 'button[title="Filter"]',
                '[aria-describedby="filter-popover"]']:
        if page.locator(sel).count():
            page.locator(sel).first.click()
            page.wait_for_timeout(1200)
            return
    page.evaluate("""() => {
        const b=[...document.querySelectorAll('button')]
          .find(x=>x.querySelector('svg[data-testid*="Filter"]'));
        if(b) b.click();
    }""")
    page.wait_for_timeout(1200)


def close_filter_popover(page) -> None:
    """Dismisses the filter popover by clicking outside it, the way a user would.

    Two things this harness got wrong first, both worth recording:

    The popover does not close by clicking the filter button again - that button is inert
    while the popover is open, and the popover's own invisible backdrop intercepts the click.
    The backdrop is the thing that dismisses it, so that is what is clicked here.

    The click is dispatched on the element rather than sent to screen coordinates because our
    dialog's backdrop covers the viewport: a real mouse click would land on that instead. The
    event sequence is dispatched in full rather than as a bare .click(), since MUI acts on
    mousedown and a lone click event would leave the popover open - which is what made an
    earlier run look like "the dialog survived, but so did the popover".
    """
    page.evaluate("""() => {
        const root = document.getElementById('filter-popover');
        const backdrop = root && root.querySelector('.MuiBackdrop-root');
        const target = backdrop || root;
        if (!target) return;
        const opts = {bubbles:true, cancelable:true, view:window, detail:1};
        for (const type of ['pointerdown','mousedown','pointerup','mouseup','click']) {
            const Ctor = type.startsWith('pointer') ? PointerEvent : MouseEvent;
            target.dispatchEvent(new Ctor(type, opts));
        }
    }""")
    page.wait_for_timeout(1500)


def popover_visible(page) -> bool:
    """Whether the native filter popover is actually on screen.

    Presence in the DOM is not the question: MUI can leave a closed popover mounted through
    its exit transition, or keep it mounted outright.
    """
    return page.evaluate("""() => {
        const h = document.getElementById('filtersGenres-header');
        return !!h && !!h.offsetParent;
    }""")


def add_genre(page, group_index: int, name: str) -> bool:
    """Types into a group's genre box and clicks the matching option, through the real UI."""
    box = page.locator(f'[data-jf-intersect="genre-search"][data-group-index="{group_index}"]')
    if not box.count():
        return False
    box.first.fill(name)
    page.wait_for_timeout(350)

    # Scoped to this group's list. Every group's options are in the DOM at once, so an
    # unqualified query would click the first group's copy of the genre and silently add it
    # there instead - which is how this harness first "proved" a three-way OR.
    options = page.locator(
        f'[data-jf-intersect="genre-options"][data-group-index="{group_index}"]'
        f' [data-jf-intersect="genre-option"]')
    for i in range(options.count()):
        if options.nth(i).inner_text().strip() == name:
            options.nth(i).click()
            page.wait_for_timeout(700)
            return True
    return False


def type_genre(page, group_index: int, name: str) -> None:
    """Types a genre into a group's box with real key events and presses Enter.

    The keys go through the keyboard rather than locator.fill(), and that distinction is the
    whole point. fill() assigns the value directly and never moves focus, so it passed
    against a box that a person could not type into at all: the native filter popover is a
    MUI Modal, and a Modal drags focus back to itself whenever focus escapes. Every
    keystroke was landing on the popover. Only real keystrokes can see that.

    Enter is sent via the keyboard too, for the same reason - locator.press() focuses the
    element first, which would paper over the focus loss being tested for.
    """
    box = page.locator(f'[data-jf-intersect="genre-search"][data-group-index="{group_index}"]')
    box.first.click()
    page.wait_for_timeout(200)
    page.keyboard.type(name, delay=50)
    page.wait_for_timeout(250)
    page.keyboard.press("Enter")
    page.wait_for_timeout(900)


def chip_names(page, group_index: int, only_unknown: bool = False) -> list[str]:
    """The genre names shown as chips in one group, optionally only the unlisted ones."""
    groups = page.locator('[data-jf-intersect="group"]')
    if group_index >= groups.count():
        return []
    selector = ('[data-jf-intersect="chip"].jf-chip-unknown' if only_unknown
                else '[data-jf-intersect="chip"]')
    nodes = groups.nth(group_index).locator(selector)
    return [nodes.nth(i).locator("span").first.inner_text().strip()
            for i in range(nodes.count())]


def remove_last_chip(page, group_index: int) -> None:
    """Clicks the × on a group's last chip."""
    groups = page.locator('[data-jf-intersect="group"]')
    buttons = groups.nth(group_index).locator('[data-jf-intersect="chip"] button')
    if buttons.count():
        buttons.last.click()
        page.wait_for_timeout(700)


def card_names(page) -> list[str]:
    cards = page.locator('[data-jf-intersect="card"]')
    return sorted(cards.nth(i).get_attribute("title") or "" for i in range(cards.count()))


def summary(page) -> str:
    node = page.locator('[data-jf-intersect="summary"]')
    return node.first.inner_text().strip() if node.count() else ""


def main() -> int:
    print("0. the plugin injects the script into the shell it serves")
    status, shell = http_status(f"{BASE}/web/index.html")
    check("GET /web/index.html is 200", status == 200, f"status={status}")
    check("the shell carries the client script", "__JF_INTERSECT__" in shell)
    check("...inline, so no second request or token is needed",
          "<script>" in shell and "__JF_INTERSECT__" in shell)

    # Ground truth, from the stock API, before the browser is involved.
    alpha, beta, gamma = stock_genre("Alpha"), stock_genre("Beta"), stock_genre("Gamma")
    truth_ab = alpha & beta
    truth_abc = alpha & beta & gamma
    print(f"   Alpha={sorted(alpha)}")
    print(f"   Alpha AND Beta AND Gamma = {sorted(truth_abc)}")
    check("the fixture library has the expected shape",
          truth_abc == {ALL_THREE}, f"got {sorted(truth_abc)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        sign_in(page)

        print("\n1. the script runs in the SPA")
        check("window.__JF_INTERSECT__ is present",
              page.evaluate("() => !!window.__JF_INTERSECT__"))
        check("...after the client has created ApiClient",
              page.evaluate("() => !!window.ApiClient"))

        print("\n2. the entry point is in the native filter popover")
        click_filter_button(page)
        entry = page.locator('[data-jf-intersect="entry"]')
        check("the popover opened", page.locator("#filtersGenres-header").count() > 0
              or entry.count() > 0)
        check("the entry button is mounted", entry.count() == 1,
              f"found {entry.count()}")
        check("...exactly once, not accumulated", entry.count() == 1)

        print("\n3. it opens a surface of its own")
        entry.first.click()
        page.wait_for_timeout(2000)
        dialog = page.locator("#jf-intersect-dialog")
        check("the dialog opened", dialog.count() == 1, f"found {dialog.count()}")
        # Not tidiness: the popover is a MUI Modal and holds focus while open, so leaving it
        # up makes the genre box untypable. Asserting it here is what would have caught that.
        check("...and the native popover closed itself, releasing the focus it was holding",
              not popover_visible(page))
        check("...attached outside the React root, so React cannot unmount it",
              page.evaluate("""() => {
                  const d = document.getElementById('jf-intersect-dialog');
                  return !!d && !d.closest('#reactRoot');
              }"""))
        check("...and it has one empty group to start with",
              page.locator('[data-jf-intersect="group"]').count() == 1)
        option_count = page.locator('[data-jf-intersect="genre-option"]').count()
        check("...offering the library's own genres", option_count > 0,
              f"{option_count} options")

        print("\n4. the dialog outlives the popover, and the box takes focus")
        # Usually a no-op now that openDialog dismisses the popover itself. Kept so the
        # survival assertion below does not silently depend on our own dismissal succeeding.
        close_filter_popover(page)
        check("the native popover is gone", not popover_visible(page))
        check("...and the dialog is still there",
              page.locator("#jf-intersect-dialog").count() == 1)

        focus_box = page.locator('[data-jf-intersect="genre-search"][data-group-index="0"]')
        focus_box.first.click()
        page.wait_for_timeout(300)
        check("...and the genre box actually holds focus when clicked",
              page.evaluate("""() => {
                  const a = document.activeElement;
                  return !!a && a.getAttribute('data-jf-intersect') === 'genre-search';
              }"""),
              page.evaluate("() => document.activeElement ? document.activeElement.className : 'none'"))

        print("\n5. three AND groups, driven through the UI")
        check("Alpha added to group 1", add_genre(page, 0, "Alpha"))
        page.click('[data-jf-intersect="add-group"]')
        page.wait_for_timeout(500)
        check("a second group can be added",
              page.locator('[data-jf-intersect="group"]').count() == 2)
        check("Beta added to group 2", add_genre(page, 1, "Beta"))
        page.click('[data-jf-intersect="add-group"]')
        page.wait_for_timeout(500)
        check("a third group can be added - the reason the plugin exists",
              page.locator('[data-jf-intersect="group"]').count() == 3)
        check("Gamma added to group 3", add_genre(page, 2, "Gamma"))
        page.wait_for_timeout(2000)

        print("\n6. the intersection, live")
        names = card_names(page)
        check("Alpha AND Beta AND Gamma shows exactly one item",
              names == [ALL_THREE], f"got {names}")
        check("...matching the independently computed intersection",
              set(names) == truth_abc)
        check("...and the summary reports the whole intersection",
              "1 item" in summary(page), f"got {summary(page)!r}")
        check("the criteria trace is shown, so the narrowing is visible",
              page.locator('[data-jf-intersect="trace"]').count() == 1,
              page.locator('[data-jf-intersect="trace"]').first.inner_text().strip()
              if page.locator('[data-jf-intersect="trace"]').count() else "")

        print("\n7. the request the UI builds cannot trigger the delimiter trap")
        built = page.evaluate(
            "() => window.__JF_INTERSECT__.buildUrl([['Alpha'],['Beta'],['Gamma']])")
        params = urllib.parse.parse_qs(urllib.parse.urlparse(built).query)
        check("one groups parameter per AND group",
              params.get("groups") == ["Alpha", "Beta", "Gamma"], f"got {params.get('groups')}")
        check("...and no group is comma-joined or comma-split",
              "," not in urllib.parse.urlparse(built).query, built)
        check("...scoped to the library being browsed",
              params.get("parentId") == [LIB], f"got {params.get('parentId')}")

        print("\n8. removing a criterion widens the result again")
        # The last group, undoing the last addition: the result should widen back to exactly
        # what the two-way intersection gave. (Removing the first group instead would drop
        # Alpha and correctly leave Beta AND Gamma - also right, but it tests a different
        # transition than "undo".)
        page.locator('[data-jf-intersect="remove-group"]').last.click()
        page.wait_for_timeout(2000)
        check("...leaving two groups",
              page.locator('[data-jf-intersect="group"]').count() == 2)
        wider = card_names(page)
        check("dropping back to two groups gives the two-way intersection",
              set(wider) == truth_ab, f"got {wider}")

        print("\n9. a genre can be typed and entered, not only clicked")
        page.click('[data-jf-intersect="add-group"]')
        page.wait_for_timeout(500)
        typed_group = page.locator('[data-jf-intersect="group"]').count() - 1
        check("a third group was added to type into", typed_group == 2, f"got {typed_group}")

        type_genre(page, typed_group, "Gamma")
        page.wait_for_timeout(2000)
        check("typing a genre and pressing Enter adds it, with no option clicked",
              "Gamma" in chip_names(page, typed_group),
              f"chips={chip_names(page, typed_group)}")
        check("...and the intersection narrows to the one three-way match",
              card_names(page) == [ALL_THREE], f"got {card_names(page)}")

        print("\n10. a genre the library does not list can still be typed")
        # Items/Filters is scoped to the view being browsed and is not guaranteed to
        # enumerate every genre, so the box has to accept a name that is not offered.
        remove_last_chip(page, typed_group)
        type_genre(page, typed_group, UNLISTED)
        page.wait_for_timeout(2500)
        check("the unlisted name is accepted",
              UNLISTED in chip_names(page, typed_group),
              f"chips={chip_names(page, typed_group)}")
        check("...and is marked as not one the library lists",
              UNLISTED in chip_names(page, typed_group, only_unknown=True),
              f"unknown={chip_names(page, typed_group, only_unknown=True)}")

        print("\n11. an empty result says which criterion emptied it")
        check("nothing matches the unlisted genre",
              card_names(page) == [], f"got {card_names(page)}")
        trace = page.locator('[data-jf-intersect="trace"]')
        check("the trace is shown even though the result is empty",
              trace.count() == 1, f"count={trace.count()}")
        check("...and names the criterion that matched nothing",
              trace.count() == 1 and "0 → 0" in trace.first.inner_text(),
              trace.first.inner_text() if trace.count() else "")

        print("\n12. arrow keys decide what Enter adds")
        remove_last_chip(page, typed_group)
        box = page.locator(
            f'[data-jf-intersect="genre-search"][data-group-index="{typed_group}"]')
        box.first.click()
        page.wait_for_timeout(200)
        page.keyboard.type("a", delay=50)
        page.wait_for_timeout(400)
        options = page.locator(
            f'[data-jf-intersect="genre-options"][data-group-index="{typed_group}"]'
            f' [data-jf-intersect="genre-option"]')
        check("a query matching several genres offers a choice", options.count() >= 2,
              f"got {options.count()}")
        second = options.nth(1).inner_text().strip()
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(250)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
        check("ArrowDown then Enter adds the highlighted genre, not the first",
              second in chip_names(page, typed_group),
              f"expected {second!r}, chips={chip_names(page, typed_group)}")

        # Put the builder back the way the next section expects to find it.
        page.locator('[data-jf-intersect="remove-group"]').last.click()
        page.wait_for_timeout(2000)
        check("...and the criteria are back to two groups",
              page.locator('[data-jf-intersect="group"]').count() == 2)

        print("\n13. a result opens in the native details page")
        page.locator('[data-jf-intersect="card"]').first.click()
        page.wait_for_timeout(1500)
        check("clicking a card navigates to the details route",
              "#/details" in page.url, page.url.split("#")[-1])
        check("...and the dialog got out of the way",
              page.locator("#jf-intersect-dialog").count() == 0)

        print("\n14. no client errors")
        check("the page raised no uncaught errors", not errors, "; ".join(errors[:3]))
        page.screenshot(path=str(OUT / "client-test.png"))

        browser.close()

    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("\nfailures:")
        for label, _, detail in failed:
            print(f"  - {label}" + (f"  ({detail})" if detail else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
