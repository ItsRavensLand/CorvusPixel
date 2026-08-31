#!/usr/bin/env bash
# CorvusPixel setup: install dependencies, register the MCP server with Claude
# Code, and open a two-pane tmux session:
#
#   left pane  -> Claude Code
#   right pane -> live renderer (renderer.py)
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="corvuspixel"
PYTHON="${PYTHON:-python3}"

echo "==> 1/4  Python venv + dependencies (mcp, rich)"
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    "$PYTHON" -m venv "$PROJECT_DIR/.venv"
fi
"$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install --quiet "mcp>=2,<3" "rich>=13"

echo "==> 2/4  Git repository"
if [ ! -d "$PROJECT_DIR/.git" ]; then
    git -C "$PROJECT_DIR" init
fi

echo "==> 3/4  Register MCP server with Claude Code (project scope)"
if command -v claude >/dev/null 2>&1; then
    claude mcp add --scope project corvuspixel -- \
        "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/server.py"
else
    echo "!! 'claude' CLI not found — skipping MCP registration."
    echo "   Install it, then run:"
    echo "   claude mcp add --scope project corvuspixel -- $PROJECT_DIR/.venv/bin/python $PROJECT_DIR/server.py"
fi

echo "==> 4/4  Open the two-pane tmux session"
if ! command -v tmux >/dev/null 2>&1; then
    echo "!! 'tmux' not found — start the two pieces manually:"
    echo "   left pane :  claude"
    echo "   right pane:  $PROJECT_DIR/.venv/bin/python $PROJECT_DIR/renderer.py"
    exit 0
fi

tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" -x 220 -y 50 -n pixel -c "$PROJECT_DIR"
tmux split-window -h -t "$SESSION:pixel" -c "$PROJECT_DIR"

# Right pane first so it is already retrying to connect while Claude boots.
tmux send-keys -t "$SESSION:pixel.1" ".venv/bin/python renderer.py" Enter
tmux send-keys -t "$SESSION:pixel.0" "claude" Enter

tmux select-pane -t "$SESSION:pixel.0"
tmux attach-session -t "$SESSION"
