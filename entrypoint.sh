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
    if [ ! -d node_modules ]; then
        echo "[DEV] Installing frontend dependencies..."
        npm install --no-audit --no-fund
    fi
    npm run dev -- --host 0.0.0.0 --port 5173 &
    cd /app || { echo "[DEV] Failed to return to app directory"; exit 1; }
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
