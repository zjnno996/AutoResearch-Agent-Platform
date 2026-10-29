#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEFAULT_WEB_PYTHON=python3
[ -x "$PROJECT_ROOT/.venv-web/bin/python" ] && DEFAULT_WEB_PYTHON="$PROJECT_ROOT/.venv-web/bin/python"
SETUP_PYTHON="${BOOTSTRAP_PYTHON:-$DEFAULT_WEB_PYTHON}"
"$SETUP_PYTHON" -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,13), "Use Python 3.11 or 3.12 via BOOTSTRAP_PYTHON=/path/to/python"'
node -e 'const [major,minor]=process.versions.node.split(".").map(Number);if(!((major===20&&minor>=19)||(major===22&&minor>=12)||major>22))throw new Error("Node 20.19+ or 22.12+ is required")'
if [ ! -x "$PROJECT_ROOT/.venv-web/bin/python" ]; then
    "$SETUP_PYTHON" -m venv "$PROJECT_ROOT/.venv-web"
fi
"$PROJECT_ROOT/.venv-web/bin/python" -m pip install -e "$PROJECT_ROOT/backend/agent" -r "$PROJECT_ROOT/backend/requirements-web.txt"
npm ci --prefix "$PROJECT_ROOT/frontend-web"
printf '\nEMNETS-RA Web is ready. Start with: ./start-web.sh start\n'
