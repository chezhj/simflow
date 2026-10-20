#!/bin/bash
# SessionStart hook for Claude Code cloud sessions: installs the Python
# dependencies so tests and linters work right away.
# Idempotent and non-interactive; only runs in remote (cloud) sessions.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"

# Backend: poetry.toml puts the virtualenv in .venv (including the dev group).
# pytest.ini already selects settings.dev, and the JS helper tests (node --test)
# have no dependencies to install.
poetry install --no-interaction
