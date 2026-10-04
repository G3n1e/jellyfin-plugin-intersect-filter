/*
 * Intersect — Multi-Criteria Filtering. Client script, injected into /web/index.html.
 *
 * Why this file exists at all: the modern React client cannot be asked to filter by
 * genre intersection. `getFiltersQuery` (src/utils/items.ts) is a hardcoded whitelist that
 * omits `genreIds`, and `Filters` (src/types/library.ts) has no field for it, so nothing in
 * the client's data path will forward a third criterion. A UI-only plugin is therefore not
 * possible: the results have to be fetched and rendered here. See docs/FINDINGS.md 4.
 *
 * Where the UI lives, and why:
 *
 *   - The entry point is a button prepended into the native filter popover. That popover is
 *     a MUI <Popover> with keepMounted left false, so React destroys its subtree every time
 *     it closes - measured, docs/FINDINGS.md 4 - and anything with real state cannot live
 *     there.
 *   - The dialog is appended to document.body, NOT to the popover, so closing the popover
 *     cannot take it with it. Being outside #reactRoot also means React's delegated event
 *     listeners never see our events, so the two UIs cannot fight over a click.
 *   - All real state lives in `state` below, never in the DOM.
 *
 * The popover is located by a *content* anchor - the native accordion header ids - rather
 * than by `.MuiPopover-paper`. Other popovers in the client (user menu, cast) render the
 * same class, and targeting it alone mounted the panel into the wrong one and accumulated
 * duplicates.
 *
 * Plain JavaScript on purpose: no build step, so the repository stays buildable with the
 * .NET SDK alone and this file is exactly what ships.
 */
(function () {
    'use strict';

    var ENDPOINT = 'BetterJellyfinFiltering/Intersect';
    var GENRE_SEPARATOR = '|';

    // Stable hooks for the verification harness. The prototype's harnesses keyed on the same
    // convention, and every selector a test uses should be one of these rather than a class.
    var SEL_ENTRY = '[data-jf-intersect="entry"]';
    var DIALOG_ID = 'jf-intersect-dialog';

    // The browse routes this script understands, mapped to the item type they browse. A
    // genre list is only meaningful for the type on screen: the genres of Series are not the
    // genres of Movie, and asking /Items/Filters without a type mixes them.
    var ROUTE_ITEM_TYPES = {
        movies: 'Movie',
        tvshows: 'Series',
        music: 'MusicAlbum',
        musicvideos: 'MusicVideo',
        homevideos: 'Video',
        boxsets: 'BoxSet',
        books: 'Book'
    };

    var state = {
        // One array of genre names per AND group; a group's names are ORed together.
        // Starts as a single empty group so the dialog opens with somewhere to type.
        groups: [[]],
        genres: [],
        genresFor: undefined,
        // The in-progress text of each group's genre filter box, per group index. Kept here
        // rather than read from the DOM so a re-render can restore it.
        pickers: {},
        // Which row of each group's filtered option list the keyboard is on, per group index.
        // Enter acts on this, so arrowing down and pressing Enter is predictable instead of
        // adding whatever happened to sort first.
        highlight: {},
        open: false,
        busy: false,
        error: null,
        result: null
    };

    // Counters rather than logs: a user reporting "the button never appeared" can read these
    // from the console, and the verification harness asserts on them. `lastSkip` names the
    // branch mountEntry bailed at, which is the difference between "the popover never opened",
    // "it opened but we could not find our anchor" and "we inserted and something removed it".
    var diagnostics = {
        errors: [],
        mounts: 0,
        mountCalls: 0,
        observerFires: 0,
        lastSkip: null
    };

    // ---------------------------------------------------------------- utilities

    function el(tag, attrs, children) {
        var node = document.createElement(tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (key) {
                var value = attrs[key];
                if (value === null || value === undefined) return;
                if (key === 'class') node.className = value;
                else if (key === 'text') node.textContent = value;
                else if (key.indexOf('on') === 0) node.addEventListener(key.slice(2), value);
                else node.setAttribute(key, value);
            });
        }
        (children || []).forEach(function (child) {
            if (child) node.appendChild(child);
        });
        return node;
    }

    /** The library currently being browsed, from the hash route. */
    function currentRoute() {
        var hash = window.location.hash || '';
        var query = hash.indexOf('?');
        var path = (query < 0 ? hash : hash.slice(0, query)).replace(/^#\/?/, '');
        return {
            view: path.split('/')[0],
            params: new URLSearchParams(query < 0 ? '' : hash.slice(query + 1))
        };
    }

    function currentParentId() {
        var params = currentRoute().params;
        return params.get('topParentId') || params.get('parentId') || null;
    }

    function currentItemType() {
        return ROUTE_ITEM_TYPES[currentRoute().view] || null;
    }

    // ---------------------------------------------------------------- server calls

    /**
     * Builds the request URL for a set of groups.
     *
     * The query string is assembled here rather than handed to `ApiClient.getUrl` as a params
     * object, because `groups` must appear once per AND group. Letting a generic serialiser
     * decide how to encode an array is how the delimiter trap gets reintroduced: it would
     * either join the groups with commas - making one group of everything - or comma-split a
     * group's own names. Building it explicitly means each group is one encoded value and the
     * AND/OR structure cannot be flattened by accident.
     */
    function buildUrl(groups) {
        var params = new URLSearchParams();
        var parentId = currentParentId();
        if (parentId) params.set('parentId', parentId);

        var itemType = currentItemType();
        if (itemType) params.set('includeItemTypes', itemType);

        params.set('limit', '100');
        groups.forEach(function (names) {
            params.append('groups', names.join(GENRE_SEPARATOR));
        });

        return window.ApiClient.getUrl(ENDPOINT) + '?' + params.toString();
    }

    /** Pulls the server's typed error payload out of whatever the client rejected with. */
    function describeFailure(error) {
        var body = error && (error.responseText || error.body || error.response);
        if (typeof body === 'string') {
            try {
                var parsed = JSON.parse(body);
                if (parsed && parsed.Errors && parsed.Errors.length) return parsed.Errors.join(' ');
            } catch (ignored) {
                // Not JSON; fall through to the generic message.
            }
        }
        if (error && error.status === 403) return 'Not permitted to read this library.';
        if (error && error.status) return 'The server refused the request (' + error.status + ').';
        return 'Could not reach the server.';
    }

    function resolve(groups) {
        state.busy = true;
        state.error = null;
        render();

        return window.ApiClient.getJSON(buildUrl(groups)).then(function (response) {
            state.busy = false;
            state.result = response;
            render();
        }, function (error) {
            state.busy = false;
            state.result = null;
            state.error = describeFailure(error);
            render();
        });
    }

    // Re-resolving on every change is what makes the filter feel live; the debounce keeps a
    // click-through of several genres from firing a request each.
    var pending = null;
    function scheduleResolve() {
        if (pending) window.clearTimeout(pending);
        pending = window.setTimeout(function () {
            pending = null;
            applyFilter();
        }, 250);
    }

    /** Every group must have at least one name before a request means anything. */
    function activeGroups() {
        return state.groups.filter(function (names) { return names.length > 0; });
    }

    function applyFilter() {
        if (!window.ApiClient || !window.ApiClient.getCurrentUserId()) {
            state.error = 'Not signed in.';
            state.result = null;
            render();
            return;
        }

        // A genre name containing the separator cannot be expressed in this wire format:
        // the server would read it as two names and answer a different question. Refusing is
        // the only honest option - sending it would produce a plausible wrong result, which
        // is the failure this whole plugin exists to remove.
        var broken = [];
        state.groups.forEach(function (names) {
            names.forEach(function (name) {
                if (name.indexOf(GENRE_SEPARATOR) >= 0) broken.push(name);
            });
        });
        if (broken.length) {
            state.error = 'These genre names contain "' + GENRE_SEPARATOR + '" and cannot be '
                + 'filtered on: ' + broken.join(', ');
            state.result = null;
            render();
            return;
        }

        var groups = activeGroups();
        if (!groups.length) {
            // No criteria is "no filter", which is a different answer from "nothing matched"
            // and is reported as such rather than silently showing an empty grid.
            state.busy = false;
            state.error = null;
            state.result = { Items: [], TotalRecordCount: 0, FilterApplied: false, Criteria: [], Warnings: [] };
            render();
            return;
        }

        resolve(groups);
    }

    // ---------------------------------------------------------------- genre options

    function ensureGenres() {
        var parentId = currentParentId();
        if (state.genresFor === parentId && state.genres.length) {
            return Promise.resolve(state.genres);
        }

        var api = window.ApiClient;
        var userId = api && api.getCurrentUserId();
        if (!userId) return Promise.reject(new Error('Not signed in.'));

        var params = { userId: userId };
        var itemType = currentItemType();
        if (itemType) params.includeItemTypes = itemType;
        if (parentId) params.parentId = parentId;

        // The same endpoint the native panel uses (useGetQueryFiltersLegacy), so the options
        // offered here cannot drift from the ones the client itself would show.
        return api.getJSON(api.getUrl('Items/Filters', params)).then(function (response) {
            state.genres = (response.Genres || []).slice().sort();
            state.genresFor = parentId;
            render();
            return state.genres;
        }, function (error) {
            state.genresFor = undefined;
            throw error;
        });
    }

    /**
     * Whether a name is one the library actually offers.
     *
     * Only ever used to *label* a chip, never to block one. The list comes from
     * `Items/Filters`, which is scoped to the view being browsed and is not guaranteed to
     * enumerate every genre - so refusing an unlisted name would put a real genre out of
     * reach, while accepting it silently would present a typo as a valid filter. Marking it
     * is the honest middle, and the criteria trace then shows it matching nothing.
     */
    function isKnownGenre(name) {
        var needle = name.toLowerCase();
        return state.genres.some(function (known) {
            return known.toLowerCase() === needle;
        });
    }

    /**
     * Adds a genre to a group. The only way in, so the picker's options and the keyboard
     * cannot drift into behaving differently.
     */
    function addGenre(groupIndex, name) {
        var trimmed = name.trim();
        if (!trimmed) return;

        // A name carrying the separator cannot be expressed on the wire - the server splits
        // the parameter on it and would answer a different question than the one asked.
        // Caught here rather than in applyFilter, which would otherwise let the offending
        // chip exist and then refuse the entire filter.
        if (trimmed.indexOf(GENRE_SEPARATOR) >= 0) {
            state.error = 'Genre names cannot contain "' + GENRE_SEPARATOR + '".';
            render();
            return;
        }

        var names = state.groups[groupIndex];
        var already = names.some(function (existing) {
            return existing.toLowerCase() === trimmed.toLowerCase();
        });

        if (!already) names.push(trimmed);

        state.pickers[groupIndex] = '';
        state.highlight[groupIndex] = 0;
        state.error = null;
        scheduleResolve();
        render();

        // The dialog was rebuilt, so put the caret back where the user was working rather
        // than making them click the box again.
        var search = document.querySelector(
            '[data-jf-intersect="genre-search"][data-group-index="' + groupIndex + '"]');
        if (search) search.focus();
    }

    // ---------------------------------------------------------------- rendering

    function chip(groupIndex, name) {
        // An unlisted name is still a usable filter, so it is marked rather than refused -
        // see isKnownGenre. The dashed outline and the tooltip both say which it is; the
        // colour alone would not survive being told to someone over the phone.
        //
        // Guarded on having a list at all: an empty state.genres means the options never
        // loaded, and marking every chip then would report a failure of the genre list as
        // though it were a failure of the name.
        var known = !state.genres.length || isKnownGenre(name);

        return el('span', {
            class: known ? 'jf-chip' : 'jf-chip jf-chip-unknown',
            'data-jf-intersect': 'chip',
            title: known ? name : name + ' - not a genre this library lists'
        }, [
            el('span', { text: name }),
            el('button', {
                class: 'jf-chip-x',
                type: 'button',
                title: 'Remove ' + name,
                'aria-label': 'Remove ' + name,
                text: '×',
                onclick: function () {
                    var names = state.groups[groupIndex];
                    var at = names.indexOf(name);
                    if (at >= 0) names.splice(at, 1);
                    scheduleResolve();
                    render();
                }
            })
        ]);
    }

    function groupCard(groupIndex) {
        var names = state.groups[groupIndex];
        var picker = genrePicker(groupIndex);

        var children = [
            el('div', { class: 'jf-group-head' }, [
                el('span', { class: 'jf-group-title', text: 'Group ' + (groupIndex + 1) }),
                el('span', {
                    class: 'jf-group-hint',
                    text: names.length > 1 ? 'any of these' : 'this genre'
                }),
                state.groups.length > 1
                    ? el('button', {
                        class: 'jf-link',
                        type: 'button',
                        'data-jf-intersect': 'remove-group',
                        text: 'Remove group',
                        onclick: function () {
                            state.groups.splice(groupIndex, 1);
                            if (!state.groups.length) state.groups = [[]];
                            scheduleResolve();
                            render();
                        }
                    })
                    : null
            ]),
            el('div', { class: 'jf-chips' }, names.map(function (name) {
                return chip(groupIndex, name);
            }))
        ];

        if (picker) children.push(picker);
        return el('div', { class: 'jf-group', 'data-jf-intersect': 'group' }, children);
    }

    /** Fills a group's option list in place, so typing never rebuilds the input. */
    function renderOptions(groupIndex, list) {
        var query = (state.pickers && state.pickers[groupIndex]) || '';
        var names = state.groups[groupIndex];
        var needle = query.trim().toLowerCase();
        var active = state.highlight[groupIndex] || 0;

        var matches = state.genres.filter(function (name) {
            // Case-insensitive, because a genre can now arrive by typing rather than by
            // clicking: "action" already in the group has to hide "Action" from the options
            // just as an exact match would, or the same genre can be added twice in two cases.
            var already = names.some(function (existing) {
                return existing.toLowerCase() === name.toLowerCase();
            });
            return !already && (!needle || name.toLowerCase().indexOf(needle) >= 0);
        });

        while (list.firstChild) list.removeChild(list.firstChild);

        matches.slice(0, 60).forEach(function (name, index) {
            list.appendChild(el('button', {
                class: index === active ? 'jf-option jf-option-active' : 'jf-option',
                type: 'button',
                'data-jf-intersect': 'genre-option',
                text: name,
                onclick: function () {
                    addGenre(groupIndex, name);
                }
            }));
        });

        if (!matches.length) {
            list.appendChild(el('div', {
                class: 'jf-muted',
                text: state.genres.length ? 'No matching genre.' : 'Loading genres…'
            }));
        }
    }

    /**
     * The genre chooser for one group: a text box plus the names matching what has been typed.
     *
     * The box is an entry box, not merely a filter. The option list comes from
     * `Items/Filters`, which is scoped to the view being browsed and cannot be assumed
     * complete, so a real genre missing from it would otherwise be unreachable. Typing a name
     * and pressing Enter adds it; when the library does not list that genre, the chip says so
     * instead of passing itself off as a verified one.
     *
     * The list is filtered rather than shown whole because a real library can carry hundreds
     * of genres and the plugin targets large libraries.
     */
    function genrePicker(groupIndex) {
        var input = el('input', {
            class: 'jf-input',
            type: 'text',
            placeholder: 'Add a genre…',
            'data-jf-intersect': 'genre-search',
            'data-group-index': String(groupIndex),
            value: (state.pickers && state.pickers[groupIndex]) || ''
        });

        // The group index is on the list as well as the input so a caller can address one
        // group's options: every group's list is in the DOM at once, and an unqualified
        // "genre-option" query matches whichever group happens to come first.
        var list = el('div', {
            class: 'jf-options',
            'data-jf-intersect': 'genre-options',
            'data-group-index': String(groupIndex)
        });

        // Only the list is rebuilt as the user types. Re-rendering the dialog here would
        // replace the input mid-keystroke and drop focus - which is what the prototype's first
        // iteration did, making the box unusable.
        input.addEventListener('input', function () {
            state.pickers[groupIndex] = input.value;
            // A new query is a new list, so the keyboard returns to the top. Leaving it on
            // the previous row number would point Enter at an unrelated genre.
            state.highlight[groupIndex] = 0;
            renderOptions(groupIndex, list);
        });

        // Keyboard entry. Without this the box was a filter only: the placeholder said "Add a
        // genre..." while Enter did nothing, so every genre had to be found and clicked.
        input.addEventListener('keydown', function (event) {
            var options = list.querySelectorAll('[data-jf-intersect="genre-option"]');
            var active = state.highlight[groupIndex] || 0;

            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                if (!options.length) return;
                event.preventDefault();
                var step = event.key === 'ArrowDown' ? 1 : -1;
                // Wraps, so arrowing past the end returns to the top rather than sticking.
                state.highlight[groupIndex] = (active + step + options.length) % options.length;
                renderOptions(groupIndex, list);
                return;
            }

            if (event.key !== 'Enter') return;
            event.preventDefault();

            var typed = input.value.trim();
            if (!typed) return;

            // An exact match wins outright, so typing a genre in full and pressing Enter adds
            // that genre rather than whichever one happens to sort first.
            var needle = typed.toLowerCase();
            var exact = null;
            for (var i = 0; i < options.length; i++) {
                if (options[i].textContent.toLowerCase() === needle) {
                    exact = options[i];
                    break;
                }
            }

            if (exact) {
                addGenre(groupIndex, exact.textContent);
                return;
            }

            if (options.length) {
                addGenre(groupIndex, options[Math.min(active, options.length - 1)].textContent);
                return;
            }

            // The library lists nothing matching what was typed. Add it anyway - the list is
            // not guaranteed complete, and isKnownGenre marks the chip as unlisted so the
            // name is not mistaken for a verified one.
            addGenre(groupIndex, typed);
        });

        renderOptions(groupIndex, list);
        return el('div', { class: 'jf-picker' }, [input, list]);
    }

    function card(item) {
        var tag = item.ImageTags && item.ImageTags.Primary;
        var image = tag
            ? el('img', {
                class: 'jf-card-img',
                alt: '',
                loading: 'lazy',
                src: window.ApiClient.getUrl('Items/' + item.Id + '/Images/Primary', {
                    maxHeight: 300,
                    quality: 90,
                    tag: tag
                })
            })
            : el('div', { class: 'jf-card-img jf-card-img-none' });

        return el('button', {
            class: 'jf-card',
            type: 'button',
            'data-jf-intersect': 'card',
            'data-item-id': item.Id,
            title: item.Name,
            onclick: function () {
                // Hand off to the native details route rather than reimplementing a detail
                // view: it keeps this surface small and gives the user the real page.
                window.location.hash = '#/details?id=' + item.Id;
                closeDialog();
            }
        }, [image, el('div', { class: 'jf-card-name', text: item.Name })]);
    }

    function results() {
        var result = state.result;

        if (state.busy) return el('div', { class: 'jf-muted', 'data-jf-intersect': 'status', text: 'Searching…' });
        if (state.error) return el('div', { class: 'jf-error', 'data-jf-intersect': 'error', text: state.error });
        if (!result) return el('div', { class: 'jf-muted', 'data-jf-intersect': 'status', text: 'Pick a genre above.' });

        if (!result.FilterApplied) {
            return el('div', {
                class: 'jf-muted',
                'data-jf-intersect': 'status',
                text: 'No genres selected, so nothing is being filtered.'
            });
        }

        var items = result.Items || [];
        var total = result.TotalRecordCount || 0;

        // The narrowing is the whole point of the feature, so it is shown rather than hidden:
        // each criterion reports how many it matched and how many survived the AND.
        var trace = (result.Criteria || []).map(function (criterion) {
            return (criterion.Names || []).join(' or ') + ': '
                + criterion.Matched + ' → ' + criterion.RunningIntersection;
        }).join('   ·   ');

        if (!total) {
            // The trace belongs here most of all. An empty answer is the one case where
            // "which criterion killed it" has to be visible, and it used to be the one case
            // that omitted it: a group whose name matches nothing reads as "Dramma: 0 → 0"
            // and names the culprit, rather than leaving the user unable to tell a typo from
            // a library that genuinely has no such overlap.
            return el('div', { class: 'jf-results' }, [
                el('div', {
                    class: 'jf-muted',
                    'data-jf-intersect': 'status',
                    text: 'No items have every one of those genres.'
                }),
                trace ? el('div', { class: 'jf-trace', 'data-jf-intersect': 'trace', text: trace }) : null
            ]);
        }

        var summary = items.length < total
            ? 'Showing ' + items.length + ' of ' + total + ' items'
            : total + (total === 1 ? ' item' : ' items');

        return el('div', { class: 'jf-results' }, [
            el('div', { class: 'jf-summary', 'data-jf-intersect': 'summary', text: summary }),
            trace ? el('div', { class: 'jf-trace', 'data-jf-intersect': 'trace', text: trace }) : null,
            el('div', { class: 'jf-grid', 'data-jf-intersect': 'grid' }, items.map(card))
        ]);
    }

    function builder() {
        var children = state.groups.map(function (_, index) { return groupCard(index); });

        children.push(el('button', {
            class: 'jf-button',
            type: 'button',
            'data-jf-intersect': 'add-group',
            text: 'Add an AND group',
            onclick: function () {
                state.groups.push([]);
                render();
            }
        }));

        if (state.groups.length > 1) {
            children.push(el('div', {
                class: 'jf-muted jf-explain',
                text: 'An item must have a genre from every group. Within a group, any one is enough.'
            }));
        }

        return el('div', { class: 'jf-builder' }, children);
    }

    function dialogBody() {
        return [
            el('div', { class: 'jf-head' }, [
                el('h2', { class: 'jf-title', text: 'Filter by several genres at once' }),
                el('button', {
                    class: 'jf-close',
                    type: 'button',
                    title: 'Close',
                    'aria-label': 'Close',
                    'data-jf-intersect': 'close',
                    text: '×',
                    onclick: closeDialog
                })
            ]),
            el('p', {
                class: 'jf-muted jf-explain',
                text: 'Every group must match: this finds items that have a genre from each group, '
                    + 'rather than any of them.'
            }),
            builder(),
            results()
        ];
    }

    /**
     * Renders the dialog in place.
     *
     * The node is reused rather than replaced: the prototype's first iteration rebuilt it on
     * every change, which destroyed focus and made the genre filter box unusable.
     */
    function render() {
        var dialog = document.getElementById(DIALOG_ID);
        if (!dialog) return;

        while (dialog.firstChild) dialog.removeChild(dialog.firstChild);
        var panel = el('div', { class: 'jf-panel', role: 'dialog', 'aria-modal': 'true' },
            dialogBody());
        dialog.appendChild(panel);
    }

    // ---------------------------------------------------------------- dialog lifecycle

    function styles() {
        if (document.getElementById('jf-intersect-style')) return;

        // Colours come from the web client's own palette variables, so this follows the
        // user's chosen theme instead of guessing at one. The fallbacks are the dark defaults.
        var css = [
            '.jf-backdrop{position:fixed;inset:0;z-index:1400;display:flex;align-items:center;',
            'justify-content:center;background:rgba(0,0,0,.6);padding:24px}',
            '.jf-panel{background:var(--jf-palette-background-paper,#1c1c1c);',
            'color:var(--jf-palette-text-primary,#fff);border-radius:8px;',
            'box-shadow:0 12px 48px rgba(0,0,0,.5);width:min(1100px,100%);max-height:100%;',
            'overflow:auto;padding:20px;font:14px/1.45 system-ui,sans-serif}',
            '.jf-head{display:flex;align-items:center;justify-content:space-between;gap:12px}',
            '.jf-title{margin:0;font-size:1.15rem;font-weight:600}',
            '.jf-close{background:none;border:0;color:inherit;font-size:1.5rem;line-height:1;',
            'cursor:pointer;padding:0 6px;border-radius:4px}',
            '.jf-close:hover{background:var(--jf-palette-action-hover,rgba(255,255,255,.08))}',
            '.jf-muted{color:var(--jf-palette-text-secondary,#aaa)}',
            '.jf-explain{margin:8px 0 0;font-size:.85rem}',
            '.jf-builder{margin:16px 0}',
            '.jf-group{border:1px solid var(--jf-palette-divider,rgba(255,255,255,.16));',
            'border-radius:6px;padding:10px;margin-bottom:10px}',
            '.jf-group-head{display:flex;align-items:center;gap:10px;margin-bottom:8px}',
            '.jf-group-title{font-weight:600}',
            '.jf-group-hint{font-size:.8rem;color:var(--jf-palette-text-secondary,#aaa)}',
            '.jf-link{margin-left:auto;background:none;border:0;color:var(--jf-palette-primary-main,#4c8dff);',
            'cursor:pointer;font:inherit;font-size:.85rem}',
            '.jf-chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}',
            '.jf-chip{display:inline-flex;align-items:center;gap:4px;padding:3px 4px 3px 10px;',
            'border-radius:999px;background:var(--jf-palette-primary-main,#4c8dff);',
            'color:var(--jf-palette-primary-contrastText,#fff);font-size:.85rem}',
            '.jf-chip-x{background:none;border:0;color:inherit;cursor:pointer;font-size:1rem;line-height:1}',
            // A genre the library does not list. Dashed rather than merely a different colour,
            // so it survives colour-blindness and a low-contrast theme.
            '.jf-chip-unknown{background:none;color:inherit;',
            'border:1px dashed var(--jf-palette-error-main,#ff8a80)}',
            '.jf-input{width:100%;padding:7px 9px;border-radius:4px;font:inherit;',
            'border:1px solid var(--jf-palette-divider,rgba(255,255,255,.2));',
            'background:var(--jf-palette-background-default,#101010);color:inherit}',
            '.jf-options{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;max-height:150px;overflow:auto}',
            '.jf-option{border:1px solid var(--jf-palette-divider,rgba(255,255,255,.2));background:none;',
            'color:inherit;border-radius:999px;padding:3px 10px;cursor:pointer;font:inherit;font-size:.85rem}',
            '.jf-option:hover{background:var(--jf-palette-action-hover,rgba(255,255,255,.08))}',
            // Where the keyboard is. Enter adds this row, so it has to be visible.
            '.jf-option-active{background:var(--jf-palette-action-selected,rgba(255,255,255,.16));',
            'border-color:var(--jf-palette-primary-main,#4c8dff)}',
            '.jf-button{margin-top:4px;padding:7px 14px;border-radius:4px;cursor:pointer;font:inherit;',
            'border:1px solid var(--jf-palette-divider,rgba(255,255,255,.2));background:none;color:inherit}',
            '.jf-button:hover{background:var(--jf-palette-action-hover,rgba(255,255,255,.08))}',
            '.jf-entry{display:block;width:100%;margin:0 0 8px;padding:7px 10px;border-radius:4px;',
            'cursor:pointer;font:inherit;font-size:.85rem;text-align:left;',
            'border:1px solid var(--jf-palette-primary-main,#4c8dff);',
            'background:var(--jf-palette-action-hover,rgba(76,141,255,.12));color:inherit}',
            '.jf-results{margin-top:12px}',
            '.jf-summary{font-weight:600;margin-bottom:4px}',
            '.jf-trace{font-size:.8rem;color:var(--jf-palette-text-secondary,#aaa);margin-bottom:10px}',
            '.jf-error{color:#ff8a80;padding:8px 0}',
            '.jf-grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(140px,1fr))}',
            '.jf-card{background:none;border:0;padding:0;cursor:pointer;color:inherit;font:inherit;text-align:left}',
            '.jf-card-img{width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:6px;display:block;',
            'background:var(--jf-palette-background-default,#101010)}',
            '.jf-card-img-none{border:1px dashed var(--jf-palette-divider,rgba(255,255,255,.2))}',
            '.jf-card-name{font-size:.82rem;margin-top:5px;overflow:hidden;text-overflow:ellipsis}'
        ].join('');

        document.head.appendChild(el('style', { id: 'jf-intersect-style', text: css }));
    }

    /**
     * Dismisses the native filter popover our entry button lives in.
     *
     * This is not tidiness, it is what makes the genre box typable at all. That popover is a
     * MUI Modal, and a Modal enforces focus: while it is open it drags focus back to itself
     * the moment focus escapes, and our dialog sits outside it deliberately. Leaving it open
     * meant every keystroke went to the popover instead of the box - the box could be clicked,
     * focused for an instant, and never filled. Measured on 12.1: with the popover open the
     * input's value stayed empty through five keypresses and focus never left the popover
     * paper; with it closed the same five keys produced "Gamma".
     *
     * The events are dispatched on the element rather than sent as a click at coordinates,
     * because our own dialog covers the viewport and a real click would land on that instead.
     * MUI acts on mousedown, so the full sequence is sent - a lone click leaves it open.
     */
    function closeNativePopover() {
        var entry = document.querySelector(SEL_ENTRY);
        // Scoped via our own button, so this cannot reach some other popover's backdrop: the
        // first .MuiBackdrop-root in the document belongs to a menu that has nothing to do
        // with the filter popover.
        var root = (entry && entry.closest('.MuiModal-root'))
            || document.getElementById('filter-popover');
        if (!root) return;

        var target = root.querySelector('.MuiBackdrop-root') || root;
        var options = { bubbles: true, cancelable: true, view: window, detail: 1 };
        ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function (type) {
            var Ctor = type.indexOf('pointer') === 0 ? PointerEvent : MouseEvent;
            target.dispatchEvent(new Ctor(type, options));
        });
    }

    function openDialog() {
        if (state.open) return;
        state.open = true;
        state.result = null;
        state.error = null;

        // Before the dialog exists, while our entry is still findable inside the popover.
        closeNativePopover();

        var dialog = el('div', {
            id: DIALOG_ID,
            class: 'jf-backdrop',
            'data-jf-intersect': 'dialog',
            onclick: function (event) {
                // Click the backdrop, not the panel, to dismiss.
                if (event.target === dialog) closeDialog();
            }
        });
        dialog.appendChild(el('div', { class: 'jf-panel', role: 'dialog', 'aria-modal': 'true' }, dialogBody()));

        // Appended to body, outside #reactRoot: React's events never reach it, and the popover
        // closing cannot destroy it.
        document.body.appendChild(dialog);
        render();

        ensureGenres().then(function () {
            applyFilter();
        }, function (error) {
            state.error = error && error.message ? error.message : 'Could not load the genre list.';
            render();
        });
    }

    function closeDialog() {
        state.open = false;
        state.pickers = {};
        var dialog = document.getElementById(DIALOG_ID);
        if (dialog) dialog.remove();
    }

    // ---------------------------------------------------------------- injection

    function findFilterAnchor() {
        return document.getElementById('filtersGenres-header')
            || document.getElementById('filtersStatus-header')
            || document.getElementById('filtersStudios-header');
    }

    function mountEntry() {
        diagnostics.mountCalls++;

        var anchor = findFilterAnchor();
        if (!anchor) {
            diagnostics.lastSkip = 'no anchor';
            return false;
        }

        var container = anchor.closest('.MuiPopover-paper');
        if (!container) {
            diagnostics.lastSkip = 'anchor has no popover paper';
            return false;
        }

        // Purge any entry left in a popover we are no longer anchored to. Other popovers
        // render the same paper class, and this is what stopped duplicates accumulating.
        Array.prototype.forEach.call(document.querySelectorAll(SEL_ENTRY), function (node) {
            if (!container.contains(node)) node.remove();
        });

        // Membership is tested against the container, not the accordion: a sibling inserted
        // after the accordion has the container as its parent, and testing against the
        // accordion instead rebuilt the panel on every mutation of the document - measured at
        // 92 mounts during a single popover open.
        if (container.querySelector(SEL_ENTRY)) {
            diagnostics.lastSkip = null;
            return true;
        }

        container.insertBefore(el('button', {
            class: 'jf-entry',
            type: 'button',
            'data-jf-intersect': 'entry',
            text: 'Filter by several genres…',
            onclick: openDialog
        }), container.firstChild);

        diagnostics.mounts++;
        return true;
    }

    var scheduled = false;
    var observer = null;

    /**
     * Starts observing, or resumes after a pagehide disconnected us. Safe to call repeatedly:
     * observe() on an observer that is already attached just updates its options.
     */
    function watch() {
        if (!observer) {
            observer = new MutationObserver(function () {
                diagnostics.observerFires++;
                if (scheduled) return;
                scheduled = true;
                window.requestAnimationFrame(function () {
                    scheduled = false;
                    mountEntry();
                });
            });
        }

        // Observing body rather than documentElement, and debounced to one animation frame,
        // because this fires on every mutation in a busy client. A narrower subtree is not
        // available: the popover is mounted under body by the client itself.
        observer.observe(document.body, { childList: true, subtree: true });

        // The popover may already be open by the time we attach - on a resume, or on a slow
        // start - and no further mutation would tell us so.
        mountEntry();
    }

    function start() {
        styles();

        // The tag runs before the SPA has created window.ApiClient, so wait for it.
        var tries = 0;
        (function awaitApi() {
            if (window.ApiClient) {
                watch();
                return;
            }
            if (++tries > 120) {
                diagnostics.errors.push('ApiClient never appeared');
                return;
            }
            window.setTimeout(awaitApi, 250);
        })();

        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && state.open) closeDialog();
        });

        // The dialog is appended to body, outside React, so it is not part of any route's
        // tree and nothing unmounts it when the client navigates. Left alone it stayed on
        // screen over the next page - observed covering the dashboard after navigating to a
        // plugin's settings page by URL. The web client routes on the hash, so a hash change
        // is the navigation signal. Clicking a result card already closes the dialog; this
        // covers everywhere else (Back/Forward, a bookmark, a link).
        window.addEventListener('hashchange', function () {
            if (state.open) closeDialog();
        });

        // Stop working while the page is frozen or being torn down, and start again when it
        // comes back. The pair is the point: the web client moves between its routes without
        // ever reloading the document, and that transition fires pagehide and then pageshow
        // on the very same document - measured at one document, one body node, a pagehide at
        // the sign-in transition followed a millisecond later by pageshow. Disconnecting on
        // pagehide alone therefore killed the injection for the rest of the page's life: the
        // user signed in and got no entry button until they reloaded by hand.
        window.addEventListener('pagehide', function () {
            if (observer) observer.disconnect();
        });
        window.addEventListener('pageshow', watch);
    }

    // Exposed for the verification harness, and for anyone debugging in a console.
    window.__JF_INTERSECT__ = {
        state: state,
        diagnostics: diagnostics,
        open: openDialog,
        close: closeDialog,
        apply: applyFilter,
        buildUrl: buildUrl,
        mountEntry: mountEntry,
        setGroups: function (groups) {
            state.groups = groups;
            render();
        }
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
