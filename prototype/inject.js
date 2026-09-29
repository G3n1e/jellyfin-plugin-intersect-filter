/*
 * Client script injected into /web/index.html — the payload that will ship
 * inside the plugin.
 *
 * Design under test (§12.1). The native filter panel is a MUI <Popover> with
 * keepMounted={false}, so React destroys its subtree whenever the panel closes.
 * Anything appended inside is therefore transient. So:
 *
 *   - all real state lives in a module-level object (never in the DOM)
 *   - a MutationObserver re-mounts the UI whenever the popover appears
 *   - the UI is a pure rendering of that state
 *   - state changes update the existing nodes IN PLACE; the panel node is never
 *     replaced, so focus and scroll survive and external references stay valid
 *
 * Anchoring: the filter popover is located by a *content* anchor — the native
 * accordion header ids — rather than by `.MuiPopover-paper`. Other popovers in
 * the client (user menu, cast, etc.) also render a paper, and targeting that
 * class alone caused the panel to mount into the wrong popover and to
 * accumulate duplicates.
 */
(function () {
    'use strict';

    var PANEL_CLASS = 'jf-intersect-panel';
    var QUERY_ATTR = 'data-jf-intersect-query';

    // Module-level state: survives every React unmount/re-render.
    var state = {
        selected: [],        // genre names chosen for intersection
        genres: [],          // available genres
        genresFor: undefined,// parentId the genre list belongs to
        loadingFor: undefined,
        lastQuery: null,
        result: null,        // last resolved intersection, for display
        resolving: false
    };

    var probe = {
        loadedAt: Date.now(),
        readyStateAtInject: document.readyState,
        reactRootAtInject: !!document.getElementById('reactRoot'),
        mounts: 0,
        mounted: false,
        disappearances: 0,   // times the panel vanished (popover closed)
        duplicatesRemoved: 0,
        errors: []
    };
    window.__JF_PROBE__ = probe;
    window.__JF_INTERSECT__ = state;
    // Exposed so the harness can drive the intersection directly and compare
    // it against the server-side ground truth, independent of the panel UI.
    window.__JF_RESOLVE__ = function (names, opts) {
        return resolveIntersection(names, opts);
    };

    /**
     * Locate the *filter* popover by its content, not by a generic class:
     * several popovers exist in the client and all render `.MuiPopover-paper`.
     */
    function findFilterAnchor() {
        return document.getElementById('filtersGenres-header')
            || document.getElementById('filtersStatus-header')
            || document.getElementById('filtersStudios-header');
    }

    function describeQuery() {
        if (state.selected.length < 2) {
            return 'select 2+ genres → AND (have ' + state.selected.length + ')';
        }
        state.lastQuery = { genreNames: state.selected.slice() };
        return state.selected.length + '-way intersection: ' +
               state.selected.join(' AND ');
    }

    function el(tag, style, text) {
        var n = document.createElement(tag);
        if (style) n.style.cssText = style;
        if (text != null) n.textContent = text;
        return n;
    }

    function onResolve() {
        if (state.selected.length < 2) {
            state.result = { error: 'select 2 or more genres' };
            syncPanel();
            return;
        }
        state.resolving = true;
        state.result = null;
        syncPanel();
        resolveIntersection(state.selected.slice()).then(function (r) {
            state.resolving = false;
            state.result = r;
            syncPanel();
        });
    }

    function onToggle(name, checked) {
        var i = state.selected.indexOf(name);
        if (checked && i === -1) state.selected.push(name);
        if (!checked && i !== -1) state.selected.splice(i, 1);
        syncPanel();   // update in place — never replace the node
    }

    /** Build the panel once. */
    function buildPanel() {
        var wrap = el('div', [
            'border:2px dashed #7c4dff', 'border-radius:6px', 'padding:8px',
            'margin:8px', 'background:rgba(124,77,255,0.12)',
            'font:12px/1.4 system-ui,sans-serif', 'color:inherit'
        ].join(';'));
        wrap.className = PANEL_CLASS;
        wrap.setAttribute('data-jf-intersect', 'panel');

        wrap.appendChild(el('div', 'font-weight:600;margin-bottom:6px',
                            'Intersect genres (AND)'));

        var list = el('div', null, null);
        list.setAttribute('data-jf-intersect', 'list');
        wrap.appendChild(list);

        wrap.appendChild(el('div', 'margin-top:6px;opacity:.85;word-break:break-all',
                            describeQuery())).setAttribute(QUERY_ATTR, '1');

        var btn = el('button', 'margin-top:6px;padding:4px 8px;cursor:pointer',
                     'Resolve intersection');
        btn.setAttribute('data-jf-intersect', 'resolve');
        btn.addEventListener('click', onResolve);
        wrap.appendChild(btn);

        var out = el('div', 'margin-top:6px;opacity:.9;word-break:break-word', '');
        out.setAttribute('data-jf-intersect', 'result');
        wrap.appendChild(out);

        renderList(wrap);
        return wrap;
    }

    /** Rebuild just the checkbox rows from state. */
    function renderList(panel) {
        var list = panel.querySelector('[data-jf-intersect=list]');
        if (!list) return;
        list.textContent = '';
        if (!state.genres.length) {
            list.appendChild(el('div', 'opacity:.7', 'no genres loaded yet'));
            return;
        }
        state.genres.forEach(function (name) {
            var row = el('label', 'display:flex;align-items:center;gap:6px;cursor:pointer');
            var cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.value = name;
            cb.checked = state.selected.indexOf(name) !== -1;
            cb.addEventListener('change', function () { onToggle(name, cb.checked); });
            row.appendChild(cb);
            row.appendChild(el('span', null, name));
            list.appendChild(row);
        });
    }

    /** Update an existing panel in place, preserving node identity. */
    function syncPanel() {
        var panels = document.querySelectorAll('.' + PANEL_CLASS);
        if (!panels.length) return false;
        for (var i = 0; i < panels.length; i++) {
            var panel = panels[i];
            // reflect state onto existing checkboxes without recreating them
            var boxes = panel.querySelectorAll('input[type=checkbox]');
            var seen = 0;
            Array.prototype.forEach.call(boxes, function (b) {
                seen++;
                b.checked = state.selected.indexOf(b.value) !== -1;
            });
            if (seen !== state.genres.length) renderList(panel);
            var q = panel.querySelector('[' + QUERY_ATTR + ']');
            if (q) q.textContent = describeQuery();
            var out = panel.querySelector('[data-jf-intersect=result]');
            if (out) out.textContent = describeResult();
        }
        return true;
    }

    function describeResult() {
        if (state.resolving) return 'resolving…';
        var r = state.result;
        if (!r) return '';
        if (r.error) return 'error: ' + r.error;
        var lines = r.perGenre.map(function (g) {
            return g.name + '=' + g.total + (g.truncated ? ' (truncated!)' : '');
        }).join(', ');
        var head = 'AND(' + r.genreNames.join(', ') + ') → ' + r.items.length + ' item(s)';
        if (r.truncated) {
            head += ' — UNRELIABLE: a genre set was truncated by limit';
        }
        var titles = r.items.map(function (i) { return i.name; }).join(', ');
        return head + '\n  ' + lines + (titles ? '\n  ' + titles : '');
    }

    /**
     * Resolve the intersection of N genre selections.
     *
     * THE POINT OF THE WHOLE PLUGIN. The server's /Items accepts exactly two
     * genre groups (`genres`, pipe-delimited names; `genreIds`, comma-delimited
     * GUIDs) and ANDs across groups, so it can express (A|B) AND (C|D) but NOT
     * A AND B AND C. This does the N-way case by fetching one complete set per
     * genre and intersecting on the client.
     *
     * The correctness trap, and the reason this returns `truncated` rather than
     * just a list: intersecting *pages* is meaningless. If a genre has 400
     * items and we read the first 100, the result is the intersection of the
     * first 100 of each — a number that looks plausible and is simply wrong.
     * So each set is compared against its own TotalRecordCount and the caller
     * is told when a set was incomplete. A production implementation must
     * either fetch ids without paging or push the intersection to the server.
     */
    function resolveIntersection(genreNames, opts) {
        opts = opts || {};
        var limit = opts.limit || 1000;
        var pid = currentParentId();

        return new Promise(function (resolve) {
            whenApiReady(function (client) {
                var uid = client.getCurrentUserId ? client.getCurrentUserId() : null;
                if (!uid) { resolve({ error: 'not signed in' }); return; }

                function fetchOne(name) {
                    var q = '/Items?userId=' + encodeURIComponent(uid) +
                            '&recursive=true&includeItemTypes=Movie' +
                            '&enableImages=false&enableUserData=false' +
                            '&limit=' + limit +
                            '&genres=' + encodeURIComponent(name);
                    if (pid) q += '&parentId=' + encodeURIComponent(pid);
                    return client.getJSON(client.getUrl(q)).then(function (res) {
                        var items = res.Items || [];
                        return {
                            name: name,
                            total: res.TotalRecordCount,
                            returned: items.length,
                            truncated: res.TotalRecordCount > items.length,
                            items: items
                        };
                    });
                }

                Promise.all(genreNames.map(fetchOne)).then(function (sets) {
                    var out = {
                        genreNames: genreNames.slice(),
                        perGenre: sets.map(function (s) {
                            return { name: s.name, total: s.total,
                                     returned: s.returned, truncated: s.truncated };
                        }),
                        truncated: sets.some(function (s) { return s.truncated; }),
                        items: []
                    };
                    if (sets.length) {
                        // Intersect on Id, keeping the richest name/Id record.
                        var byId = {};
                        sets.forEach(function (s) {
                            s.items.forEach(function (it) { byId[it.Id] = it; });
                        });
                        var first = sets[0].items.map(function (i) { return i.Id; });
                        out.items = first.filter(function (id) {
                            return sets.every(function (s) {
                                return s.items.some(function (i) { return i.Id === id; });
                            });
                        }).map(function (id) {
                            return { id: id, name: byId[id] && byId[id].Name };
                        });
                    }
                    resolve(out);
                }).catch(function (e) { resolve({ error: String(e) }); });
            });
        });
    }

    function mount() {
        var anchor = findFilterAnchor();
        if (!anchor) {
            if (probe.mounted) probe.disappearances += 1;  // popover closed
            probe.mounted = false;
            return false;
        }

        // The popover being open implies a signed-in user on a library page,
        // which is the earliest safe moment to fetch the genre list.
        ensureGenres();
        var container = anchor.closest('.MuiPopover-paper');
        if (!container) return false;

        // Purge any panel that ended up outside this container (e.g. mounted
        // into a different popover earlier).
        var all = document.querySelectorAll('.' + PANEL_CLASS);
        Array.prototype.forEach.call(all, function (p) {
            if (!container.contains(p)) { p.remove(); probe.duplicatesRemoved += 1; }
        });

        // IMPORTANT: the panel is inserted as a *sibling* of the accordion, so
        // its parentElement is the container — NOT the accordion. Checking
        // against the accordion here caused a mount storm (a fresh panel on
        // every mutation). Always test membership against the container.
        if (container.querySelector('.' + PANEL_CLASS)) {
            probe.mounted = true;
            return true;
        }

        // Placement is selectable so the harness can measure the alternatives
        // against the popover's fixed 250px / maxHeight:50% geometry.
        //   after-accordion : below every accordion (first attempt; scrolls out of view)
        //   prepend         : first thing in the popover, always visible
        //   inside-genres   : inside the Genres accordion, under the native list
        var mode = window.__JF_PLACEMENT__ || 'after-accordion';
        var panel = buildPanel();
        var accordion = anchor.closest('.MuiAccordion-root');

        if (mode === 'prepend') {
            container.insertBefore(panel, container.firstChild);
        } else if (mode === 'inside-genres' && accordion) {
            var details = accordion.querySelector('.MuiAccordionDetails-root') || accordion;
            details.appendChild(panel);
        } else if (accordion) {
            accordion.insertAdjacentElement('afterend', panel);
        } else {
            container.appendChild(panel);
        }
        probe.placement = mode;
        probe.mounts += 1;
        probe.mounted = true;
        return true;
    }

    /**
     * The script is injected as <script defer>, so it runs BEFORE the SPA has
     * created window.ApiClient. Any API access must wait for that global to
     * appear — bailing out immediately silently produced an empty genre list.
     */
    function whenApiReady(cb, tries) {
        tries = tries || 0;
        if (window.ApiClient) return cb(window.ApiClient);
        if (tries > 120) {           // ~30s
            probe.errors.push('ApiClient never appeared');
            return;
        }
        setTimeout(function () { whenApiReady(cb, tries + 1); }, 250);
    }

    function currentParentId() {
        try {
            var m = location.hash.match(/topParentId=([a-f0-9]+)/i);
            return m ? m[1] : null;
        } catch (e) { return null; }
    }

    /**
     * Load the genre list lazily, the first time the panel is actually shown.
     *
     * Loading once at startup does NOT work: the script runs on the login page,
     * where ApiClient already exists but no user is authenticated, so
     * getCurrentUserId() returns null and the request fails. There is nothing
     * to retry it — so drive this from the panel appearing instead, which
     * implies a signed-in user on a library page.
     *
     * Uses the SAME endpoint as the native filter panel
     * (useGetQueryFiltersLegacy -> GET /Items/Filters) so our option list can
     * never drift from the client's.
     */
    function ensureGenres() {
        var pid = currentParentId();
        if (state.genresFor === pid && state.genres.length) return;
        if (state.loadingFor === pid) return;

        whenApiReady(function (client) {
            var uid = client.getCurrentUserId ? client.getCurrentUserId() : null;
            if (!uid) return;   // not signed in yet; retried on the next mount
            state.loadingFor = pid;
            if (!probe.apiReadyAt) probe.apiReadyAt = Date.now();

            var q = '/Items/Filters?userId=' + encodeURIComponent(uid);
            if (pid) q += '&parentId=' + encodeURIComponent(pid);
            q += '&includeItemTypes=Movie';

            client.getJSON(client.getUrl(q))
                .then(function (res) {
                    state.genres = (res.Genres || []).slice().sort();
                    state.genresFor = pid;
                    state.loadingFor = null;
                    probe.genresLoaded = state.genres.length;
                    syncPanel();
                })
                .catch(function (e) {
                    state.loadingFor = null;
                    if (probe.errors.length < 10) probe.errors.push('genres: ' + e);
                });
        });
    }

    function start() {
        console.log('[jf-intersect] injected; readyState=' + document.readyState);

        var scheduled = false;
        new MutationObserver(function () {
            if (scheduled) return;
            scheduled = true;
            requestAnimationFrame(function () { scheduled = false; mount(); });
        }).observe(document.documentElement, { childList: true, subtree: true });

        mount();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
