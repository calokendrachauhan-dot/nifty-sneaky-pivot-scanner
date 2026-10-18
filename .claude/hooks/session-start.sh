#!/bin/bash
# Clones openGym next to this repo and installs its Node dependencies,
# so its app and tests are ready in every Claude Code cloud session.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

OPENGYM_DIR="${OPENGYM_DIR:-/home/user/openGym}"

if [ ! -d "$OPENGYM_DIR/.git" ]; then
  git clone --quiet https://github.com/DuarteSantos8/openGym "$OPENGYM_DIR"
fi

[ -f "$OPENGYM_DIR/.env" ] || cp "$OPENGYM_DIR/.env.example" "$OPENGYM_DIR/.env"

for pkg in api frontend mcp; do
  (cd "$OPENGYM_DIR/$pkg" && npm install --no-audit --no-fund --loglevel=error)
done

echo "export OPENGYM_DIR=\"$OPENGYM_DIR\"" >> "${CLAUDE_ENV_FILE:-/dev/null}"
