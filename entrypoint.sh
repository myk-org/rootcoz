#!/bin/bash
# Entrypoint for OpenShift compatibility.
# OpenShift runs containers as a random UID in GID 0. K8s subPath
# volume mounts create intermediate directories as root, making
# ~/.config non-writable. XDG_CONFIG_HOME redirects config writes
# to a writable location.

# Cursor auth: create config dir (owned by runtime UID so chmod works)
# and symlink auth.json to the PVC mount for persistence
if [ -d /home/appuser/.cursor-auth ]; then
    _cursor_cfg="${XDG_CONFIG_HOME:-/home/appuser/.config}/cursor"
    mkdir -p "$_cursor_cfg"
    chmod 0700 "$_cursor_cfg"
    ln -sf /home/appuser/.cursor-auth/auth.json "$_cursor_cfg/auth.json"
fi

# Resolve PORT with a default so the exec-form CMD (which cannot expand
# shell variables) gets the correct bind port at runtime.
export PORT="${PORT:-8000}"

# Dev mode: start Vite dev server in background for frontend HMR
if [ "${DEV_MODE:-}" = "true" ] && [ -f /app/frontend/package.json ]; then
    echo "[DEV] Frontend source detected, starting Vite dev server..."
    cd /app/frontend || { echo "[DEV] Failed to change to frontend directory"; exit 1; }
    npm install --no-audit --no-fund
    npm run dev -- --host 0.0.0.0 --port 5173 &
    cd /app || { echo "[DEV] Failed to return to app directory"; exit 1; }
fi

# Start Pi SDK sidecar in background with lifecycle coupling
# Dev mode: rebuild TypeScript from source before starting
if [ "${DEV_MODE:-}" = "true" ] && [ -f /app/sidecar-helper/src/server.ts ]; then
    echo "[sidecar] Dev mode: compiling TypeScript..."
    cd /app/sidecar-helper || { echo "[sidecar] Failed to enter sidecar-helper"; exit 1; }
    # Run lifecycle scripts (same as Dockerfile `npm ci`) so @myk-org/pi-sidecar
    # postinstall can enforce the protobufjs CVE floor — do not use --ignore-scripts.
    npm install || { echo "[sidecar] npm install failed"; exit 1; }
    npx tsc || { echo "[sidecar] TypeScript build failed"; exit 1; }
    cd /app || { echo "[sidecar] Failed to return to /app"; exit 1; }
fi
if [ -f /app/sidecar-helper/dist/server.js ]; then
    export SIDECAR_PORT="${SIDECAR_PORT:-9100}"
    # pi-sidecar 4.x uses monorepo-relative paths for extensions; when consumed
    # as an npm package we must point to the hoisted pi-orchestrator-config copy.
    _pi_ext="/app/sidecar-helper/node_modules/pi-orchestrator-config/extensions"
    export SIDECAR_ACPX_EXTENSION_PATH="${SIDECAR_ACPX_EXTENSION_PATH:-$_pi_ext/acpx-provider/index.ts}"
    export SIDECAR_CLI_PROVIDER_EXTENSION_PATH="${SIDECAR_CLI_PROVIDER_EXTENSION_PATH:-$_pi_ext/cli-provider/index.ts}"
    # Subagent extension falls back to spawning `pi` when argv[1] is unset;
    # keep the CLI on PATH so that path works in the container.
    export PATH="/app/sidecar-helper/node_modules/.bin:${PATH}"
    node /app/sidecar-helper/dist/server.js &
    SIDECAR_PID=$!
    echo "[sidecar] Started Pi SDK sidecar (PID $SIDECAR_PID) on port $SIDECAR_PORT"

    # Kill sidecar when main process exits
    trap 'kill $SIDECAR_PID 2>/dev/null; wait $SIDECAR_PID 2>/dev/null' EXIT

    # Monitor sidecar — if it dies, kill the main process too
    (while kill -0 $SIDECAR_PID 2>/dev/null; do sleep 5; done; echo "[sidecar] Sidecar died, shutting down container"; kill 1 2>/dev/null) &
fi

# Check if any argument contains "uvicorn" to detect all uvicorn invocations
has_uvicorn=false
has_port=false
for arg in "$@"; do
    case "$arg" in
        *uvicorn*) has_uvicorn=true ;;
        --port|--port=*) has_port=true ;;
    esac
done

# Build final arguments
extra_args=""
if [ "$has_uvicorn" = true ] && [ "$has_port" = false ]; then
    extra_args="$extra_args --port $PORT"
fi
if [ "$has_uvicorn" = true ] && [ "${DEV_MODE:-}" = "true" ]; then
    extra_args="$extra_args --reload --reload-dir /app/src --timeout-graceful-shutdown 3"
fi

if [ -n "$extra_args" ]; then
    exec "$@" $extra_args
else
    exec "$@"
fi
