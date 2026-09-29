# Findings — browse-view injection prototype (§12.1)

Date: 2026-09-28
Jellyfin server: **12.1.0**
jellyfin-web: **v12.1**, commit `fae41f33eb7cd636a9ef68984adb82bb247a6e1b` (tag date 2026-09-14)

Every claim below is either a `file:line` citation at the pinned revision or a
measurement from a reproducible script in `prototype/`. Nothing is from memory.

---

## 1. Environment corrections to the kickoff

**The kickoff's §4 client claims are correct, but were derived from a checkout
that does not match them.** The only `jellyfin-web` tree left in `/tmp` was at
`master` = **13.0.0** (commit `4e2831e`), which §4 explicitly warns against
reading. The claims were re-verified here against the real `v12.1` tag, fetched
into a separate worktree. They hold. See §3.

**`limit=0` does not mean "unlimited" — this likely closes an open question.**
§7 records an unexplained anomaly: an unfiltered `/Items` call with `parentId`
returned **1,356** while the library held **~5,614**, and says *"do not assume
`limit=0` means unlimited."*

Measured on 12.1 against the fixture library:

| Request | `TotalRecordCount` | `Items` returned |
|---|---|---|
| `/Items?parentId=…&recursive=true&limit=0` | 6 | **0** |
| `/Items?parentId=…&recursive=true&limit=100` | 6 | 6 |

`limit=0` still computes the count but returns **no items**. A caller that read
`Items.length` (rather than `TotalRecordCount`) from a `limit=0` request would
observe a smaller "library" than the real one. That is consistent with the
1,356-vs-5,614 discrepancy. **This is a strong hypothesis, not a proof** — the
original call is not preserved, so it cannot be re-run.

**Correction to our own fixture prose.** An earlier draft of
`prototype/make-fixtures.sh` asserted `(Alpha|Beta) AND (Gamma|Delta)` = 4,
counting `Gamma Delta`. That item has neither Alpha nor Beta, so the correct
value is **3**. The harness caught the error. Per §13: trust the test.

---

## 2. Server-side behaviour (re-confirmed on 12.1)

Auth, against the live server:

| Header | Result |
|---|---|
| `X-Emby-Token: <key>` | **401** |
| `Authorization: MediaBrowser Token="<key>"` | **200** |

Confirms §3 exactly.

**API keys are not user-bound.** `GET /Users/Me` with a valid API key returns
`{Name: null, Id: null, …}`. Consequences:

- API keys are fine for direct REST calls (`/Library/VirtualFolders` returns
  all libraries; `/Users/{id}/Items` works when the id is passed explicitly).
- An API key **cannot** authenticate the web client, which needs a real user
  session (`UserId` + user token). The browser prototype therefore requires a
  user login, not just a key.

**The delimiter trap reproduces exactly as described** (§3). All counts from
`prototype/verify-fixtures.py`, which passes in full:

| Query | Result | Meaning |
|---|---|---|
| `genres=Alpha` | 4 | |
| `genres=Beta` | 4 | |
| `genres=Alpha\|Beta` | 5 | union (OR) |
| `genres=Alpha&genreIds=<Beta>` | 3 | intersection (AND) |
| `genreIds=<Alpha>,<Beta>` | 5 | union (OR) |
| `genreIds=<Alpha>\|<Beta>` | **6** | **filter silently dropped** |

6 is the unfiltered count: the pipe makes the GUID parse fail and the whole
filter is discarded with **no error**. This is asserted permanently as a
regression case.

**The 2-group ceiling holds.** `(Alpha|Beta) AND (Gamma|Delta)` = 3 via the
API. A true 3-way `Alpha AND Beta AND Gamma` = 1 — **not expressible**, because
only `genres` and `genreIds` exist. This is the plugin's reason to exist.

---

## 3. Client-side verification (§4 claims, at v12.1)

**§4.1 confirmed** — `getFiltersQuery` returns a hardcoded whitelist that
**omits `genreIds`**: `src/utils/items.ts:138-156`.

**§4.2 confirmed** — the `Filters` interface declares `Genres?: string[]` and
other fields but **no `genreIds`**: `src/types/library.ts:35-50`.

**§4.3 confirmed** — settings are persisted through
`getSettingsKey(settingsViewType, settingsKey)` into `useLocalStorage`:
`src/apps/modern/features/libraries/hooks/useLibrary.tsx:50-53`.

**New finding — the block is narrower than "four independent blockers" implies.**
`genreIds` is *not* blocked at the transport layer:

- `useGetItems(parametersOptions: LibraryApiGetItemsRequest)` accepts the SDK
  request type, which includes `genreIds` — `src/hooks/useFetchItems.ts:54`.
- `GenresSectionContainer.tsx:45` already passes `genreIds: [genre.Id]` in
  production code.

So the only thing preventing `genreIds` from reaching `/Items` through the
filter UI is the single mapping function `getFiltersQuery`. The wall is one
function wide, not four. This does **not** change the §5 architecture decision
(patching that function still requires modifying the shipped bundle), but it
does mean an HTTP-layer shim would work for ≤2 groups with very little code —
which is precisely the option §5 rejects as buying nothing over the raw API.

---

## 4. The injection mechanism — §12.1 result

**Prototype:** `prototype/browse-injection-probe.py`,
`prototype/filter-survival-test.py`, with the payload under test in
`prototype/inject.js`. The `/web/index.html` intercept mirrors the planned
`IStartupFilter` transform byte for byte (strip `Content-Encoding` /
`Content-Length` / `ETag` / `Last-Modified`, insert `<script defer>` before
`</body>`).

**Result: 12/13 checks pass.** The mechanism is viable. Verdict per question:

| Question | Result |
|---|---|
| Does an injected `<script defer>` execute in the SPA? | **Yes** |
| Does a node appended inside the filter popover survive a React re-render caused by changing a native filter? | **Yes** |
| Does it survive the popover closing? | **No** — destroyed, as predicted |
| Does a MutationObserver re-mount it on reopen, with state intact? | **Yes** |
| Is it visible without scrolling? | **No** — see §5 |

**The structural fact that drives the design:** the native filter panel is a MUI
`<Popover id="filter-popover">` (`filter/FilterButton.tsx:197-218`) with
`keepMounted` left at its default `false`. React therefore **destroys the entire
subtree every time the panel closes.** Measured: `disappearances=1` on close,
`mounts=2` after one reopen.

Consequences, all verified:

1. All real state must live in a **module-level object**, never the DOM. After a
   close/reopen cycle the module state was restored intact
   (`selected: ['Alpha','Beta']`, panel text `2-way intersection: Alpha AND Beta`)
   while every DOM node had been recreated.
2. The UI must be re-mounted by a **MutationObserver**, debounced to an
   animation frame.
3. State changes must update the panel **in place**. Replacing the node on each
   change destroys focus and invalidates external references — the first
   iteration did exactly that and the harness caught it.

The accordions are **exclusive** (`setExpanded(newExpanded ? panel : false)`,
`FilterButton.tsx:108-112`): only one can be open at a time. Stable anchors are
`#filtersGenres-header` / `#filtersGenres-content`.

**Native policy, for contrast:** `FiltersGenres.tsx:18-38` toggles genres into
`Filters.Genres[]`, which `getFiltersQuery` maps to `genres=A|B` → **OR**. The
stock UI can only ever express a union.

---

## 5. Problems the prototype surfaced

These are the reason §12.1 demanded a prototype rather than reasoning.

**A. Mount storm (fixed).** The first implementation inserted the panel with
`insertAdjacentElement('afterend', …)` — as a *sibling* of the accordion — but
tested for an existing mount with `parentElement === accordion`. The check never
matched, so the observer rebuilt the panel on **every mutation of the document**:
measured **`mounts=92`** during a single popover open, and `476` by the end of
the run. Fixed by testing membership against the *container*
(`container.querySelector(...)`); now `mounts=1` per open.

**B. Wrong-popover and duplicate mounts (fixed).** Targeting `.MuiPopover-paper`
is unsafe: other client popovers (user menu, cast) render the same class, and
the panel mounted into the wrong one and accumulated duplicates. Fixed by
anchoring on filter-specific content (`#filtersGenres-header`) and purging any
panel found outside the resolved container.

**C. Injection timing (fixed).** The script runs at `DOMContentLoaded`, when
`window.ApiClient` does not yet exist, and — once it does on the login page —
`getCurrentUserId()` is still `null`. Loading the genre list once at startup
therefore failed silently and never retried. Fixed by loading lazily when the
panel is first mounted, which implies a signed-in user on a library page.

**D. Options-source drift (avoided).** Our genre list is fetched from
`GET /Items/Filters`, the same endpoint the native panel uses
(`useGetQueryFiltersLegacy`, `useFetchItems.ts:113-133`), so our options cannot
diverge from the client's. `userId` is required; omitting it is not fatal for
this endpoint on 12.1 but is passed explicitly.

**E. The panel is not visible without scrolling (open — design decision).**
The native paper is `width: 250` and `maxHeight: '50%'`
(`FilterButton.tsx:210-217`). Measured on a 900px-tall viewport: paper height
**450px**, `scrollHeight` **632px**, `overflowY: auto`. Our panel sits at
`top=487 … bottom=629` while the paper's bottom is **539** — so it renders
**entirely below the visible area** and needs a 182px scroll to reach. It is
reachable, not unreachable, but a user opening the filter panel does not see it.

---

## 6. Recommendations

**On the §12.1 decision (inject into browse vs. own page):** injection is
**technically viable** — the survival mechanism works and is now proven. But it
is materially harder than it looks, for reasons that did not appear until it was
actually run:

- The panel is destroyed on every close, forcing module-level state plus
  observer-driven re-mounting.
- Four distinct hazards were found and fixed in one afternoon (A–D above); a
  shipped plugin must carry all of them permanently.
- The popover's fixed 250px/50% geometry leaves no good place to put a
  multi-group builder (E).

**Placement, before committing.** Appending after all accordions is the worst
option tested. Better candidates, in rough order: prepend the panel so it is
always visible; inject *inside* the expanded Genres accordion beneath the native
list (matches the user's mental model, costs no extra height, but is only
visible while that accordion is open — and the accordions are exclusive); or
give our panel its own scroll container. This should be settled before the
server controller is written, because it determines how much UI space exists.

**Do not ship the observer as written.** Observing `document.documentElement`
with `subtree: true` fires on every mutation in a busy client. It is cheap once
`mount()` is correct, but the production version should observe a narrower
subtree and disconnect when the SPA tears down.

**Next cheapest useful step.** The 3-group case is the entire justification for
the plugin and has *not* been built yet — only the shell UI that would drive it.
A minimal server controller returning an N-way intersection, called from the
injected panel and rendered into the existing grid, would prove the whole thesis
end to end. Note the react-query cache-key hazard in §4 before touching the
grid.

---

## 7. Reproducing

```bash
cd prototype
bash make-fixtures.sh          # deterministic library, exact genre overlaps
docker compose up -d           # throwaway Jellyfin on :8097
python3 setup-server.py        # wizard, admin, library, scan
python3 verify-fixtures.py     # server-side counts — all pass
python3 filter-survival-test.py  # §12.1 injection experiment
```

The production server on `:8096` is never touched. The prototype instance uses
its own config volume, network, and container name.
