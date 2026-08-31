#!/usr/bin/env bash
# CorvusPixel setup: install dependencies and register the MCP server with Claude
# Code. The canvas window itself is opened by asking Claude Code to call the
# open_canvas tool — no tmux required.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

echo "==> 1/3  Python venv + dependencies (mcp, rich)"
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    "$PYTHON" -m venv "$PROJECT_DIR/.venv"
fi
"$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install --quiet "mcp>=2,<3" "rich>=13"

echo "==> 2/3  Git repository"
if [ ! -d "$PROJECT_DIR/.git" ]; then
    git -C "$PROJECT_DIR" init
fi

echo "==> 3/3  Register MCP server with Claude Code (user scope)"
if command -v claude >/dev/null 2>&1; then
    claude mcp add --scope user corvuspixel -- \
        "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/server.py"
else
    echo "!! 'claude' CLI not found — skipping MCP registration."
    echo "   Install it, then run:"
    echo "   claude mcp add --scope user corvuspixel -- $PROJECT_DIR/.venv/bin/python $PROJECT_DIR/server.py"
fi

cat <<'EOF'

CorvusPixel is ready. The server is registered at user scope, so in any
Claude Code session on this machine (any directory) just type /canvas to open
the interactive drawing window. Draw with your mouse/keyboard, read it back
with see_canvas, and ask Claude to draw only when you want it to.

Manual run (optional, if you skip MCP registration):
  terminal 1:  <project>/.venv/bin/python <project>/server.py
  terminal 2:  <project>/.venv/bin/python <project>/canvas_app.py --socket <server socket>
EOF
