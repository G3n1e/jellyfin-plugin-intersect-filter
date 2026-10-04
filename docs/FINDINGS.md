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

**Root cause of the delimiter trap — a doc/code mismatch, found while writing the
controller.** `Jellyfin.Api/Controllers/ItemsController.cs` documents `genreIds`
as *"Optional. If specified, results will be filtered to include only those
containing the specified genre ids. Pipe ('|') separated for OR results."*
(line 165) — but binds it with `CommaDelimitedCollectionModelBinder` (line 257),
which splits on **commas only**. So the documented separator is not the one
implemented, and `genreIds=<A>|<B>` reaches the binder as a single unparseable
GUID. `CommaDelimitedCollectionModelBinder.GetParsedResult` catches the
`FormatException`, logs at **Debug**, and drops the element; with nothing left,
the parameter binds empty and no filter is applied at all.

The neighbouring `genres` parameter (line 222) correctly uses
`PipeDelimitedCollectionModelBinder`, which is why `genres=Alpha|Beta` unions and
`genreIds=<A>|<B>` silently unfilters. `studioIds` has the same doc/code mismatch
as `genreIds`. The trap is therefore not "Jellyfin mishandles pipes" but "two
parameters document the same separator and implement different ones", and the
silence is `CommaDelimitedCollectionModelBinder`'s catch-and-log-at-Debug.

This is the exact failure mode the plugin must not reproduce: unparseable input
is a hard error there, never a dropped filter (§8).

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
| Is it visible without scrolling? | **Placement-dependent; no good placement exists** — §6E |

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

## 5. The N-way intersection — the thesis, proven

Everything above is about *how* to inject. This section is about whether the
thing being injected is worth anything, which had not been tested at all until
now. `prototype/nway-test.py`, **19/19 checks pass**.

The claim in §2 is that `/Items` ANDs across exactly two genre groups, so
`Alpha AND Beta AND Gamma` cannot be expressed. Measured live against the
fixture library:

| Expression | Result | Via |
|---|---|---|
| `Alpha\|Beta\|Gamma` (native union) | **6** — the entire library | `genres=` |
| `(Alpha\|Beta) AND Gamma` | **3** | `genres=` + `genreIds=` — **the ceiling** |
| `Alpha AND Beta AND Gamma` | **1** — "All Three" | **our resolve, 3 groups** |

Three genres each covering 4 of the 6 fixtures union to everything and intersect
to a single item. That gap is the product, and it is now demonstrated rather
than argued.

The 4-way case (`Alpha AND Beta AND Gamma AND Delta`) correctly returns **0**.
An empty intersection is a real answer, not an error, and code that treats it as
"no results yet" would be wrong.

**The trap that makes a naive implementation silently wrong.** Intersecting
*pages* is not intersecting *sets*. Resolving `Alpha AND Beta` with `limit=2`:

```
perGenre: Alpha total=4 returned=2 truncated=True
          Beta  total=4 returned=2 truncated=True
result:   2 items — [All Three, Alpha Beta One]      WRONG (truth is 3)
```

`Alpha Beta Two` falls outside the page, so the answer is quietly short by one.
It looks exactly like a normal result: two plausible movies, no error. The same
call at the default limit returns all three and sets `truncated=False`.
**A production implementation must never page a set it intends to intersect** —
it either fetches ids unpaged, or pushes the intersection to the server. The
client prototype carries an explicit `truncated` flag precisely so this failure
cannot pass silently, and the harness asserts the flag fires.

**The panel drives the same code path, not just the hook.** Selecting Alpha,
Beta and Gamma and clicking *Resolve intersection* renders
`AND(Alpha, Beta, Gamma) → 1 item(s)` — so the UI is wired to the real
resolution, not a stub.

---

## 6. Problems the prototype surfaced

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

**E. The popover has no room for this UI (open — design decision).**
The native paper is `width: 250` and `maxHeight: '50%'`
(`FilterButton.tsx:210-217`). Measured on a 900px-tall viewport with the Genres
accordion expanded — the state a user is actually in when picking genres — the
paper is **450px** tall with **632px** of content and `overflowY: auto`. It is
**182px over-subscribed before our panel is added at all**, and our panel is a
further **142px**.

That reframes the question. Placement does not decide *whether* something falls
below the fold; it decides *what*. All three variants measured
(`prototype/placement-test.py`), viewport 1440x900, same payload in all three:

| Placement | Panel top…bottom | Visible on open | Stable across reopen | Ancestor |
|---|---|---|---|---|
| `after-accordion` | 487…629 | **No** (paper ends 539) | yes | paper |
| `prepend` | 97…239 | **Yes** | **yes** | paper (first child) |
| `inside-genres` | 293…435 → 471…630 | yes → **No** | **No** | *changes between mounts* |

The baseline reproduces the original failure exactly (487/629/539, 182px scroll),
so the harness is faithful.

`prepend` is the only placement that is both visible and **deterministic** — it
is the paper's first child on open and on reopen alike, with state intact and
exactly one panel. But it is visible *at the native filters' expense*: the
expanded Genres accordion then runs to 637, **98px below the fold**. We would be
pushing the controls the user opened the panel for out of view to make room for
ourselves.

`inside-genres` looked attractive and is not viable. Its mount point **depends on
whether the accordion happened to be expanded at the instant `mount()` ran**:
- first mount (accordion still collapsed) → `.MuiAccordionDetails-root` does not
  exist yet, so the code falls back to the accordion root; the panel becomes a
  sibling of the collapse region;
- reopen (accordion already expanded) → the panel goes *inside* the region
  (`MuiAccordionDetails-root` → `MuiCollapse-wrapper` → root).

Two different parents for one mode. Worse, once inside the collapse region the
panel is destroyed whenever the user collapses Genres — and on reopen it measured
471…630, i.e. below the fold again. The instability is structural, not a bug to
patch.

**Conclusion: the native popover cannot host a multi-group builder.** A 250px
column that already overflows by 182px has no placement that keeps both the
native genre list and an intersect panel visible. This is evidence *against*
injecting into the filter popover, and for giving the panel its own surface.

---

## 7. Recommendations

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

**Placement — settled, against the popover.** All three candidates were
implemented and measured (§6E). `after-accordion` is deterministic but invisible;
`inside-genres` is unstable and dies with the accordion; `prepend` is
deterministic and visible but only by pushing the native genre list below the
fold. None is good, and the reason is arithmetic rather than taste: the popover
holds 450px and its own content is 632px. **Do not build the multi-group builder
inside the filter popover.** Give it its own surface — a dialog, a drawer, or the
plugin's own page. The popover remains useful as the *entry point* (a button
that opens that surface), which is cheap and needs no placement decision.

This is settled *before* the server controller is written, as intended: it
determines how much UI space exists, and the answer is "as much as we want, but
not inside a 250px popover".

**Do not ship the observer as written.** Observing `document.documentElement`
with `subtree: true` fires on every mutation in a busy client. It is cheap once
`mount()` is correct, but the production version should observe a narrower
subtree and disconnect when the SPA tears down.

**The server controller is built and verified** (§8). What remains is the client:
the injection/UI work of §4–§6, plus the react-query cache key below.

Two hazards carried over into the controller and were handled there rather than
discovered later:

- **Never page a set you intend to intersect** (§5). Handled: criteria are read
  whole, the cap is enforced by fetching one extra id, and an over-cap criterion
  is refused rather than truncated.
- **The react-query cache key** (§4 of the kickoff) is built before the HTTP
  layer and is *not* the controller's to solve. If the grid is driven by our
  query, the key must not collide with the native one, or a filtered result will
  be served to — or from — the native view.

Once the controller exists, the surface question is already answered: its own
dialog or page, reached from a button, not the filter popover (§6E).

---

## 8. The server controller (§7 next step, built)

`src/BetterJellyfinFiltering/` — a plugin exposing
`GET /BetterJellyfinFiltering/Intersect`. `groups` is repeated once per
AND-group, `|` ORs names within a group, so `?groups=Alpha|Beta&groups=Gamma`
is `(Alpha OR Beta) AND Gamma` and three repetitions are a 3-way intersection the
stock API cannot express.

**Shape of the implementation, and the decision behind it.** The service holds an
`IItemRepository` and asks it one question per criterion, then intersects the id
sets in memory. The alternative — one hand-written EF Core query over
`BaseItemEntity`/`ItemValueMap` — was rejected: it would have to re-derive
placeholder exclusion, genre matching, item-type filters, virtual-item handling
and user rating/tag restrictions. Intersecting sets the real pipeline produced is
correct by construction; re-implementing the filters is the "looks plausible, is
wrong" failure this project exists to remove. The earlier worry that passing a
large `ItemIds` would produce an enormous `IN (...)` list was disproved:
`filter.ItemIds` binds as a **single EF collection parameter** (`json_each` /
`OPENJSON`), so no chunking is needed.

### The bug classes found building it

**1. `filter.ParentId` means *direct children*, and `Recursive` is a no-op.**
This was the blocker and it is the most dangerous finding here, because the wrong
version returns a plausible empty answer rather than an error.

```csharp
// BaseItemRepository.TranslateQuery.cs:320-322
if (!filter.ParentId.IsEmpty())
{
    baseQuery = baseQuery.Where(e => e.ParentId!.Value == filter.ParentId);
}
```

`Recursive` is never read anywhere in `Jellyfin.Server.Implementations/` or
`src/Jellyfin.Database/`. The stock `/Items?parentId=X&recursive=true` works only
because `LibraryManager` rewrites the query on the way down —
`GetItemsResult` calls `SetTopParentIdsOrAncestors`, which turns a recursive
parent into `AncestorIds` (plain folders) or `TopParentIds` (libraries), and a
BoxSet/Playlist with linked children into `DescendantOfId`. Set `ParentId` and
hand the query to the repository directly and you get only the folder's immediate
children: for a library whose movies sit one level deeper, **zero results, no
error**, indistinguishable from "this filter matched nothing".

`AncestorIds` is a closure in the database — `ItemPersistenceService` writes one
row per ancestor, not just the parent — so a single id matches at any depth.

The service now mirrors `SetTopParentIdsOrAncestors` in one documented method
(`ApplyParentScope`), and `GenreIntersectionServiceTests` pins it: a recursive
folder parent must produce `AncestorIds == [folderId]` and `ParentId == Empty`.
The mirror is checked against the stock route on real data by the harness, so a
drift from it fails a test rather than shipping.

**2. An empty `ItemIds` means "no id filter", not "match nothing".**
`TranslateQuery.cs:1046` guards `if (filter.ItemIds.Length > 0)`, so handing the
query an empty id array **skips the id filter and returns the whole library**.
An empty intersection is a real and common outcome (any contradictory filter), so
this is the difference between "no results" and "everything". The service returns
before touching the repository when the intersection is empty, and
`ExecutePage` throws `InvalidOperationException` if it is ever handed an empty
set — unreachable by construction, asserted because the failure mode is silent.

**3. The stock delimiter trap is a doc/code mismatch** (§2). The plugin reads
`groups` off `Request.Query` as raw `StringValues` rather than binding a
`string[]`, because ASP.NET Core splits array-typed query parameters on
**commas**, which would tear a group apart.

A second, subtler version of the same trap appeared in the plugin's own parser:
splitting on `|` with `RemoveEmptyEntries` silently *repairs* `Alpha||Beta` into
`Alpha|Beta` and `Alpha|` into `Alpha`, accepting a filter the caller never
wrote. Empty entries are now kept and reported. Repairing malformed input is the
same failure as dropping it.

**4. `Jellyfin.Api` is not a published NuGet package** (404 on nuget.org), so a
plugin cannot reference it. The one thing needed from it — the
`"Jellyfin-UserId"` claim string — is duplicated as a literal with a comment;
it is part of the wire contract between server and client, not an internal
detail.

**5. `SetUser` is not the same as `ConfigureUserAccess`.** `SetUser` applies the
parental-rating limit, blocked unrated items and blocked tags. `ILibraryManager.
ConfigureUserAccess` additionally scopes an otherwise-unscoped query to the
libraries that user may see. A filter endpoint calling only `SetUser` would
return items from libraries the user cannot browse.

**6. Jellyfin's REST API serializes PascalCase** (`TotalRecordCount`, `Items`),
so the plugin's records match that rather than the camelCase default. Error
payloads are a typed record for the same reason; an anonymous object would
serialize camelCase and be the one inconsistent response.

### Verification

- `tests/BetterJellyfinFiltering.Tests/` — 53 unit tests over the parser, the
  service's own algebra (with a faked repository that records what was asked), and
  the shell rewriter. Covers all four kickoff §8.2 areas: the delimiter
  asymmetry, AND-vs-OR per category, the empty-list-means-all trap, and pagination
  bounds. The rewriter tests also assert against the client script that actually
  ships — that it is embedded, that it cannot terminate its own `</script>`, and
  that the route string it calls is the controller's.
- `prototype/server-controller-test.py` — 42 checks against a live server,
  re-deriving ground truth from the stock API at run time rather than hardcoding
  it, so a stale expectation cannot make a wrong answer pass. Includes the
  empty-intersection landmine, the stock 2-group ceiling reproduced side by side,
  and authorization (401 unauthenticated, 403 for a signed-in caller naming
  another user).

Both pass. `prototype/deploy-plugin.sh` builds, installs and restarts the
throwaway instance; production on `:8096` is never touched.

### Still open

- The assembly name, namespace and route are still `BetterJellyfinFiltering` /
  `Jellyfin.Plugin.BetterFiltering` while the product is now
  `jellyfin-plugin-intersect-filter`. The client hardcodes that route, so a
  rename has to move both together. Flagged, not done.
- Non-recursive queries pass `ParentId` straight through, which is the
  repository's own meaning of direct-children but skips the in-memory
  `UserViewBuilder` filtering the stock route applies to children. This differs
  only for virtual and user-specific items, and library filtering is recursive,
  so it is recorded rather than worked around.

---

## 9. The client half (§4–§6, built)

The server endpoint returns an intersection; nothing in the web client can *ask*
for one. This section is how the client was made to, and what only a browser
could have told us.

### What ships

- `Client/ScriptInjectingStartupFilter.cs` — an `IStartupFilter` registered
  through `IPluginServiceRegistrator`. It registers its middleware before calling
  `next(app)`, which puts it **outermost**, ahead of the `UseStaticFiles` that
  serves the shell.
- `Client/IndexHtmlRewriter.cs` — the transform, kept pure so it can be unit
  tested. It inserts before the **last** `</body>`, so a `</body>` quoted inside a
  comment does not swallow the payload, and it refuses a script containing
  `</script` rather than shipping a half-executed one.
- `Client/intersect-client.js` — plain JS, no build step, embedded as a resource
  and inlined into the shell. Embedded rather than copied next to the DLL because
  an installed plugin folder is replaced wholesale and the deploy script sweeps
  only `*.dll`/`*.json`; a loose `.js` would survive an upgrade and go stale
  against the server it ships with.

### Inline, not a separate asset

The script is inlined as `<script>…</script>`. A `<script src>` would need its
own fetchable URL, and the shell is served **before sign-in**, so there is no
token to splice into it — it would need a new anonymous route on the plugin.
The server also sets **no `Content-Security-Policy`** anywhere (verified by grep
across the server source), so an inline script is permitted. Inlining removes the
extra route, the extra round trip and the stale-asset problem at the cost of a
larger document: the shell went from 5441 bytes on disk to 35324 served at the time of
writing, and ~47800 once the focus-trap and route-change fixes were added (§9).

Two consequences of rewriting a response rather than a file:

- The middleware strips the request's `Accept-Encoding` and `Range` before
  calling `next`, because a gzipped body cannot be edited as text and a range
  fragment does not contain `</body>` at all. Stripping the *response*
  `Content-Encoding` would be too late to be useful.
- It then removes `ETag`, `Last-Modified`, `Vary` and `Content-Encoding` from
  the rewritten response. The server marks the shell `no-cache`, so browsers
  revalidate it on every load; leaving the validators on would let a revalidation
  answer 304 against the *patched* entity tag while the browser replays whatever
  it cached — on the first load after an upgrade, the previous version's shell.

Only `GET` is rewritten. A `HEAD` response has no body and no `</body>` to insert
before, so the static handler's own length and validators pass through
untouched — they describe the unpatched file, which is what a `HEAD` of it is.

### Its own results grid, and why that dissolves §7

Kickoff §7 anticipated a fight with the react-query cache key. That fight belongs
to the rejected XHR-interceptor approach. §4 already showed nothing in the
client's data path can carry a third criterion — `getFiltersQuery`'s whitelist
omits `genreIds`, and the `Filters` object has no field for it — so a UI that
only manipulated the native filter state could not express the request at all.
The plugin therefore renders its **own** grid from its own fetch, which never
enters the native query cache. §7 is not solved; it is avoided.

Placement follows the prototype's result: the entry button goes in the native
filter popover (`#filtersGenres-header`'s paper), and the dialog is appended to
`document.body`, **outside** `#reactRoot`. React therefore never unmounts it —
closing the popover that opened it does not close the dialog. Result cards hand
off to the native `#/details?id=` route rather than reimplementing a detail view.

### The bug that only a browser could find

The observer that mounts the entry button was disconnected on `pagehide` and
never reconnected. In the real client **the document never reloads** — measured
at one document, one `body` node across the whole session — and signing in fires
`pagehide` and then `pageshow` on that *same* document, a millisecond apart. So
the sequence was: script runs → observer attaches → user signs in → `pagehide`
disconnects it → the injection is dead for the rest of the page's life. The user
gets no entry button until they reload by hand.

Every unit test passed throughout. The instrument that caught it was a counter
set added to the shipped script (`diagnostics.observerFires`), which sat frozen
at 3 while a probe observer attached alongside it counted 27 mutations in the
same period. The fix is the pair: disconnect on `pagehide`, `watch()` again on
`pageshow`.

### The harness proved a three-way OR and called it a pass

The first run of `client-test.py` reported `Alpha or Beta or Gamma: 6 → 6` and
six results — the union. The fault was the harness: it clicked the first element
matching `[data-jf-intersect="genre-option"]` with the right text **across the
whole document**, and every group's option list is in the DOM at once, so group
0's unfiltered list always matched first and swallowed every genre. The plugin's
own criteria trace is what made it legible; a bare "6 results" assertion would
have been tuned until it passed. The options list now carries its group index,
and the harness scopes to it.

### The settings page

The plugin injects into a shell the web client does not intend anyone to modify, so it
needs a way to be turned off that does not involve uninstalling it or hand-editing XML —
and the person who needs that is an administrator on a server other people are using, not
someone with a shell open. `Configuration/configPage.html` is declared by
`Plugin.GetPages()` and served by the host out of the assembly at
`configurationpage?name=Intersect`.

Both halves are load-bearing. **The off switch is the point**: it restores a stock web
client without touching the endpoint, so a future web-client upgrade that changes the
shell's shape is a settings change rather than an outage. The page also exposes the three
bounds (`MaxCriterionIds`, `DefaultPageSize`, `MaxPageSize`), which until now were editable
only by editing XML.

Two details worth keeping:

- The page is registered as `Intersect`, not as the plugin's display name, because the name
  becomes a URL parameter and the display name carries an em dash and spaces for no benefit.
- Configuration is read per request, so the toggle takes effect on the next page load. No
  restart, which is the difference between a usable escape hatch and a theoretical one.

The page deviates from the official template in one place: the template assigns an
`<input>`'s value straight onto the config object, but an input hands back a **string** and
these properties are `int`, so the save would be rejected. Ours parses. That is asserted
rather than assumed — the harness saves 137 and reads it back.

### The genre box could not be typed into at all

Found by using it, not by testing it. The box carried the placeholder **"Add a genre…"** but
its only listener was `input`, which narrowed the option list; the file's only `keydown`
handler was Escape-to-close. So **Enter did nothing**, every genre had to be found in the
list and clicked, and a genre that `Items/Filters` did not return could not be selected at
all. A control that labels itself an entry box and behaves as a filter is a defect however
well it is tested — and the harness passed throughout, because it drove the box the way the
code worked rather than the way the label promised.

**That was the second defect, not the first.** With Enter implemented the box *still* could
not be typed into, and the reported symptom — "doesn't allow input" — was the first one
surviving underneath. The cause is a focus trap, and it is worth stating precisely because
nothing in our own code is wrong:

> Jellyfin's filter popover is a MUI `Popover`, which is a `Modal` underneath. A `Modal`
> enforces focus: while it is open it pulls focus back to its own container the instant
> focus escapes. Our dialog is appended to `document.body` **on purpose**, outside that
> popover, so the genre box could be clicked, focused for an instant, and then have focus
> yanked away before a single key arrived. Every keystroke went to the popover.

Measured on 12.1, same build, the only variable being whether the popover was open:

| native popover | `document.activeElement` after clicking the box | typing `Gamma` |
|---|---|---|
| open | the popover paper | value stayed `''` through all five keys |
| closed | our input | `G` → `Ga` → `Gam` → `Gamm` → `Gamma` |

The fix is to dismiss the popover as the dialog opens — `closeNativePopover()`, which
dispatches MUI's own dismissal sequence on the modal's backdrop. That is also what a user
expects: choosing "Filter by several genres…" replaces the small popover with the dialog
rather than stacking one on the other. It is scoped through our own entry button's
`.MuiModal-root` rather than a document-wide backdrop query, because the first
`.MuiBackdrop-root` on the page belongs to an unrelated menu. The "dialog outlives the
popover" property asserted in §9 is what makes this safe rather than a reload.

**Why the harness missed it.** `locator.fill()` assigns a value and never moves focus. It
will happily fill an input that no human can reach, so every check passed against an
untypable box. `type_genre` now sends real key events through the keyboard, and Enter goes
through `page.keyboard.press` rather than `locator.press()` — the latter focuses the element
first, which would paper over exactly the focus loss under test. The harness also asserts
outright that the box holds focus when clicked. The general lesson is the same one §9
already records twice: a harness that drives a control the way the code happens to work
cannot find the cases where it doesn't.

Two consequences, both worth keeping:

- **`Items/Filters` is not a complete genre list.** It is scoped to the view being browsed,
  so treating it as authoritative makes a real genre unreachable. The box now accepts a
  typed name that is not in the list, and the chip is *marked* rather than refused or
  silently accepted — a dashed outline and a tooltip, since colour alone does not survive
  being described to someone over the phone.
- **The criteria trace was hidden in exactly the case that needs it.** `results()` returned
  early on an empty intersection, before building the trace, so a typo produced "No items
  have every one of those genres" with nothing to say which criterion did it. The trace is
  now built first and shown on the empty branch, where a bad name reads as
  `Zzz Not A Genre: 0 → 0` and names itself. That is what makes accepting an unlisted name
  honest rather than a new way to produce a plausible-but-wrong answer.

The `|` guard moved from `applyFilter` to the point of entry. It used to refuse the *entire*
filter after the offending chip already existed; now the chip is never created. Same rule,
better failure. `applyFilter` keeps its copy as a backstop for state set programmatically.

### Verification

`prototype/config-page-test.py` — **14 checks** driving the real dashboard: the page
renders, it reflects the server's current settings, a numeric setting survives a save and a
restore, and — the reason it exists — turning the injection off actually removes the script
from the served shell *while the endpoint keeps answering*, then turning it back on
restores it.

`prototype/client-test.py` — **45 checks**, against the real server with no route
interception, since the plugin performs the injection itself and patching the
page would let a broken plugin pass. It asserts that the entry appears exactly
once, that the dialog sits outside `#reactRoot`, that it survives the popover
closing, that three groups driven through the real picker yield exactly the
intersection, that removing a group widens back to the previous answer, that
`buildUrl` emits one `groups` parameter per group with no comma-joining, that a
card opens the native details route, and that the page raises no uncaught errors.
Ground truth is re-derived from the stock API at run time.

Six of those checks exist because of the two defects above, and they drive the box the
way the label promises rather than the way the code happened to work: the native popover
dismisses itself when the dialog opens, the genre box actually holds focus when clicked,
typing a genre and pressing Enter — clicking nothing — adds it, a name the library does
not list is accepted and marked, an empty result still shows the trace naming the criterion
that matched nothing, and ArrowDown before Enter adds the highlighted genre rather than
whatever sorted first. Before that the harness drove the picker with `fill()` and by
clicking options, so it was structurally incapable of noticing either that Enter was dead
or that the box could not take focus.

This is also where the divergence from kickoff §13 lives: §13 proposed joining
`jellyfin.db` directly. Every harness here re-derives truth from the stock REST
API instead, which keeps them honest against the same server the plugin runs on
and needs no database coupling.

---

## 10. Reproducing

```bash
cd prototype
bash make-fixtures.sh          # deterministic library, exact genre overlaps
docker compose up -d           # throwaway Jellyfin on :8097
python3 setup-server.py        # wizard, admin, library, scan
python3 verify-fixtures.py     # server-side counts — all pass
python3 filter-survival-test.py  # §12.1 injection experiment
python3 placement-test.py        # §6E placement comparison (3 variants)
python3 nway-test.py             # §5 the 3-group intersection — 19/19 pass
bash deploy-plugin.sh            # build + install + restart the plugin
python3 server-controller-test.py  # §8 the server endpoint — 42/42 pass
python3 client-test.py             # §9 the shipped client — 32/32 pass
python3 config-page-test.py        # §9 the settings page — 14/14 pass
```

```bash
dotnet test tests/BetterJellyfinFiltering.Tests   # §8/§9 unit suite — 53/53 pass
```

The production server on `:8096` is never touched. The prototype instance uses
its own config volume, network, and container name.
