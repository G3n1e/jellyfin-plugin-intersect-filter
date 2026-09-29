#!/usr/bin/env bash
# Generate a small, deterministic Jellyfin "Movies" library for prototyping.
#
# Neutral genre names (Alpha/Beta/Gamma/Delta) are deliberate: the fixtures are
# used for public docs and screenshots, and the counts below are exact so the
# verification harness can assert equality rather than approximate matches.
#
# Expected results against the generated library (see docs/VERIFICATION.md):
#
#   unfiltered                      = 6
#   Alpha                           = 4   {1,2,3,5}
#   Beta                            = 4   {1,2,4,5}
#   Gamma                           = 4   {3,4,5,6}
#   Alpha OR  Beta                  = 5   {1,2,3,4,5}
#   Alpha AND Beta                  = 3   {1,2,5}
#   Alpha AND Gamma                 = 2   {3,5}
#   Beta  AND Gamma                 = 2   {4,5}
#   (Alpha|Beta) AND (Gamma|Delta)  = 3   {3,4,5}     <- 2-group ceiling, API can do
#   Alpha AND Beta AND Gamma        = 1   {5}         <- 3 groups, API CANNOT do
#
# Note: an earlier draft of this file claimed the 2-group case was 4, counting
# "Gamma Delta". That item has neither Alpha nor Beta, so it is excluded; the
# harness caught the error. Trust the test, not the prose.
#
# The last case is the one that justifies the plugin (§3: the ceiling is 2).

set -euo pipefail

ROOT="${1:-/mnt/SSDs/SSD_1/docker/jellyfin-proto/media/Movies}"
mkdir -p "$ROOT"

# name|year|genre1,genre2,...
ITEMS=(
  "Alpha Beta One|2020|Alpha,Beta"
  "Alpha Beta Two|2021|Alpha,Beta"
  "Alpha Gamma|2020|Alpha,Gamma"
  "Beta Gamma|2021|Beta,Gamma"
  "All Three|2022|Alpha,Beta,Gamma"
  "Gamma Delta|2022|Gamma,Delta"
)

for entry in "${ITEMS[@]}"; do
  IFS='|' read -r name year genres <<<"$entry"
  dir="$ROOT/$name ($year)"
  base="$dir/$name ($year)"
  mkdir -p "$dir"

  # 1s 64x64 black clip: small, fast, and a valid H.264 stream Jellyfin will probe.
  if [ ! -f "$base.mp4" ]; then
    ffmpeg -hide_banner -loglevel error -y \
      -f lavfi -i "color=c=black:s=64x64:d=1" \
      -c:v libx264 -pix_fmt yuv420p -t 1 "$base.mp4"
  fi

  {
    printf '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
    printf '<movie>\n'
    printf '  <title>%s</title>\n' "$name"
    printf '  <year>%s</year>\n' "$year"
    IFS=',' read -ra gs <<<"$genres"
    for g in "${gs[@]}"; do printf '  <genre>%s</genre>\n' "$g"; done
    printf '</movie>\n'
  } >"$base.nfo"

  echo "  $name ($year)  [$(echo "$genres" | tr ',' ' ')]"
done

echo
echo "Generated ${#ITEMS[@]} items under $ROOT"
