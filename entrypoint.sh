#!/bin/bash
set -euo pipefail

# Entrypoint for OpenShift compatibility.
# OpenShift runs containers as a random UID in GID 0. K8s subPath
# volume mounts create intermediate directories as root, making
# ~/.config non-writable. XDG_CONFIG_HOME redirects config writes
# to a writable location.

# OpenShift runs an arbitrary UID: ~/.config is not ours and /tmp is an emptyDir
# whose root we may not own. Create the dir here so a bad mount fails at start
# with a named cause instead of on the first config write. Probe with a real
# create, not `test -w`: a dir that is writable but not traversable passes -w and
# then fails on the first config subdirectory -- creating a child needs both bits.
if [ -n "${XDG_CONFIG_HOME:-}" ]; then
    if ! mkdir -p "$XDG_CONFIG_HOME" 2>/dev/null \
        || ! mkdir "$XDG_CONFIG_HOME/.probe.$$" 2>/dev/null; then
        echo "[entrypoint] ERROR: XDG_CONFIG_HOME=$XDG_CONFIG_HOME is not writable (uid=$(id -u) gid=$(id -g))" >&2
        exit 1
    fi
    rmdir "$XDG_CONFIG_HOME/.probe.$$"
    echo "[entrypoint] XDG_CONFIG_HOME=$XDG_CONFIG_HOME (uid=$(id -u))"
fi
# Self-check hook for the `tox -e chart` env, gated on argv and not on an env var:
# the image's CMD is fixed, so nothing sets argv by accident, while a stray
# ROOTCOZ_* variable in a deployment would make the container exit 0 here with no
# server and no error -- a silent crash-loop.
if [ "${1:-}" = "--xdg-check" ]; then
    exit 0
fi

# Wait for a background app without letting our own signal traps end this script
# early: a TERM/INT trap makes `wait` return 128+signum the moment it fires, while
# the app is still shutting down (the lifespan shutdown that records an in-flight
# job as a server restart, #458, happens *during* it). Loop until the pid is really
# gone, and report the status of the `wait` that saw it exit -- the interruption's
# 143 is not the app's status, so a clean stop must not exit 143.
_supervise_app() {
    local app_pid="$1" status
    while :; do
        status=0
        wait "$app_pid" || status=$?
        kill -0 "$app_pid" 2>/dev/null || break
    done
    return "$status"
}

# Kill the sidecar when the app is gone. Every command here is tolerant on purpose:
# the `wait` returns the 143 the sidecar died with from the SIGTERM the line above
# just sent, and errexit would abort the EXIT trap there and hand 143 to Docker
# instead of the status `_run_app` computed (#458). A trap may clean up; it must
# never choose the exit status.
_cleanup_sidecar() {
    kill "$SIDECAR_PID" 2>/dev/null || true
    wait "$SIDECAR_PID" 2>/dev/null || true
}

# Watch the sidecar and remember a death nobody asked for. The watcher runs in a
# subshell, so it cannot set a variable the parent's exit path reads: it signals this
# shell instead (`$$` is this shell even inside a subshell) and the USR1 trap records
# the crash in the parent's own context. Arming the trap before the watcher starts also
# keeps USR1's default action -- kill this shell -- unreachable. The signal traps further
# down clear the record, so a requested stop is never reported as a crash.
_monitor_sidecar() {
    SIDECAR_DIED=false
    trap 'SIDECAR_DIED=true; kill -TERM "${APP_PID:-}" 2>/dev/null || true' USR1
    (trap 'exit 0' TERM
     while kill -0 "$SIDECAR_PID" 2>/dev/null; do sleep 5; done
     echo "[sidecar] Sidecar died, shutting down container"
     kill -USR1 $$ 2>/dev/null) &
}

# Run the app in the background so the sidecar's EXIT trap fires, forward the
# container's signals to it, and exit with the app's own status. The shipped path and
# the `--exit-check` harness below both come through here, so the harness asserts the
# status Docker really gets.
_run_app() {
    if [ -n "${extra_args:-}" ]; then
        "$@" $extra_args &
    else
        "$@" &
    fi
    APP_PID=$!

    # Forward signals to the app (|| true guards against errexit when PID gone). Both
    # also clear the sidecar-crash record: a stop we asked for is not a crash, and the
    # sidecar only dies here from `_cleanup_sidecar`, after the status is decided.
    trap 'SIDECAR_DIED=false; kill -TERM $APP_PID 2>/dev/null || true' TERM
    trap 'SIDECAR_DIED=false; kill -INT $APP_PID 2>/dev/null || true' INT

    # `wait` returns 128+signum the moment that TERM trap fires, while uvicorn is
    # still shutting down -- and the lifespan shutdown that records an in-flight
    # job as a server restart (#458) happens *during* that shutdown. A bare `wait`
    # under errexit therefore exited this script, and as PID 1 it took the
    # container (and uvicorn, mid-write) with it: SIGTERM looked like a crash on
    # the next boot. `_supervise_app` keeps waiting until the pid is really gone
    # and reports the app's own status, never the interruption's.
    status=0
    _supervise_app "$APP_PID" || status=$?
    # A sidecar that died on its own takes the app down with it, and the app can well
    # exit 0 on the way out -- a 0 would report that as a clean stop, so no supervisor
    # ever sees a crash: no CrashLoopBackOff, no restart alert. Report 70 instead. A stop
    # we asked for cleared SIDECAR_DIED in the traps above, so `docker stop` still exits 0.
    if [ "$status" -eq 0 ] && [ "${SIDECAR_DIED:-false}" = true ]; then
        echo "[sidecar] ERROR: sidecar died while the app was running — exiting 70" >&2
        status=70
    fi
    exit "$status"
}

# Self-check hook for the `tox -e chart` env, argv-gated like --xdg-check: it runs
# the real supervision function above against a child that ignores TERM until
# <release> exists, then exits <code>. The child touches <ready> once its trap is
# in place, so the harness cannot signal it in the startup window and read a 143
# that is the child's own, not the interruption's.
if [ "${1:-}" = "--wait-check" ]; then
    _check_release="$2"
    _check_code="$3"
    bash -c "trap '' TERM; : > \"$4\"; while [ ! -f \"$_check_release\" ]; do sleep 0.01; done; exit $_check_code" &
    _check_pid=$!
    trap 'kill -TERM $_check_pid 2>/dev/null || true' TERM
    status=0
    _supervise_app "$_check_pid" || status=$?
    exit "$status"
fi

# Self-check hook for the `tox -e chart` env, argv-gated like the two above: it runs
# the *whole* shipped exit path -- a live sidecar reaped by the EXIT trap, the signal
# traps and `_run_app`'s supervision -- against a fake sidecar and a fake app, then
# reports what Docker would see. `--wait-check` covers supervision alone, which is why
# it stayed green while every clean stop exited 143 (#458).
if [ "${1:-}" = "--exit-check" ]; then
    _check_release="$2"
    _check_code="$3"
    # Armed before the app exists, so a TERM landing between the fork inside `_run_app`
    # and its own trap cannot kill this shell with 143 and be read as the defect under
    # test. `_run_app` replaces it microseconds later.
    trap 'SIDECAR_DIED=false; kill -TERM ${APP_PID:-} 2>/dev/null || true' TERM
    # A sidecar that outlives the script, so the EXIT trap really kills and reaps it --
    # the shape that turned a clean exit into 143. It dies when argv 5 exists (never, if
    # unset), which is how the harness crashes it mid-run. The app mirrors --wait-check's
    # child: it ignores TERM until <release> exists, then exits <code>, and touches
    # <ready> once its trap is in place.
    bash -c "while [ ! -f \"${5:-/nonexistent}\" ]; do sleep 0.01; done" &
    SIDECAR_PID=$!
    trap _cleanup_sidecar EXIT
    _monitor_sidecar
    _run_app bash -c "trap '' TERM; : > \"$4\"; while [ ! -f \"$_check_release\" ]; do sleep 0.01; done; exit $_check_code"
fi

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

SIDECAR_RUNNING=false
if [ -f /app/sidecar-helper/dist/server.js ]; then
    export SIDECAR_PORT="${SIDECAR_PORT:-9100}"
    # Rootcoz Server Settings bridge: the sidecar reads RECORD_TRANSCRIPTS
    # from its process env at start. Rootcoz persists the DB-configured value
    # to /data/pi-sidecar/env; sourcing it here applies the setting to the
    # sidecar after a container restart.
    #
    # Capture the pristine deployment value FIRST: once the bridge assigns
    # RECORD_TRANSCRIPTS, the sidecar's boot env carries the DB value until
    # restart. Rootcoz reads ROOTCOZ_DEPLOY_RECORD_TRANSCRIPTS to push the
    # deployment value to the running sidecar when a DB override is reset.
    export ROOTCOZ_DEPLOY_RECORD_TRANSCRIPTS="${RECORD_TRANSCRIPTS:-}"
    if [ -f /data/pi-sidecar/env ]; then
        set -a
        . /data/pi-sidecar/env
        set +a
    fi
    # Transcripts must survive container replacement — keep them under /data.
    export PI_SIDECAR_TRANSCRIPTS_DIR="${PI_SIDECAR_TRANSCRIPTS_DIR:-/data/pi-sidecar/transcripts}"
    # A port override must not strand the Python client on the default 9100:
    # pi_sidecar_client reads SIDECAR_URL (default http://127.0.0.1:9100)
    # regardless of SIDECAR_PORT, so derive the URL from the effective port
    # unless the deployment (or the DB bridge above) set one explicitly.
    export SIDECAR_URL="${SIDECAR_URL:-http://127.0.0.1:${SIDECAR_PORT}}"
    mkdir -p "$PI_SIDECAR_TRANSCRIPTS_DIR" 2>/dev/null || echo "[sidecar] WARN: cannot create transcripts dir" >&2
    # Subagent extension falls back to spawning `pi` when argv[1] is unset;
    # keep the CLI on PATH so that path works in the container.
    export PATH="/app/sidecar-helper/node_modules/.bin:${PATH}"
    # NODE_BINARY comes from the DB bridge sourced above (node_binary
    # setting) or the deployment env — launching with it here is what makes
    # the saved setting take effect on restart.
    "${NODE_BINARY:-node}" /app/sidecar-helper/dist/server.js &
    SIDECAR_PID=$!
    SIDECAR_RUNNING=true
    echo "[sidecar] Started Pi SDK sidecar (PID $SIDECAR_PID) on port $SIDECAR_PORT"

    # Kill sidecar when main process exits
    trap _cleanup_sidecar EXIT

    # Monitor sidecar — if it dies, kill the main process too
    _monitor_sidecar

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
# Bounded graceful shutdown, so SIGTERM always reaches the lifespan shutdown that
# records an in-flight analysis as a server restart (#458). Budget: 15 s here +
# SHUTDOWN_DRAIN_SECONDS (40) in the app, under the chart's 60 s grace period --
# tox's chart env asserts the relationship, reading these two numbers from here
# and from src/rootcoz/main.py.
if [ "$has_uvicorn" = true ]; then
    extra_args="$extra_args --timeout-graceful-shutdown 15"
fi
if [ "$has_uvicorn" = true ] && [ "${DEV_MODE:-}" = "true" ]; then
    extra_args="$extra_args --reload --reload-dir /app/src"
fi

# When sidecar is running, run app in background + wait so EXIT trap fires
# for sidecar cleanup and signals are forwarded properly.
if [ "$SIDECAR_RUNNING" = true ]; then
    _run_app "$@"
else
    if [ -n "$extra_args" ]; then
        exec "$@" $extra_args
    else
        exec "$@"
    fi
fi
