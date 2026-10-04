# Architecture

For maintainers. How the plugin works, why it is built this way, what will break on a Jellyfin
upgrade, and how to fix it.

Every factual claim about Jellyfin carries a `file:line` citation at a pinned revision or a
measurement from a script in `prototype/`. Where something could not be verified it says so.
`docs/FINDINGS.md` is the fuller research record; this document is the map.

**Pinned versions**

| | Version | Commit |
|---|---|---|
| Jellyfin server | **12.1.0** | — |
| jellyfin-web | **v12.1** | `fae41f33eb7cd636a9ef68984adb82bb247a6e1b` (tagged 2026-09-14) |
| Plugin | 0.1.0.0 | `targetAbi` 12.1.0.0 |

> `jellyfin-web` master is **13.0.0**, unreleased. Do not read master and assume it describes
> 12.x. This bit the project once already (FINDINGS §1).

---

## 1. The shape of the thing

```
                     ┌──────────────────────────────────────────┐
  browser  ────────► │ IStartupFilter middleware                │
                     │   rewrites /web/index.html               │
                     │   inserts <script>…client…</script>      │
                     └──────────────────────────────────────────┘
                                        │
                     ┌──────────────────▼───────────────────────┐
                     │ injected UI (plain JS, no build step)    │
                     │   entry button in the filter popover     │
                     │   dialog on document.body                │
                     └──────────────────┬───────────────────────┘
                                        │  GET /BetterJellyfinFiltering/Intersect
                                        │      ?groups=Action&groups=Comedy
                     ┌──────────────────▼───────────────────────┐
                     │ IntersectController                      │
                     │   GroupParser → criteria                 │
                     │   GenreIntersectionService               │
                     │     one IItemRepository query per group  │
                     │     intersect the id sets in memory      │
                     └──────────────────────────────────────────┘
```

The key design decision: **the intersection is done on the server, by intersecting the id sets
the real query pipeline produced.** The alternative — one hand-written EF Core query over
`BaseItems`/`ItemValuesMap` — was rejected because it would have to re-derive placeholder
exclusion, genre matching, item-type filters, virtual-item handling and user rating/tag
restrictions. Intersecting sets that the real pipeline produced is correct by construction;
re-implementing those filters is the "looks plausible, is wrong" failure this project exists to
remove.

The earlier worry that passing a large `ItemIds` list would produce an enormous `IN (...)`
was disproved: `filter.ItemIds` binds as a **single EF collection parameter** (`json_each` /
`OPENJSON`), so no chunking is needed.

---

## 2. Query semantics — what the server can and cannot do

### 2.1 AND across parameters, OR within one

`Jellyfin.Server.Implementations/Item/BaseItemRepository.TranslateQuery.cs` adds one
`baseQuery = baseQuery.Where(...)` per criterion, and EF Core composes consecutive `Where`
calls with **AND**. Values inside a single parameter are a single `Where`, so they are **OR**.

Measured live on a 5,614-item library:

| Query | Result | Meaning |
|---|---|---|
| `genres=Action` | 260 | |
| `genres=Comedy` | 223 | |
| `genres=Action\|Comedy` | 456 | union — OR |
| `genres=Action&genreIds=Comedy` | **27** | intersection — AND |

`260 + 223 − 27 = 456`. Re-verify with `prototype/verify-fixtures.py`.

### 2.2 The ceiling is two groups

There are exactly two genre parameters. `MediaBrowser.Controller/Entities/InternalItemsQuery.cs`
declares `Genres` and `GenreIds` and nothing else — no `ExcludeGenreIds`, no third lever. So the
best the stock API can express is `(A or B) and (C or D)`. Beating that ceiling is this plugin's
entire reason to exist; wiring up `genreIds` alone would build something the API already offers.

### 2.3 The delimiter trap — the single most important gotcha

`Jellyfin.Api/Controllers/ItemsController.cs`, v12.1:

| Param | Type | Binder | Separator |
|---|---|---|---|
| `genres` | `string[]` | `PipeDelimitedCollectionModelBinder` (line 222) | `\|` |
| `genreIds` | `Guid[]` | `CommaDelimitedCollectionModelBinder` (line 257) | `,` |

`genreIds` is **documented** at line 165 as *"Pipe ('|') separated for OR results"* — but it is
**bound** on commas only. So the documented separator is not the implemented one. A `|` in
`genreIds` makes the whole string fail to parse as a GUID,
`CommaDelimitedCollectionModelBinder.GetParsedResult` catches the `FormatException`, logs at
**Debug**, and drops the element; with nothing left the parameter binds empty and **no filter is
applied at all**. Measured: `genreIds=<A>|<B>` returns the unfiltered count.

`studioIds` has the same doc/code mismatch. The trap is not "Jellyfin mishandles pipes" — it is
"two parameters document the same separator and implement different ones", and the silence is
one catch block logging at Debug.

**This is the failure mode the plugin must never reproduce.** Every malformed input here is a
hard error (`GroupParser`, and the `errors` list in `IntersectController.Intersect`). Notes:

- `groups` is read off `Request.Query` as raw `StringValues`, **not** bound as `string[]`,
  because ASP.NET Core's default binder splits array-typed query parameters on **commas** and
  would tear a group apart.
- `GroupParser` splits on `|` *without* `RemoveEmptyEntries`. Splitting with it would silently
  **repair** `Alpha||Beta` into `Alpha|Beta` and `Alpha|` into `Alpha`, accepting filters the
  caller never wrote. Repairing malformed input is the same failure as dropping it.

### 2.4 The other two landmines, both silent

**`filter.ParentId` means *direct children*, and `Recursive` is a no-op.**
`TranslateQuery.cs:320-322`:

```csharp
if (!filter.ParentId.IsEmpty())
{
    baseQuery = baseQuery.Where(e => e.ParentId!.Value == filter.ParentId);
}
```

`Recursive` is never read anywhere in `Jellyfin.Server.Implementations/` or
`src/Jellyfin.Database/`. The stock `/Items?parentId=X&recursive=true` works only because
`LibraryManager.GetItemsResult` calls `SetTopParentIdsOrAncestors`, rewriting a recursive parent
into `AncestorIds` (plain folders) or `TopParentIds` (libraries). Set `ParentId` and hand the
query straight to the repository and you get only the folder's immediate children — for a
library whose movies sit one level deeper, **zero results, no error**, indistinguishable from
"this filter matched nothing".

The service mirrors `SetTopParentIdsOrAncestors` in one documented method (`ApplyParentScope`),
and `GenreIntersectionServiceTests` pins it: a recursive folder parent must produce
`AncestorIds == [folderId]` and `ParentId == Empty`. That mirror is also checked against the
stock route on real data by `prototype/server-controller-test.py`, so drift fails a test rather
than shipping.

**An empty `ItemIds` means "no id filter", not "match nothing".**
`TranslateQuery.cs:1046` guards `if (filter.ItemIds.Length > 0)`, so an empty id array skips the
filter and returns **the whole library**. An empty intersection is a real and common outcome
(any contradictory filter), so this is the difference between "no results" and "everything". The
service returns before touching the repository when the intersection is empty, and `ExecutePage`
throws if it is ever handed an empty set — unreachable by construction, asserted because the
failure mode is silent.

### 2.5 Never page a set you intend to intersect

Intersecting *pages* is not intersecting *sets*. Measured, resolving `Alpha AND Beta` with
`limit=2`:

```
perGenre: Alpha total=4 returned=2 truncated=True
          Beta  total=4 returned=2 truncated=True
result:   2 items — [All Three, Alpha Beta One]      WRONG (truth is 3)
```

One item fell outside the page and the answer is quietly short. It looks like a normal result:
two plausible movies, no error. Handled by reading criteria whole, enforcing the cap by fetching
one extra id, and **refusing** an over-cap criterion rather than truncating it.

### 2.6 Authorization

`SetUser` is not the same as `ConfigureUserAccess`. `SetUser` applies the parental-rating limit,
blocked unrated items and blocked tags; `ILibraryManager.ConfigureUserAccess` additionally scopes
an otherwise-unscoped query to the libraries that user may see. A filter endpoint calling only
`SetUser` would return items from libraries the user cannot browse. Both are applied.

API keys are **not user-bound** (FINDINGS §2): `GET /Users/Me` with a valid key returns
`{Name: null, Id: null}`. A signed-in caller is therefore always scoped to itself and may not
name another `userId` (403); an API-key caller must name one to get user-scoped filtering.

### 2.7 Wire format

Jellyfin's REST API serializes **PascalCase** (`TotalRecordCount`, `Items`), so the plugin's
response records match that rather than the camelCase default. Error payloads are a typed record
for the same reason — an anonymous object would serialize camelCase and be the one inconsistent
response in the API.

`Jellyfin.Api` is **not published on NuGet** (404), so a plugin cannot reference it. The one
thing needed from it, the `"Jellyfin-UserId"` claim string, is duplicated as a literal with a
comment: it is part of the wire contract between server and client, not an internal detail.

---

## 3. The client injection

### 3.1 Why `IStartupFilter` and not File Transformation

`Jellyfin.Plugin.FileTransformation` works on 12.x today but is structurally fragile:

- Its own FAQ: *"plugins are strictly 1 version compatible"* — every point release needs a
  rebuild.
- Open issue #84 (2026-09-10) reporting it broken on 12.1.
- A history of DI race conditions (#46/#52/#55) that silently disable **all** transformations on
  restart.
- Jellyfin Enhanced [PR #531](https://github.com/n00bcodr/Jellyfin-Enhanced/pull/531) documents
  a *"File Transformation DI race condition (not fixable from JE)"* affecting every dependent
  plugin until a container restart.

Jellyfin Enhanced — a maintained, popular plugin — has already migrated off it to
`IStartupFilter`. This plugin does the same:

1. `Client/ScriptInjectingStartupFilter.cs` registers an `IStartupFilter` via
   `IPluginServiceRegistrator`.
2. It registers its middleware **before** calling `next(app)`, which puts it **outermost**, ahead
   of the `UseStaticFiles` that serves the shell.
3. `Client/IndexHtmlRewriter.cs` is the transform, kept pure so it can be unit tested.

### 3.2 Details that are load-bearing

**Rewriting a response, not a file.** The middleware strips the request's `Accept-Encoding` and
`Range` before calling `next`, because a gzipped body cannot be edited as text and a range
fragment does not contain `</body>` at all. Stripping the *response* `Content-Encoding` would be
too late to be useful. It then removes `ETag`, `Last-Modified`, `Vary` and `Content-Encoding`
from the rewritten response: the server marks the shell `no-cache`, so browsers revalidate it on
every load, and leaving the validators on would let a revalidation answer `304` against the
**patched** entity tag while the browser replays whatever it cached — on the first load after an
upgrade, the previous version's shell.

**Insert before the last `</body>`**, so a `</body>` quoted inside a comment cannot swallow the
payload. The rewriter refuses a script containing `</script` rather than shipping a
half-executed one.

**Inline, not `<script src>`.** A separate asset would need its own fetchable URL, and the shell
is served **before sign-in**, so there is no token to splice into it — it would need a new
anonymous route. The server also sets **no `Content-Security-Policy`** anywhere (verified by
grep across the server source), so an inline script is permitted. Inlining removes the extra
route, the extra round trip and the stale-asset problem at the cost of a larger document: the
shell grew from 5,441 bytes on disk to ~47,800 served (the client script is ~42,300 of that,
and it grew as the focus-trap and route-change fixes were added).

**The script is embedded in the assembly**, not copied next to the DLL. An installed plugin
folder is replaced wholesale, and the deploy script sweeps only `*.dll`/`*.json`, so a loose
`.js` would survive an upgrade and go stale against the server it ships with.

**Only `GET` is rewritten.** A `HEAD` response has no body and no `</body>` to insert before, so
the static handler's own length and validators pass through untouched — they describe the
unpatched file, which is what a `HEAD` of it is.

### 3.3 The client script

`Client/intersect-client.js` — plain JS, no build step.

**State lives in a module-level object, never in the DOM.** The native filter panel is a MUI
`<Popover id="filter-popover">` (`filter/FilterButton.tsx:197-218`) with `keepMounted` at its
default `false`, so React **destroys the entire subtree every time the panel closes**. Measured:
`disappearances=1` on close, `mounts=2` after one reopen, with module state intact
(`selected: ['Alpha','Beta']`) while every DOM node had been recreated.

**The UI is re-mounted by a MutationObserver**, debounced to an animation frame, observing
`document.body` (`childList`, `subtree`). A narrower subtree is not available — the popover is
mounted under `body` by the client itself.

**Membership is tested against the container, not the accordion.** Testing against the accordion
rebuilt the panel on every mutation of the document: measured at **92 mounts during a single
popover open**. Anchoring on filter-specific content (`#filtersGenres-header`) rather than
`.MuiPopover-paper` also avoids mounting into an unrelated popover — the user menu and cast menu
render the same paper class.

**The dialog goes on `document.body`, outside `#reactRoot`**, so React never unmounts it and
closing the popover does not close the dialog. Result cards hand off to the native
`#/details?id=` route rather than reimplementing a detail view.

**`pagehide` disconnects the observer; `pageshow` reconnects it.** This pair is not optional. In
the real client the document **never reloads** — measured at one document, one `body` node
across a whole session. Signing in fires `pagehide` and then `pageshow` on that *same* document,
a millisecond apart. Disconnecting on `pagehide` alone killed the injection for the rest of the
page's life: the user signed in and got no entry button until they reloaded by hand. Every unit
test passed throughout; it was found by a counter in the shipped script
(`diagnostics.observerFires`) sitting frozen at 3 while a probe observer counted 27 mutations in
the same window.

**The dialog is closed on `hashchange`.** It lives outside React, so it is not part of any
route's tree and nothing unmounts it when the client navigates. Left alone it stayed on screen
over the next page. The web client routes on the hash, so a hash change is the navigation
signal.

### 3.4 The focus trap — why the native popover is dismissed on open

Jellyfin's filter popover is a MUI `Popover`, which is a `Modal` underneath. **A `Modal`
enforces focus**: while open it pulls focus back to its own container the instant focus escapes.
Because the dialog is appended to `document.body` on purpose — outside that popover — the genre
box could be clicked, focused for an instant, and stripped of focus before a keystroke arrived.
Every keystroke went to the popover.

Measured on 12.1, same build, only the popover's state varying:

| native popover | `document.activeElement` after clicking the box | typing `Gamma` |
|---|---|---|
| open | the popover paper | value stayed `''` through all five keys |
| closed | our input | `G` → `Ga` → `Gam` → `Gamm` → `Gamma` |

`closeNativePopover()` therefore dismisses the popover as the dialog opens, by dispatching MUI's
own dismissal sequence on the modal's backdrop. Two things about it are deliberate:

- The events are dispatched **on the element**, not sent as a click at coordinates, because our
  dialog covers the viewport and a real click would land on that instead.
- The modal root is resolved **through our own entry button's `.MuiModal-root`**, not by a
  document-wide `.MuiBackdrop-root` query — the first `.MuiBackdrop-root` on the page belongs to
  an unrelated menu.

If you touch the dialog's mounting strategy, re-check this. It is invisible in code review and
only reproduces with **real keystrokes**: `locator.fill()` assigns a value and never moves
focus, so it will pass against a box no human can type into. That is exactly how it shipped once.

---

## 4. Upgrade playbook

**The web client is the fragile surface, not the server API.** Every upgrade risk below is in
the injection, not the intersection.

### On each Jellyfin release

1. **Check the plugin ABI.** `targetAbi` in `meta.json` must match the server's
   `AssemblyVersion`. Jellyfin refuses a plugin whose ABI does not match.
2. **Diff `jellyfin-web`'s `index.html`.** Does it still contain `</body>`? That is the entire
   contract the injection depends on.
3. **Diff the filter popover.** These are the anchors and the script breaks silently if they
   move:
   - `#filtersGenres-header` / `#filtersGenres-content` — the mount anchor. If the Genres
     accordion is renamed, `findFilterAnchor()` returns null and **no entry button appears**.
   - `.MuiPopover-paper` — the container the entry is inserted into.
   - `#filter-popover` — the MUI modal root, used by `closeNativePopover()`.
4. **Check the MUI version.** The focus-trap behaviour in §3.4 is MUI `Modal` semantics. A major
   MUI upgrade could change how a popover is dismissed, and `closeNativePopover()` would need
   revisiting.
5. **Check `getFiltersQuery`** (`src/utils/items.ts`) and the `Filters` interface
   (`src/types/library.ts`). The plugin does not use them — it renders its own grid from its own
   fetch, which is why the react-query cache-key problem in kickoff §7 is avoided rather than
   solved — but a change there is the signal that the client's own filter plumbing moved.
6. **Run the harnesses.** `prototype/client-test.py` is the one that catches injection breakage;
   it drives the real web client with no route interception, so a broken plugin cannot pass.

### What typically breaks

| Symptom | Likely cause | Check |
|---|---|---|
| No filter button at all | Mount anchor renamed, or the shell no longer has `</body>` | §4 steps 2–3 |
| Button appears, dialog won't open | Entry mounted into the wrong popover | `mountEntry()`'s container test |
| Dialog opens, genre box won't accept typing | MUI focus trap returned — see §3.4 | `closeNativePopover()` |
| Styles look unstyled | Inline script blocked by a new CSP | Server response headers |
| Works until sign-in, then gone | `pagehide`/`pageshow` reconnection broke | §3.3 |

### What will not break

The `/BetterJellyfinFiltering/Intersect` endpoint. It touches only the documented server
pipeline. If the web client breaks, the API keeps answering and the settings page still turns
the injection off.

---

## 5. Open questions

Recorded rather than quietly omitted.

**The 1,356 baseline — unresolved.** On the originating server, an unfiltered `/Items` call with
`parentId` returned **1,356** while the library held **~5,614** items. Never explained.

A strong hypothesis, measured on 12.1 against the fixture library:

| Request | `TotalRecordCount` | `Items` returned |
|---|---|---|
| `/Items?parentId=…&recursive=true&limit=0` | 6 | **0** |
| `/Items?parentId=…&recursive=true&limit=100` | 6 | 6 |

`limit=0` still computes the count but returns **no items**. A caller reading `Items.length`
rather than `TotalRecordCount` from a `limit=0` request would observe a smaller "library" than
the real one — consistent with the 1,356-vs-5,614 discrepancy. **This is a hypothesis, not a
proof**: the original call was not preserved and cannot be re-run.

The practical rule stands regardless: **do not assume `limit=0` means unlimited, and do not use
an unfiltered call as a baseline without checking it against the database.**

**Assembly, namespace and route names.** The assembly is `Jellyfin.Plugin.BetterFiltering` and
the route is `/BetterJellyfinFiltering/Intersect`, while the product is
`jellyfin-plugin-intersect-filter`. The client hardcodes that route, so a rename has to move
both together. Deliberately left alone; flagged so it is a decision rather than an oversight.

**Non-recursive queries.** They pass `ParentId` straight through, which is the repository's own
meaning of direct-children, but they skip the in-memory `UserViewBuilder` filtering the stock
route applies to children. This differs only for virtual and user-specific items, and library
filtering is recursive, so it is recorded rather than worked around.

**Duplicate entity names with distinct UUIDs.** Common in real libraries (one test library had
four separate `Blacked` studio records and three `Brazzers`). A UI that assumes name↔id is 1:1
will silently under-match. The client works in genre *names*, which sidesteps it for genres, but
any future category added to this plugin must handle multi-select across same-named ids.

---

## 6. Test strategy

| Harness | Checks | What it proves |
|---|---|---|
| `tests/BetterJellyfinFiltering.Tests` | 53 | The parser, the service's algebra (against a faked repository that records what was asked), and the shell rewriter. Covers all four areas kickoff §8.2 asks for: the delimiter asymmetry, AND-vs-OR per category, the empty-list-means-all trap, and pagination bounds. The rewriter tests assert against the client script that actually ships. |
| `prototype/verify-fixtures.py` | — | The delimiter trap, asserted permanently. |
| `prototype/server-controller-test.py` | 42 | The live endpoint, re-deriving ground truth from the stock API at run time. Includes the empty-intersection landmine, the stock 2-group ceiling reproduced side by side, and authorization (401, and 403 for a signed-in caller naming another user). |
| `prototype/client-test.py` | 45 | The real web client, no route interception. |
| `prototype/config-page-test.py` | 14 | The real settings page, including that turning injection off removes the script from the served shell **while the endpoint keeps answering**. |

**Ground truth is re-derived at run time, never hardcoded**, so a stale expectation cannot make a
wrong answer pass.

Two lessons the harnesses learned the hard way, both worth keeping:

- **A harness that drives a control the way the code works cannot find the cases where it
  doesn't.** The client harness once reported a passing three-way *OR*: it clicked the first
  matching option across the whole document, and every group's option list is in the DOM at
  once, so group 0's unfiltered list always won. It also once passed a genre box that no human
  could type into, because `fill()` never moves focus. Drive the UI the way the label promises.
- **The plugin's own criteria trace is a test instrument.** A bare "6 results" assertion would
  have been tuned until it passed; `Alpha or Beta or Gamma: 6 → 6` made the union legible at a
  glance. Keep it.
