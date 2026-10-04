#!/usr/bin/env bash
# Lint the frontend TypeScript/TSX files pre-commit hands us, using the project's
# own eslint and flat config. Skips quietly when the frontend toolchain is not
# installed, so a fresh clone or a CI image without a build step is not blocked.
set -euo pipefail

if ! command -v node >/dev/null 2>&1; then
  echo "eslint-frontend: node not available, skipping"
  exit 0
fi

if [ ! -d frontend/node_modules ]; then
  echo "eslint-frontend: frontend/node_modules missing, skipping (run 'npm ci' in frontend/)"
  exit 0
fi

if [ "$#" -eq 0 ]; then
  exit 0
fi

exec npx --prefix frontend eslint --config frontend/eslint.config.js "$@"
