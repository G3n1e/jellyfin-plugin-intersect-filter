#!/usr/bin/env bash
#
# Build the plugin and install it into the throwaway Jellyfin instance on :8097.
#
# Never touches the production server (container "jellyfin", :8096). The config
# volume is bind-mounted from the host, so the plugin is written straight into
# it rather than docker cp'd.
#
# Plugin loading happens at startup, so the container is restarted. This is
# idempotent: re-running replaces the DLL and restarts again.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="$REPO_ROOT/src/BetterJellyfinFiltering"
CONTAINER="jellyfin-proto"
# The config volume is root-owned on the host, so files are copied through the
# docker daemon rather than written directly.
PLUGIN_DIR="/config/plugins/BetterJellyfinFiltering"

export PATH="$HOME/.dotnet:$PATH"
export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1

echo "==> building (Release)"
dotnet build "$PROJECT" -c Release -v minimal

OUT="$PROJECT/bin/Release/net10.0"
if [[ ! -f "$OUT/Jellyfin.Plugin.BetterFiltering.dll" ]]; then
    echo "ERROR: build produced no DLL at $OUT" >&2
    exit 1
fi

echo "==> installing into $CONTAINER:$PLUGIN_DIR"
docker exec "$CONTAINER" mkdir -p "$PLUGIN_DIR"
# Replace wholesale so a stale assembly from an earlier build cannot linger.
docker exec "$CONTAINER" sh -c "rm -f '$PLUGIN_DIR'/*.dll '$PLUGIN_DIR'/*.json"
docker cp "$OUT/Jellyfin.Plugin.BetterFiltering.dll" "$CONTAINER:$PLUGIN_DIR/"
docker cp "$PROJECT/meta.json" "$CONTAINER:$PLUGIN_DIR/"
docker exec "$CONTAINER" ls -la "$PLUGIN_DIR"

echo "==> restarting $CONTAINER"
docker restart "$CONTAINER" >/dev/null

echo -n "==> waiting for the server to come up"
up=0
for _ in $(seq 1 60); do
    if curl -fsS --max-time 3 "http://localhost:8097/System/Info/Public" >/dev/null 2>&1; then
        echo " ok"
        up=1
        break
    fi
    echo -n "."
    sleep 2
done

if [[ "$up" -ne 1 ]]; then
    echo " TIMED OUT" >&2
    docker logs --tail 40 "$CONTAINER" >&2
    exit 1
fi

# Confirm the plugin actually loaded rather than merely that the server is up. The log is
# captured into a variable and matched with a case pattern instead of being piped into
# `grep -q`: grep exits at the first match, docker then dies of SIGPIPE, and under
# `set -o pipefail` the pipeline reports failure even though the match succeeded. That is
# what made this check report "not seen" for a plugin that had loaded every time.
for _ in $(seq 1 20); do
    logs="$(docker logs "$CONTAINER" 2>&1 || true)"
    case "$logs" in
        *"Loaded plugin: Intersect"*)
            echo "    plugin loaded"
            exit 0
            ;;
    esac
    sleep 0.5
done

echo "    NOTE: plugin not seen in the server log - check: docker logs $CONTAINER"
exit 1
