---
name: canvas
description: Open the CorvusPixel drawing canvas in a new terminal window, linked to this session.
---

# /canvas — open the CorvusPixel drawing canvas

Open the pixel-art canvas in a new terminal window connected to this session's
socket. This is the one command to remember — no tool names needed.

## 1. Confirm the MCP server is registered

The `open_canvas` and `canvas_status` tools come from the CorvusPixel MCP
server. If they are not available in this session, the server isn't registered
for this session. Decide how to respond from where this session is running:

- **Inside the CorvusPixel project** — the session's working directory is (or
  is under) the repo root; you can confirm cheaply, e.g. `ls server.py` and
  `grep -l CorvusPixel CLAUDE.md`, or `git remote -v` showing
  `ItsRavensLand/CorvusPixel`. Then tell the user to run `./setup.sh` to
  register the server and restart Claude Code so the tools load.
- **Outside the project** — say plainly: "This command only works inside the
  CorvusPixel project. cd into the project directory and start Claude Code
  there (e.g. `cd /path/to/CorvusPixel && claude`), then run /canvas again."
  Do not mention `./setup.sh` (it only exists inside the project).

Do not call `open_canvas()` until the tools exist.

## 2. Don't open a duplicate window

Call `canvas_status()` first. If it returns `open`, a canvas window is already
connected to this session — do **not** call `open_canvas()` again. Tell the
user: "Canvas already open in this session."

## 3. Open the canvas

- **No size argument**: call `open_canvas()`. The canvas keeps its current
  size (the session default, or whatever `init_canvas` was last given).
- **Size argument** (`/canvas 64x64`): parse `$ARGUMENTS` as `WIDTHxHEIGHT`
  (e.g. `64x64`, case-insensitive `x`, surrounding whitespace ignored). Call
  `init_canvas(width=WIDTH, height=HEIGHT)` to (re)create the canvas at that
  size, then call `open_canvas()`.

The window connects to this session's socket automatically; the user draws
there with mouse or keyboard. Read their drawing back with `see_canvas()` only
when it is relevant to the conversation.
