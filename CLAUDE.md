# CorvusPixel

An MCP server that lets Claude Code draw pixel art on a **live** canvas. Changes
are pushed as diffs over a Unix socket to a separate renderer process, which
paints them in its own terminal pane using Unicode half-block characters (▀) —
real time, no file export.

## Build & run

- `./setup.sh` — create the venv, install deps, register the MCP server with
  `claude mcp add`, and open a two-pane tmux session (left: Claude Code, right:
  the live renderer).
- `.venv/bin/python server.py` — run the MCP server standalone.
- `.venv/bin/python renderer.py` — run the renderer standalone.
