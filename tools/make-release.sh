#!/usr/bin/env bash
#
# Build the release zip and print the manifest entry it belongs to.
#
# The plugin repository manifest needs a sourceUrl and a checksum for each version, and
# neither can be written before the artefact exists. This produces both from the real
# build, so the numbers in manifest.json are measured rather than invented.
#
# Usage:
#   bash tools/make-release.sh
#
# Then either paste the printed JSON into manifest.json, or re-run with --write to have
# this script fill it in for you.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="$REPO_ROOT/src/BetterJellyfinFiltering"
OUT="$REPO_ROOT/artifacts"
WRITE=0
[[ "${1:-}" == "--write" ]] && WRITE=1

export PATH="$HOME/.dotnet:$PATH"
export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1

VERSION="$(sed -n 's:.*<Version>\(.*\)</Version>.*:\1:p' "$PROJECT/BetterJellyfinFiltering.csproj" | head -1)"
TARGET_ABI="$(sed -n 's:.*<JellyfinVersion>\(.*\)</JellyfinVersion>.*:\1:p' "$REPO_ROOT/Directory.Build.props" | head -1)"

if [[ -z "$VERSION" || -z "$TARGET_ABI" ]]; then
    echo "ERROR: could not read Version or JellyfinVersion" >&2
    exit 1
fi

echo "==> building ${VERSION} against Jellyfin ${TARGET_ABI}"
dotnet build "$PROJECT" -c Release -v minimal

BUILT="$PROJECT/bin/Release/net10.0/Jellyfin.Plugin.BetterFiltering.dll"
[[ -f "$BUILT" ]] || { echo "ERROR: no DLL at $BUILT" >&2; exit 1; }

# A Jellyfin plugin zip holds the DLL and meta.json at its root, with no wrapping folder.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp "$BUILT" "$STAGE/Jellyfin.Plugin.BetterFiltering.dll"
cp "$PROJECT/meta.json" "$STAGE/meta.json"

mkdir -p "$OUT"
ZIP="$OUT/intersect_${VERSION}.zip"
rm -f "$ZIP"

# Built with Python rather than the `zip` binary, which is not installed everywhere, and
# because determinism matters here: entries are sorted, timestamps pinned and permissions
# fixed, so the same source produces the same checksum on any machine. Without that, every
# rebuild invalidates the manifest entry for a byte-identical plugin, and a checksum that
# changes for no reason is worse than no checksum.
python3 - "$ZIP" "$STAGE" <<'PY'
import os, sys, zipfile

out, stage = sys.argv[1], sys.argv[2]
paths = sorted(
    os.path.join(dirpath, name)
    for dirpath, _, names in os.walk(stage)
    for name in names
)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in paths:
        info = zipfile.ZipInfo(os.path.relpath(path, stage), date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        with open(path, "rb") as handle:
            archive.writestr(info, handle.read())
print(f"    packed {len(paths)} files")
PY

CHECKSUM="$(sha256sum "$ZIP" | cut -d' ' -f1)"
URL="https://github.com/G3n1e/jellyfin-plugin-intersect-filter/releases/download/v${VERSION}/intersect_${VERSION}.zip"

echo
echo "==> $ZIP"
echo "    bytes    $(wc -c < "$ZIP")"
echo "    sha256   $CHECKSUM"
echo
echo "manifest.json version entry:"
cat <<JSON
      {
        "version": "${VERSION}",
        "changelog": "REPLACE with a one-line summary of this release",
        "targetAbi": "${TARGET_ABI}.0",
        "sourceUrl": "${URL}",
        "checksum": "${CHECKSUM}",
        "timestamp": "$(date -u +%Y-%m-%dT00:00:00.0000000Z)"
      }
JSON

if [[ "$WRITE" -eq 1 ]]; then
    echo
    echo "==> rewriting manifest.json (sourceUrl + checksum for ${VERSION})"
    python3 - "$REPO_ROOT/manifest.json" "$VERSION" "$TARGET_ABI.0" "$URL" "$CHECKSUM" <<'PY'
import json, sys
path, version, abi, url, checksum = sys.argv[1:6]
with open(path) as fh:
    manifest = json.load(fh)
for version_entry in manifest[0]["versions"]:
    if version_entry["version"] == version:
        version_entry["sourceUrl"] = url
        version_entry["checksum"] = checksum
        version_entry["targetAbi"] = abi
with open(path, "w") as fh:
    json.dump(manifest, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print("    done")
PY
else
    echo
    echo "Re-run with --write to fill manifest.json automatically."
fi
