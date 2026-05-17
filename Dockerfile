# Frontend build stage
FROM node:22-slim AS frontend-builder

WORKDIR /frontend

# Copy package files first for layer caching
COPY frontend/package.json frontend/package-lock.json ./

# Install dependencies
RUN npm ci

# Copy frontend source
COPY frontend/ .

# Build the frontend (vite build only — type checking runs in tox/CI)
RUN npx vite build

# Sidecar build stage
FROM node:22-slim AS sidecar-builder

WORKDIR /sidecar

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY sidecar-helper/package.json sidecar-helper/package-lock.json* ./
RUN npm ci

COPY sidecar-helper/ .
RUN npx tsc

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app

# Install git (needed for gitpython dependency)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src/ src/

# Create venv and install dependencies
RUN uv sync --frozen --no-dev

# Production stage
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

# Install bash, git (runtime for gitpython), curl (for Cursor CLI install)
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Node.js 22 from the build stage (Debian apt only has Node 18/20)
COPY --from=sidecar-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=sidecar-builder /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Create non-root user, data directory, and set permissions
# OpenShift runs containers as a random UID in the root group (GID 0)
RUN useradd --create-home --shell /bin/bash -g 0 appuser \
    && mkdir -p /data \
    && chown appuser:0 /data \
    && chmod -R g+w /data

# Switch to non-root user for CLI installs
USER appuser

# Install Cursor Agent CLI (installs to ~/.local/bin)
# Claude and Gemini are handled by the Pi SDK sidecar — no CLI needed
RUN /bin/bash -o pipefail -c "curl -fsSL https://cursor.com/install | bash"

# Configure npm for non-root global installs and install acpx CLI (needed by sidecar acpx-provider extension)
RUN mkdir -p /home/appuser/.npm-global \
    && npm config set prefix '/home/appuser/.npm-global' \
    && npm install -g acpx

# Switch to root for file copies and permission fixes
USER root

# Copy the virtual environment from builder
COPY --chown=appuser:0 --from=builder /app/.venv /app/.venv

# Copy project files needed by uv
COPY --chown=appuser:0 --from=builder /app/pyproject.toml /app/uv.lock ./

# Copy source code
COPY --chown=appuser:0 --from=builder /app/src /app/src

# Copy built frontend assets from frontend builder
COPY --chown=appuser:0 --from=frontend-builder /frontend/dist /app/frontend/dist

# Copy sidecar
COPY --chown=appuser:0 --from=sidecar-builder /sidecar/dist /app/sidecar-helper/dist
COPY --chown=appuser:0 --from=sidecar-builder /sidecar/node_modules /app/sidecar-helper/node_modules
COPY --chown=appuser:0 --from=sidecar-builder /sidecar/package.json /app/sidecar-helper/package.json

# Copy entrypoint script
COPY --chown=appuser:0 entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Make /app group-writable for OpenShift compatibility
RUN chmod -R g+w /app

# Make appuser home accessible by OpenShift arbitrary UID
# Only chmod directories (not files) — files are already group-readable by default.
# Directories need group write+execute for OpenShift's arbitrary UID (in GID 0)
# to create config/cache files at runtime.
RUN find /home/appuser -type d -exec chmod g=u {} + \
    && npm cache clean --force 2>/dev/null; \
    rm -rf /home/appuser/.npm/_cacache

# Switch back to non-root user for runtime
USER appuser

# Ensure CLIs are in PATH
ENV PATH="/home/appuser/.local/bin:/home/appuser/.npm-global/bin:${PATH}"
# Set HOME for OpenShift compatibility (random UID has no passwd entry)
ENV HOME="/home/appuser"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/health && curl -f http://localhost:${SIDECAR_PORT:-9100}/health || exit 1

# Use uv run for uvicorn
# --no-sync prevents uv from attempting to modify the venv at runtime.
# This is required for OpenShift where containers run as an arbitrary UID
# and may not have write access to the .venv directory.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uv", "run", "--no-sync", "uvicorn", "rootcoz.main:app", "--host", "0.0.0.0"]
