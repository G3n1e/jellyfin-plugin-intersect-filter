#!/usr/bin/env python3
"""Provision the throwaway prototype server (default :8097).

Drives the Jellyfin startup wizard, creates an admin user, adds a "Movies"
library pointing at the generated fixtures, and waits for the scan to finish.

CRITICAL: every metadata fetcher and image fetcher is explicitly disabled.
If they were left on, Jellyfin would reach out to online providers and inject
real genres onto the fixture items, destroying the ground truth the
verification harness asserts against (see make-fixtures.sh for expected counts).

Writes credentials to prototype/out/credentials.json (gitignored).
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8097"
ADMIN_USER = "protoadmin"
ADMIN_PASS = "prototype123"
CLIENT_AUTH = (
    'MediaBrowser Client="Jellyfin Proto", Device="Setup", '
    'DeviceId="proto-setup-script", Version="1.0.0"'
)
OUT = pathlib.Path(__file__).parent / "out" / "credentials.json"


def call(method: str, path: str, body=None, token: str | None = None):
    """Issue a request, returning (status, parsed-json-or-None)."""
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f'MediaBrowser Token="{token}"')
    elif path.startswith("/Users/AuthenticateByName"):
        req.add_header("Authorization", CLIENT_AUTH)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw.strip().startswith(b"{") else raw.decode(errors="replace"))


def wait_healthy(timeout=90):
    for _ in range(timeout):
        try:
            status, _ = call("GET", "/System/Info/Public")
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main() -> int:
    if not wait_healthy():
        print("server never became healthy on :8097", file=sys.stderr)
        return 1

    status, info = call("GET", "/System/Info/Public")
    print(f"server up: version={info['Version']} wizard_completed={info['StartupWizardCompleted']}")

    if not info["StartupWizardCompleted"]:
        # 1. locale / metadata defaults
        st, _ = call("POST", "/Startup/Configuration", {
            "UICulture": "en-US",
            "MetadataCountryCode": "US",
            "PreferredMetadataLanguage": "en",
        })
        print(f"  POST /Startup/Configuration -> {st}")

        # 2. first admin user.
        #    This can 404 if issued while the user manager is still coming up
        #    (observed: immediately after Configuration on a cold server), so
        #    retry rather than treating a 404 as fatal.
        for attempt in range(1, 6):
            st, _ = call("POST", "/Startup/User", {"Name": ADMIN_USER, "Password": ADMIN_PASS})
            if st in (200, 204):
                break
            print(f"  POST /Startup/User          -> {st} (retry {attempt}/5)")
            time.sleep(2)
        print(f"  POST /Startup/User          -> {st}")

        # 3. finish
        st, _ = call("POST", "/Startup/Complete")
        print(f"  POST /Startup/Complete      -> {st}")
    else:
        print("  wizard already completed; reusing existing admin")

    # 4. authenticate as the admin to get a *user* access token.
    #    An API key is not user-bound (/Users/Me is empty), which the web client
    #    cannot use; a real user token is required for the browser prototype.
    st, auth = call("POST", "/Users/AuthenticateByName", {"Username": ADMIN_USER, "Pw": ADMIN_PASS})
    if st != 200 or not isinstance(auth, dict):
        print(f"auth failed: {st} {auth}", file=sys.stderr)
        return 1
    token = auth["AccessToken"]
    user_id = auth["User"]["Id"]
    print(f"  authenticated: user={auth['User']['Name']} id={user_id}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"base": BASE, "token": token, "userId": user_id,
                               "user": ADMIN_USER, "password": ADMIN_PASS}, indent=2))
    OUT.chmod(0o600)
    print(f"  credentials -> {OUT}")

    # 5. create the Movies library with all fetchers disabled.
    st, existing = call("GET", "/Library/VirtualFolders", token=token)
    names = [f.get("Name") for f in (existing or [])]
    if "Movies" in names:
        print("  library 'Movies' already exists; skipping creation")
    else:
        no_fetch = {
            "MetadataFetchers": [],
            "MetadataFetcherOrder": [],
            "ImageFetchers": [],
            "ImageFetcherOrder": [],
        }
        lib_options = {
            "PathInfos": [{"Path": "/media/Movies"}],
            "EnableRealtimeMonitor": False,
            "EnableChapterImageExtraction": False,
            "MetadataFetchers": [],
            "MetadataFetcherOrder": [],
            "ImageFetchers": [],
            "ImageFetcherOrder": [],
            "TypeOptions": [{"Type": "Movie", **no_fetch}],
        }
        st, resp = call(
            "POST",
            "/Library/VirtualFolders?name=Movies&collectionType=movies&refreshLibrary=false",
            {"LibraryOptions": lib_options},
            token=token,
        )
        print(f"  POST /Library/VirtualFolders -> {st} {resp if st >= 400 else ''}")

    # 6. scan and wait for it to settle.
    call("POST", "/Library/Refresh", token=token)
    print("  scan triggered; waiting for items to appear...")

    virtual = None
    st, folders = call("GET", "/Library/VirtualFolders", token=token)
    for f in folders or []:
        if f.get("Name") == "Movies":
            virtual = f.get("ItemId")

    deadline = time.time() + 120
    last = -1
    while time.time() < deadline:
        st, items = call("GET", f"/Items?parentId={virtual}&recursive=true&limit=0", token=token)
        count = (items or {}).get("TotalRecordCount", 0)
        if count != last:
            print(f"    items so far: {count}")
            last = count
        if count >= 6:
            break
        time.sleep(3)

    st, genres = call("GET", "/Genres?parentId=" + str(virtual) + "&userId=" + user_id, token=token)
    print(f"  final item count: {last}")
    print(f"  genres: {[g.get('Name') for g in (genres or {}).get('Items', [])]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
