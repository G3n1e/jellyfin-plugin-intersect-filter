#!/usr/bin/env python3
"""Verify the settings page, and specifically that its off switch really turns the UI off.

The page exists for one reason: someone running this on a server they care about needs to be
able to put the stock client back without uninstalling the plugin or editing XML. A settings
page that renders but whose save does nothing would look identical to a working one until the
day it is needed, so the round trip is asserted end to end here - through the real dashboard,
against the real server, with the shell fetched over HTTP afterwards.

Also asserted: that toggling off removes only the *UI*. The endpoint is the plugin's actual
product and must keep working, or the "recovery step" would take the feature with it.

Usage:  python3 config-page-test.py
Needs:  prototype/out/credentials.json (an admin account) and the plugin deployed.
"""

from __future__ import annotations

import json
import pathlib
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
PLUGIN_ID = "8f3c1a52-6d24-4b7e-9c8a-1f2e3d4c5b6a"

checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    checks.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def api(path: str, params: list[tuple[str, str]] | None = None) -> dict:
    url = f"{BASE}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url)
    req.add_header("Authorization", f'MediaBrowser Token="{TOKEN}"')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_body": e.read().decode("utf-8", errors="replace")}


def shell() -> str:
    """The served SPA shell, fetched the way a browser would - no credentials."""
    with urllib.request.urlopen(f"{BASE}/web/index.html", timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def injected() -> bool:
    return "__JF_INTERSECT__" in shell()


def set_injection(page, on: bool) -> None:
    """Clicks the toggle the way a user does, then confirms it took.

    The input is an `emby-checkbox` web component: the visible label span sits over it and
    intercepts pointer events, so clicking the input itself is refused by the actionability
    check. The label is what a person actually clicks, so it is what is clicked here - and
    the result is re-read rather than assumed, since a component that swallows the click
    would otherwise pass silently.
    """
    label = page.locator("#IntersectConfigPage label.emby-checkbox-label")
    for _ in range(4):
        if page.locator("#InjectClientScript").is_checked() == on:
            return
        label.click()
        page.wait_for_timeout(500)


def endpoint_still_works() -> tuple[bool, str]:
    """The product itself: Alpha AND Beta AND Gamma, straight at the API."""
    r = api("/BetterJellyfinFiltering/Intersect", [
        ("userId", USER_ID), ("parentId", LIB), ("includeItemTypes", "Movie"),
        ("groups", "Alpha"), ("groups", "Beta"), ("groups", "Gamma")])
    names = sorted(i["Name"] for i in r.get("Items", []))
    return names == ["All Three"], f"{r.get('TotalRecordCount')} items {names}"


def main() -> int:
    config_path = f"/Plugins/{PLUGIN_ID}/Configuration"

    print("0. baseline")
    check("the plugin injects the client script", injected())

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(f"{BASE}/web/", wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(3000)
        page.fill("#txtManualName", USER)
        page.fill("#txtManualPassword", PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_timeout(5000)

        print("\n1. the page is reachable from the dashboard")
        page.goto(f"{BASE}/web/#/configurationpage?name=Intersect", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        check("the dashboard route renders the page",
              page.locator("#IntersectConfigPage").count() == 1)
        check("...with the settings form", page.locator("#IntersectConfigForm").count() == 1)

        print("\n2. it shows the server's current settings")
        live = api(config_path)
        shown = page.evaluate("""() => ({
            inject: document.getElementById('InjectClientScript').checked,
            maxCriterionIds: document.getElementById('MaxCriterionIds').value,
            defaultPageSize: document.getElementById('DefaultPageSize').value,
            maxPageSize: document.getElementById('MaxPageSize').value
        })""")
        check("the injection toggle reflects the server",
              shown["inject"] == (live.get("InjectClientScript") is not False),
              f"page={shown['inject']} server={live.get('InjectClientScript')}")
        check("...and so do the numeric settings",
              int(shown["maxCriterionIds"]) == live["MaxCriterionIds"]
              and int(shown["defaultPageSize"]) == live["DefaultPageSize"]
              and int(shown["maxPageSize"]) == live["MaxPageSize"],
              f"page={shown} server={ {k: live[k] for k in ('MaxCriterionIds','DefaultPageSize','MaxPageSize')} }")

        print("\n3. saving a number survives the round trip")
        # The failure this catches: an <input> hands back a string, and the server's
        # properties are ints, so assigning the raw value makes the save 400 - which the
        # dashboard reports as a toast the test would never see.
        page.fill("#DefaultPageSize", "137")
        page.locator("#IntersectConfigForm button[type=submit]").click()
        page.wait_for_timeout(3000)
        check("a numeric setting saves as a number",
              api(config_path).get("DefaultPageSize") == 137,
              f"server={api(config_path).get('DefaultPageSize')}")

        page.fill("#DefaultPageSize", "100")
        page.locator("#IntersectConfigForm button[type=submit]").click()
        page.wait_for_timeout(3000)
        check("...and can be set back", api(config_path).get("DefaultPageSize") == 100,
              f"server={api(config_path).get('DefaultPageSize')}")

        print("\n4. turning the UI off restores a stock client")
        set_injection(page, False)
        page.locator("#IntersectConfigForm button[type=submit]").click()
        page.wait_for_timeout(3000)
        check("the setting was saved", api(config_path).get("InjectClientScript") is False,
              f"server={api(config_path).get('InjectClientScript')}")
        check("...and the shell no longer carries the script", not injected())
        check("...with no leftover <script> from us",
              "__JF_INTERSECT__" not in shell())

        print("\n5. ...but the endpoint is untouched")
        ok, detail = endpoint_still_works()
        check("the intersection still answers with the UI off", ok, detail)

        print("\n6. turning it back on restores the UI")
        set_injection(page, True)
        page.locator("#IntersectConfigForm button[type=submit]").click()
        page.wait_for_timeout(3000)
        check("the setting was saved", api(config_path).get("InjectClientScript") is True,
              f"server={api(config_path).get('InjectClientScript')}")
        check("...and the shell carries the script again", injected())

        print("\n7. no client errors")
        check("the dashboard raised no uncaught errors", not errors, "; ".join(errors[:3]))
        page.screenshot(path=str(OUT / "config-page-test.png"))

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
