#!/bin/bash
set -euo pipefail

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
    # Bind-mounted host node_modules is often not writable by appuser. Skip
    # install when Vite is already present; never abort the API if npm fails.
    if [ -x node_modules/.bin/vite ]; then
        echo "[DEV] Using existing frontend/node_modules (skipping npm install)"
    elif [ ! -w . ] || { [ -d node_modules ] && [ ! -w node_modules ]; }; then
        echo "[DEV] frontend/node_modules is not writable; skipping npm install."
        echo "[DEV] Run 'npm install' on the host, or overlay an anonymous volume on /app/frontend/node_modules."
    else
        npm install --no-audit --no-fund || echo "[DEV] npm install failed; continuing"
    fi
    export VITE_CACHE_DIR="${VITE_CACHE_DIR:-/tmp/rootcoz-vite-cache}"
    mkdir -p "$VITE_CACHE_DIR"
    _vite_can_write_temp() {
        if mkdir -p node_modules/.vite-temp 2>/dev/null \
            && touch node_modules/.vite-temp/.write-probe 2>/dev/null; then
            rm -f node_modules/.vite-temp/.write-probe
            return 0
        fi
        # Host-owned .vite-temp: mkdir succeeds, writes fail. Remove it so Vite
        # falls back to a temp file next to vite.config.ts.
        rm -rf node_modules/.vite-temp 2>/dev/null || true
        touch .vite-config-write-probe 2>/dev/null || return 1
        rm -f .vite-config-write-probe
        return 0
    }
    if [ -x node_modules/.bin/vite ] && _vite_can_write_temp; then
        npm run dev -- --host 0.0.0.0 --port 5173 &
    elif [ -x node_modules/.bin/vite ]; then
        echo "[DEV] Vite cannot write under frontend/node_modules; skipping HMR."
        echo "[DEV] Overlay an anonymous volume on /app/frontend/node_modules, or chmod the host frontend dir."
    else
        echo "[DEV] Vite not found; serving built assets from /app/frontend/dist"
    fi
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

SIDECAR_RUNNING=false
if [ -f /app/sidecar-helper/dist/server.js ]; then
    export SIDECAR_PORT="${SIDECAR_PORT:-9100}"
    # Subagent extension falls back to spawning `pi` when argv[1] is unset;
    # keep the CLI on PATH so that path works in the container.
    export PATH="/app/sidecar-helper/node_modules/.bin:${PATH}"
    node /app/sidecar-helper/dist/server.js &
    SIDECAR_PID=$!
    SIDECAR_RUNNING=true
    echo "[sidecar] Started Pi SDK sidecar (PID $SIDECAR_PID) on port $SIDECAR_PORT"

    # Kill sidecar when main process exits
    trap 'kill $SIDECAR_PID 2>/dev/null; wait $SIDECAR_PID 2>/dev/null' EXIT

    # Monitor sidecar — if it dies, kill the main process too
    (trap 'exit 0' TERM
     while kill -0 $SIDECAR_PID 2>/dev/null; do sleep 5; done
     echo "[sidecar] Sidecar died, shutting down container"
     kill 1 2>/dev/null) &

    # Wait for sidecar to be healthy
    echo "[sidecar] Waiting for sidecar to be ready..."
    for i in $(seq 1 30); do
        curl -sf "http://127.0.0.1:${SIDECAR_PORT}/health" > /dev/null 2>&1 && break
        sleep 0.5
    done
    if ! curl -sf "http://127.0.0.1:${SIDECAR_PORT}/health" > /dev/null 2>&1; then
        echo "[sidecar] ERROR: not healthy after 15s — aborting" >&2
        exit 1
    fi
    echo "[sidecar] Sidecar is ready"
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

# When sidecar is running, run app in background + wait so EXIT trap fires
# for sidecar cleanup and signals are forwarded properly.
if [ "$SIDECAR_RUNNING" = true ]; then
    if [ -n "$extra_args" ]; then
        "$@" $extra_args &
    else
        "$@" &
    fi
    APP_PID=$!

    # Forward signals to the app (|| true guards against errexit when PID gone)
    trap 'kill -TERM $APP_PID 2>/dev/null || true' TERM
    trap 'kill -INT $APP_PID 2>/dev/null || true' INT

    wait $APP_PID
else
    if [ -n "$extra_args" ]; then
        exec "$@" $extra_args
    else
        exec "$@"
    fi
fi
