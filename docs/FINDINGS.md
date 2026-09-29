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

**Next step — build the server controller.** The 3-group case is now proven
client-side (§5), so what remains is moving it server-side where it belongs. The
client prototype is *not* shippable as-is and should not be mistaken for the
design: it fetches one full genre set per selected genre and intersects in the
browser, which is fine for 6 fixtures and untenable for a real library (N
requests, each unbounded). The server controller does the intersection in one
query, and is the only place that can report a correct `TotalRecordCount` for a
paginated result.

Two hazards carry over and must be handled there, not discovered later:

- **Never page a set you intend to intersect** (§5). This is the failure that
  looks like a normal answer.
- **The react-query cache key** (§4 of the kickoff) is built before the HTTP
  layer. If the grid is driven by our query, the key must not collide with the
  native one, or a filtered result will be served to — or from — the native view.

Once the controller exists, the surface question is already answered: its own
dialog or page, reached from a button, not the filter popover (§6E).

---

## 8. Reproducing

```bash
cd prototype
bash make-fixtures.sh          # deterministic library, exact genre overlaps
docker compose up -d           # throwaway Jellyfin on :8097
python3 setup-server.py        # wizard, admin, library, scan
python3 verify-fixtures.py     # server-side counts — all pass
python3 filter-survival-test.py  # §12.1 injection experiment
python3 placement-test.py        # §6E placement comparison (3 variants)
python3 nway-test.py             # §5 the 3-group intersection — 19/19 pass
```

The production server on `:8096` is never touched. The prototype instance uses
its own config volume, network, and container name.
