# Changelog

## Version policy

This plugin is coupled to the Jellyfin **web client**, which it modifies by rewriting
`/web/index.html` as it is served. Jellyfin does not support web-client modification
([jellyfin#9095](https://github.com/jellyfin/jellyfin/pull/9095) was closed unmerged), so the
injection is the part that can break on upgrade — never the server API.

The policy is therefore explicit:

- **A plugin version states exactly one supported Jellyfin version.** The `targetAbi` in
  `meta.json` is the server `AssemblyVersion`, and Jellyfin refuses a plugin whose ABI does not
  match. There is no "supports 12.x" claim here, only "supports 12.1.0".
- **Every entry below names the Jellyfin version it supports.** If your server is not listed,
  the plugin has not been tested against it, whatever the ABI says.
- **The web-client build is part of the support statement.** `docs/FINDINGS.md` records the
  `jellyfin-web` commit each finding was measured against; a plugin version is only as tested as
  that pairing.
- **Breaking the injection is not a breaking change to the API.** The
  `/BetterJellyfinFiltering/Intersect` endpoint is expected to keep working across versions that
  break the UI. If the UI breaks, the fix is a plugin version, not a server downgrade.

## 0.1.0.0

First release.

**Supports Jellyfin 12.1.0** (`targetAbi` 12.1.0.0), verified against a live 12.1.0 server and
the `jellyfin-web` v12.1 tag (`fae41f33eb7cd636a9ef68984adb82bb247a6e1b`).

Added:

- `GET /BetterJellyfinFiltering/Intersect` — intersects any number of genre groups. `groups` is
  repeated once per AND-group; `|` ORs names within one. Removes the stock two-group ceiling.
- A browse-view filter UI: an entry button in the native filter panel opening a dialog with any
  number of genre groups, a live result grid, and a per-criterion trace showing how each group
  narrowed the result.
- Typed genre entry. A genre the library does not list is accepted and **marked** rather than
  refused or silently accepted, because `Items/Filters` is scoped to the view being browsed and
  is not a complete inventory.
- A settings page (Dashboard → Plugins → Intersect) with an injection off-switch and the three
  query bounds.
- Malformed input is a hard error, never a silently dropped filter.

Known limitations at this version:

- **Source-only.** No packaged release is published; see the README's install section.
- The assembly and route still carry the original `BetterJellyfinFiltering` name while the
  product is `jellyfin-plugin-intersect-filter`. The client hardcodes the route, so renaming
  moves both together. Deliberately deferred.
- Non-recursive queries skip the in-memory `UserViewBuilder` filtering the stock route applies
  to children. This differs only for virtual and user-specific items.
- One Jellyfin behaviour remains unexplained — an unfiltered `/Items` call returning 1,356 where
  the library held ~5,614. Recorded with a hypothesis in `docs/ARCHITECTURE.md` §5 rather than
  quietly omitted.
