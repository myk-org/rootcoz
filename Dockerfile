# Frontend build stage
FROM registry.access.redhat.com/ubi9/nodejs-22-minimal AS frontend-builder

USER 0
WORKDIR /frontend

# Copy package files first for layer caching
COPY frontend/package.json frontend/package-lock.json ./

# Install dependencies
RUN npm ci

# Copy frontend source
COPY frontend/ .

# Build the frontend. `npm run build` is `tsc -b && vite build`: the type check is
# part of this stage on purpose. Plain `vite build` strips types without checking
# them, which is how six type errors once reached a branch uncaught.
# Apply group-write permissions here before COPY so the runtime stage needs no
# post-COPY `chmod -R g+w /app` (which would copy-on-write duplicate /app).
RUN npm run build \
    && test -f /frontend/dist/favicon.svg && test ! -e /frontend/dist/icons.svg \
    && find /frontend/dist -not -type l -exec chmod g+w {} +

# Sidecar build stage
FROM registry.access.redhat.com/ubi9/nodejs-22-minimal AS sidecar-builder

USER 0
WORKDIR /sidecar
# tree-sitter-kotlin falls back to node-gyp when no matching prebuild exists.
RUN microdnf install -y python3 make gcc-c++ && microdnf clean all

COPY sidecar-helper/package.json sidecar-helper/package-lock.json* ./
# pi-sidecar@>=4.2.0 requires Node >=22.19.0 (engines field); fail early if base image lags.
RUN node -e "const [maj,min]=process.versions.node.split('.').map(Number); if (maj<22||(maj===22&&min<19)) { console.error('Need Node >=22.19.0, got', process.versions.node); process.exit(1); }"
# --foreground-scripts: nested pi-sidecar/pi-coding-agent trees make parallel reify
# delete package dirs while their postinstall runs (ENOENT uv_cwd). Sequential scripts avoid it.
RUN DO_NOT_TRACK=1 npm ci --foreground-scripts

COPY sidecar-helper/ .
RUN npx tsc
RUN npm prune --omit=dev

# Prune build-only artifacts and non-target-arch prebuilds before COPY into the
# runtime stage, then set group-write permissions here to avoid a runtime CoW layer.
# Preserve runtime .js/.mjs/.cjs/.json/.node/.wasm files and node_modules/.bin/{trail,pi}.
RUN set -e \
    && ARCH="$(uname -m)" \
    && case "$ARCH" in \
        x86_64) NODE_PREBUILD="linux-x64" ;; \
        aarch64|arm64) NODE_PREBUILD="linux-arm64" ;; \
        *) NODE_PREBUILD="" ;; \
    esac \
    && find /sidecar/node_modules \
        -type f \( -name '*.map' -o -name '*.o' -o -name '*.c' -o -name '*.cc' -o -name '*.cpp' -o -name '*.h' \) \
        -exec rm -f {} + \
    && find /sidecar/node_modules \
        -type d \( -name 'obj.target' -o -name '.deps' -o -name 'test' -o -name 'tests' -o -name '__tests__' -o -name 'docs' \) \
        -prune -exec rm -rf {} + \
    && if [ -n "$NODE_PREBUILD" ]; then \
        find /sidecar/node_modules \
            -type d -name 'prebuilds' -exec sh -c ' \
                keep="$1"; shift; \
                for prebuild_dir in "$@"; do \
                    for target in "$prebuild_dir"/*; do \
                        [ -e "$target" ] || continue; \
                        [ "$(basename "$target")" = "$keep" ] && continue; \
                        rm -rf "$target"; \
                    done; \
                done' _ "$NODE_PREBUILD" {} +; \
    fi \
    && find /sidecar/dist /sidecar/node_modules /sidecar/package.json -not -type l -exec chmod g+w {} +

# Node.js runtime stage — copy only the node binary and bundled npm (used in
# DEV_MODE=true), omitting /usr/include/node headers, gnupg, corepack, and yarn.
# Keep current: the Node 22.23.0 keep-alive regression
# (nodejs/node#63989, broke google-auth-library/gaxios/node-fetch@2) was fixed
# in 22.23.4 — stay on >=22.23.4.
FROM node:22.23.4-bookworm-slim AS node-runtime

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app

# Install git (needed for gitpython dependency), plus the krb5 headers and
# a C toolchain required to build python-gssapi for the kerberos auth-mode
# extra (it has no wheels — it links against the system krb5 ABI).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gcc \
    libc6-dev \
    libkrb5-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src/ src/

# Create venv, install dependencies (kerberos extra: the Greenwave exporter's
# kerberos auth mode must work in the shipped image), and apply group-write
# permissions before COPY
RUN uv sync --frozen --no-dev --extra kerberos \
    && find /app/.venv /app/src -not -type l -exec chmod g+w {} +

# Production stage — plain python:3.14-slim-bookworm without shipping uv at runtime
FROM python:3.14-slim-bookworm

WORKDIR /app

# Install bash, git (runtime for gitpython), curl (healthcheck), ca-certificates,
# tini, and the krb5 runtime libraries the Greenwave kerberos auth mode links
# against (libgssapi-krb5-2/libkrb5-3 are pulled in transitively by curl,
# but the requirement is declared here so it cannot break on a base change)
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    git \
    curl \
    ca-certificates \
    tini \
    libgssapi-krb5-2 \
    libkrb5-3 \
    && rm -rf /var/lib/apt/lists/*

# Copy Node.js runtime and bundled npm from official node:22.23.4-bookworm-slim
# (no gnupg, no /usr/include/node headers, no redundant global npm upgrade layer).
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/bin/node /usr/local/bin/nodejs \
    && ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version

# Create non-root user, data/app directories, and set OpenShift GID-0 permissions.
# OpenShift runs containers as a random UID in the root group (GID 0).
# Pre-creating parent directories under /app with appuser:0 and g+w ensures every
# path under /app is group-writable without a post-COPY `chmod -R g+w /app` pass
# (which would copy-on-write duplicate ~1.17 GB).
# Only chmod directories under /home/appuser (files are already group-readable).
RUN useradd --create-home --shell /bin/bash -g 0 appuser \
    && mkdir -p /data /app /app/.venv /app/src /app/frontend/dist \
        /app/sidecar-helper/dist /app/sidecar-helper/node_modules \
        /app/sidecar-helper/bin /app/third_party \
    && chown appuser:0 /data /app /app/.venv /app/src /app/frontend /app/frontend/dist \
        /app/sidecar-helper /app/sidecar-helper/dist /app/sidecar-helper/node_modules \
        /app/sidecar-helper/bin /app/third_party \
    && chmod g+w /data /app /app/.venv /app/src /app/frontend /app/frontend/dist \
        /app/sidecar-helper /app/sidecar-helper/dist /app/sidecar-helper/node_modules \
        /app/sidecar-helper/bin /app/third_party \
    && find /home/appuser -type d -exec chmod g=u {} +

# Copy the virtual environment and source code from builder (already g+w)
COPY --chown=appuser:0 --from=builder /app/.venv /app/.venv
COPY --chown=appuser:0 --from=builder /app/src /app/src

# Copy built frontend assets from frontend builder (already g+w)
COPY --chown=appuser:0 --from=frontend-builder /frontend/dist /app/frontend/dist

# Copy sidecar (already pruned and g+w)
COPY --chown=appuser:0 --from=sidecar-builder /sidecar/dist /app/sidecar-helper/dist
COPY --chown=appuser:0 --from=sidecar-builder /sidecar/node_modules /app/sidecar-helper/node_modules
COPY --chown=appuser:0 --from=sidecar-builder /sidecar/package.json /app/sidecar-helper/package.json
COPY --chown=appuser:0 third_party/graft-LICENSE /app/third_party/graft-LICENSE
# Expose only the required sidecar CLIs (trail, pi) on PATH via a dedicated bin dir
# so transitive npm binaries (e.g. acpx) from node_modules/.bin are never on PATH.
RUN test -x /app/sidecar-helper/node_modules/.bin/trail \
    && test -x /app/sidecar-helper/node_modules/.bin/pi \
    && ln -sf /app/sidecar-helper/node_modules/.bin/trail /app/sidecar-helper/bin/trail \
    && ln -sf /app/sidecar-helper/node_modules/.bin/pi /app/sidecar-helper/bin/pi \
    && chown -h appuser:0 /app/sidecar-helper/bin/trail /app/sidecar-helper/bin/pi

# Copy entrypoint script and set permissions on the two build-context files only
COPY --chown=appuser:0 entrypoint.sh /app/entrypoint.sh
RUN chmod g+w /app/third_party/graft-LICENSE \
    && chmod +x,g+w /app/entrypoint.sh

# Switch back to non-root user for runtime
USER appuser

# Put the virtualenv (rootcoz CLI, uvicorn) and allowlisted sidecar tools (trail, pi) on PATH
ENV PATH="/app/.venv/bin:${PATH}"
ENV PATH="/app/sidecar-helper/bin:${PATH}"
# Set HOME for OpenShift compatibility (random UID has no passwd entry)
ENV HOME="/home/appuser"
ENV DO_NOT_TRACK=1

EXPOSE 8000

# Probe the backend only: /api/health validates the sidecar via the
# effective (possibly DB-overridden) sidecar port, so the healthcheck never
# depends on the sidecar listener port itself. The check rounds can take a
# few seconds (dependency probes run with internal timeouts), hence 10s.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/api/health || exit 1

# Which build this image is — commit, ref (the tag it is published under) and
# build time — reported by GET /api/version, the sidebar footer and `rootcoz
# version`. All three default empty: a plain local build carries no identity and
# the API omits the `build` block. Declared here, after the last RUN, because a
# build arg mid-stage invalidates every layer below it — these are read at
# runtime only (`server_status.read_build_info`).
#
# The arg names are the CI contract (`container.build-args`: `GIT_COMMIT={revision}`,
# `GIT_REF={tag}`, `BUILT_AT={created}`), not a free choice: a --build-arg whose
# name the Dockerfile does not declare is silently dropped and the image would
# carry no identity at all.
ARG GIT_COMMIT=""
ARG GIT_REF=""
ARG BUILT_AT=""
ENV ROOTCOZ_BUILD_COMMIT=${GIT_COMMIT} \
    ROOTCOZ_BUILD_REF=${GIT_REF} \
    ROOTCOZ_BUILD_BUILT_AT=${BUILT_AT}

# Invoke uvicorn directly from the built virtualenv without shipping uv at runtime.
ENTRYPOINT ["tini", "--", "/app/entrypoint.sh"]
CMD ["/app/.venv/bin/uvicorn", "rootcoz.main:app", "--host", "0.0.0.0"]
