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

## Standing rule: commit and push every meaningful change

After **every meaningful change** to this project — a code edit, a new feature,
a bug fix, a doc or config update — you MUST:

1. Stage the changed files and commit them with a clear, descriptive message.
2. Push to the GitHub remote (`git push`).

This is a **standing rule for every session working in this repo**, not a
one-time step. Review with `git status` / `git diff` before committing, and push
promptly so the remote never falls behind.
